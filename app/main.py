import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .config_manager import ConfigManager
from .models import Config, PrintMeta
from .mqtt_client import BambuMQTTClient
from .camera import capture_frame
from .timelapse import generate_timelapse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="BambuTimelapse")

# --- Global State ---
config_manager = ConfigManager()
mqtt_client: Optional[BambuMQTTClient] = None
event_queue: asyncio.Queue = asyncio.Queue()
ws_clients: Set[WebSocket] = set()
current_print: Optional[PrintMeta] = None
layer_capture_counter: int = 0
capture_semaphore = asyncio.Semaphore(1)
generating_prints: Set[str] = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_prints_dir() -> str:
    return os.path.join(config_manager.config.app.data_dir, "prints")


def _utcnow() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _sanitize_name(name: str) -> str:
    """Turn a gcode file name into a safe directory-name suffix."""
    name = os.path.splitext(name)[0]                       # strip extension
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)           # replace bad chars
    name = re.sub(r"_+", "_", name).strip("_")             # collapse underscores
    return name[:48]                                        # cap length


async def broadcast(message: dict):
    dead: Set[WebSocket] = set()
    for ws in list(ws_clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


def _save_meta(meta: PrintMeta):
    path = os.path.join(get_prints_dir(), meta.id, "meta.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(meta.model_dump(), f, indent=2)


def _load_meta(print_id: str) -> Optional[PrintMeta]:
    path = os.path.join(get_prints_dir(), print_id, "meta.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return PrintMeta(**json.load(f))
    except Exception:
        return None


def _list_prints():
    prints_dir = get_prints_dir()
    if not os.path.exists(prints_dir):
        return []

    results = []
    for entry in sorted(os.listdir(prints_dir), reverse=True):
        entry_path = os.path.join(prints_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        meta = _load_meta(entry)
        if not meta:
            continue

        frames_dir = os.path.join(entry_path, "frames")
        frame_count = (
            len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
            if os.path.exists(frames_dir)
            else 0
        )

        has_cover = os.path.exists(os.path.join(entry_path, "cover.jpg"))
        has_timelapse = os.path.exists(os.path.join(entry_path, "timelapse.mp4"))

        results.append({
            **meta.model_dump(),
            "frame_count": frame_count,
            "has_cover": has_cover,
            "cover_url": f"/api/prints/{entry}/cover" if has_cover else None,
            "timelapse_generated": has_timelapse,
            "timelapse_url": f"/api/prints/{entry}/timelapse" if has_timelapse else None,
            "is_generating": entry in generating_prints,
        })
    return results


# ---------------------------------------------------------------------------
# Print Session Logic
# ---------------------------------------------------------------------------

async def _new_print_session(layer: int = 0, total: int = 0, file_name: str = "") -> PrintMeta:
    global current_print, layer_capture_counter

    # Gracefully close any prior session
    if current_print is not None:
        current_print.gcode_state = "INTERRUPTED"
        current_print.end_time = _utcnow()
        _save_meta(current_print)

    layer_capture_counter = 0
    # Directory name: timestamp (local TZ for readability) + sanitized file name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _sanitize_name(file_name) if file_name else ""
    print_id = f"{ts}_{safe}" if safe else ts

    frames_dir = os.path.join(get_prints_dir(), print_id, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    meta = PrintMeta(
        id=print_id,
        label=file_name,
        file_name=file_name,
        start_time=_utcnow(),
        gcode_state="RUNNING",
        current_layer=layer,
        total_layers=total,
    )
    _save_meta(meta)
    current_print = meta
    await broadcast({"type": "print_started", "print": meta.model_dump()})
    logger.info(f"Print session started: {print_id}")
    return meta


async def _capture_frame_task(print_id: str, layer: int):
    """Capture a frame and save it to the print's frames directory."""
    async with capture_semaphore:
        cfg = config_manager.config
        if not cfg.camera.rtsp_url:
            return

        frames_dir = os.path.join(get_prints_dir(), print_id, "frames")
        frame_path = os.path.join(frames_dir, f"{layer:06d}.jpg")

        success = await capture_frame(cfg.camera.rtsp_url, frame_path)
        if success and current_print and current_print.id == print_id:
            current_print.frame_count += 1
            _save_meta(current_print)
            await broadcast({
                "type": "frame_captured",
                "print_id": print_id,
                "layer": layer,
                "frame_count": current_print.frame_count,
            })


async def _handle_print_finish(print_id: str):
    """Wait 30s after finish, capture final frame, generate timelapse."""
    global current_print

    logger.info(f"Print {print_id} finishing — waiting 30s for final capture")
    await broadcast({"type": "print_finishing", "print_id": print_id})
    await asyncio.sleep(30)

    cfg = config_manager.config
    if cfg.camera.rtsp_url:
        frames_dir = os.path.join(get_prints_dir(), print_id, "frames")
        final_path = os.path.join(frames_dir, "final.jpg")
        cover_path = os.path.join(get_prints_dir(), print_id, "cover.jpg")

        success = await capture_frame(cfg.camera.rtsp_url, final_path)
        if success:
            shutil.copy2(final_path, cover_path)
            if current_print and current_print.id == print_id:
                current_print.frame_count += 1

    if current_print and current_print.id == print_id:
        current_print.gcode_state = "FINISH"
        current_print.end_time = _utcnow()
        _save_meta(current_print)

    await broadcast({"type": "print_finished", "print_id": print_id})
    logger.info(f"Print {print_id} complete")

    if cfg.timelapse.auto_generate:
        await _generate_timelapse_task(print_id)

    if current_print and current_print.id == print_id:
        current_print = None


async def _generate_timelapse_task(print_id: str):
    if print_id in generating_prints:
        return

    generating_prints.add(print_id)
    await broadcast({"type": "timelapse_generating", "print_id": print_id})

    cfg = config_manager.config
    frames_dir = os.path.join(get_prints_dir(), print_id, "frames")
    output_path = os.path.join(get_prints_dir(), print_id, "timelapse.mp4")

    try:
        success = await generate_timelapse(
            frames_dir, output_path,
            fps=cfg.timelapse.fps,
            quality=cfg.timelapse.quality,
        )
    except Exception as e:
        logger.error(f"Timelapse task error: {e}")
        success = False
    finally:
        generating_prints.discard(print_id)

    if success:
        meta = _load_meta(print_id)
        if meta:
            meta.timelapse_generated = True
            _save_meta(meta)
        await broadcast({
            "type": "timelapse_ready",
            "print_id": print_id,
            "url": f"/api/prints/{print_id}/timelapse",
        })
    else:
        await broadcast({"type": "timelapse_failed", "print_id": print_id})


# ---------------------------------------------------------------------------
# Event Processing Loop
# ---------------------------------------------------------------------------

async def process_events():
    global current_print, layer_capture_counter

    while True:
        try:
            event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break

        cfg = config_manager.config

        if event["type"] == "connection":
            await broadcast({
                "type": "mqtt_status",
                "connected": event["connected"],
                "error": event.get("error"),
            })

        elif event["type"] == "status":
            if current_print:
                current_print.current_layer = event["layer"]
                current_print.total_layers = event["total_layers"]
                current_print.gcode_state = event["gcode_state"]
                _save_meta(current_print)
            await broadcast({"type": "status", **event})

        elif event["type"] == "print_start":
            await _new_print_session(file_name=event.get("file_name", ""))

        elif event["type"] == "layer_change":
            # Lazy-create session if we connected mid-print
            if current_print is None:
                file_name = mqtt_client.subtask_name if mqtt_client else ""
                await _new_print_session(event["layer"], event["total_layers"], file_name)

            layer_capture_counter += 1
            if current_print and layer_capture_counter % cfg.timelapse.capture_every_n_layers == 0:
                asyncio.create_task(
                    _capture_frame_task(current_print.id, event["layer"])
                )

        elif event["type"] == "print_finish":
            if current_print:
                asyncio.create_task(_handle_print_finish(current_print.id))


# ---------------------------------------------------------------------------
# App Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    os.makedirs(get_prints_dir(), exist_ok=True)
    asyncio.create_task(process_events())

    cfg = config_manager.config
    if all([cfg.printer.ip, cfg.printer.serial, cfg.printer.access_code]):
        asyncio.create_task(_auto_connect())


async def _auto_connect():
    await asyncio.sleep(1)
    await _connect_mqtt()


async def _connect_mqtt() -> bool:
    global mqtt_client
    cfg = config_manager.config

    if not all([cfg.printer.ip, cfg.printer.serial, cfg.printer.access_code]):
        return False

    if mqtt_client:
        mqtt_client.stop()
        mqtt_client = None

    loop = asyncio.get_event_loop()
    mqtt_client = BambuMQTTClient(cfg.printer.ip, cfg.printer.serial, cfg.printer.access_code)
    mqtt_client.start(event_queue, loop)
    return True


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)

    try:
        await websocket.send_json({
            "type": "init",
            "mqtt_connected": mqtt_client.is_connected if mqtt_client else False,
            "current_print": current_print.model_dump() if current_print else None,
            "printer_status": mqtt_client.get_status() if mqtt_client else None,
            "generating_prints": list(generating_prints),
        })

        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        ws_clients.discard(websocket)


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status():
    return {
        "mqtt_connected": mqtt_client.is_connected if mqtt_client else False,
        "printer_status": mqtt_client.get_status() if mqtt_client else None,
        "current_print": current_print.model_dump() if current_print else None,
        "generating_prints": list(generating_prints),
    }


@app.get("/api/config")
async def get_config():
    return config_manager.config.model_dump()


@app.post("/api/config")
async def save_config(config: Config):
    config_manager.update(config)
    return {"status": "saved"}


@app.post("/api/connect")
async def connect_printer():
    success = await _connect_mqtt()
    if not success:
        raise HTTPException(400, "Printer settings incomplete — configure IP, Serial, and Access Code")
    return {"status": "connecting"}


@app.post("/api/disconnect")
async def disconnect_printer():
    global mqtt_client
    if mqtt_client:
        mqtt_client.stop()
        mqtt_client = None
    await broadcast({"type": "mqtt_status", "connected": False})
    return {"status": "disconnected"}


@app.get("/api/prints")
async def list_prints():
    return _list_prints()


@app.get("/api/prints/{print_id}")
async def get_print(print_id: str):
    meta = _load_meta(print_id)
    if not meta:
        raise HTTPException(404, "Print not found")
    return meta.model_dump()


@app.put("/api/prints/{print_id}/label")
async def update_label(print_id: str, body: dict):
    meta = _load_meta(print_id)
    if not meta:
        raise HTTPException(404, "Print not found")
    meta.label = str(body.get("label", ""))[:100]
    _save_meta(meta)
    return {"status": "updated"}


@app.post("/api/prints/{print_id}/generate")
async def generate_print_timelapse(print_id: str, background_tasks: BackgroundTasks):
    meta = _load_meta(print_id)
    if not meta:
        raise HTTPException(404, "Print not found")
    if print_id in generating_prints:
        raise HTTPException(409, "Already generating")
    background_tasks.add_task(_generate_timelapse_task, print_id)
    return {"status": "generating"}


@app.delete("/api/prints/{print_id}/timelapse")
async def delete_timelapse(print_id: str):
    path = os.path.join(get_prints_dir(), print_id, "timelapse.mp4")
    if os.path.exists(path):
        os.remove(path)
    meta = _load_meta(print_id)
    if meta:
        meta.timelapse_generated = False
        _save_meta(meta)
    return {"status": "deleted"}


@app.delete("/api/prints/{print_id}")
async def delete_print(print_id: str):
    path = os.path.join(get_prints_dir(), print_id)
    if not os.path.exists(path):
        raise HTTPException(404, "Print not found")
    shutil.rmtree(path)
    return {"status": "deleted"}


@app.get("/api/prints/{print_id}/cover")
async def get_cover(print_id: str):
    cover_path = os.path.join(get_prints_dir(), print_id, "cover.jpg")
    if os.path.exists(cover_path):
        return FileResponse(cover_path)

    # Fallback: latest frame
    frames_dir = os.path.join(get_prints_dir(), print_id, "frames")
    if os.path.exists(frames_dir):
        frames = sorted([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
        if frames:
            return FileResponse(os.path.join(frames_dir, frames[-1]))

    raise HTTPException(404, "No cover image available")


@app.get("/api/prints/{print_id}/latest-frame")
async def get_latest_frame(print_id: str):
    frames_dir = os.path.join(get_prints_dir(), print_id, "frames")
    if not os.path.exists(frames_dir):
        raise HTTPException(404, "No frames")
    frames = sorted([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
    if not frames:
        raise HTTPException(404, "No frames yet")
    return FileResponse(
        os.path.join(frames_dir, frames[-1]),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/prints/{print_id}/timelapse")
async def get_timelapse(print_id: str):
    path = os.path.join(get_prints_dir(), print_id, "timelapse.mp4")
    if not os.path.exists(path):
        raise HTTPException(404, "Timelapse not found")
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/test-capture")
async def test_capture():
    cfg = config_manager.config
    if not cfg.camera.rtsp_url:
        raise HTTPException(400, "No RTSP URL configured")
    test_path = "/tmp/bambu_test_capture.jpg"
    success = await capture_frame(cfg.camera.rtsp_url, test_path)
    if not success:
        raise HTTPException(500, "Capture failed — check RTSP URL and camera connectivity")
    return FileResponse(
        test_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.post("/api/manual-capture")
async def manual_capture():
    cfg = config_manager.config
    if not cfg.camera.rtsp_url:
        raise HTTPException(400, "No RTSP URL configured")
    if not current_print:
        raise HTTPException(400, "No active print session")

    frames_dir = os.path.join(get_prints_dir(), current_print.id, "frames")
    existing = [f for f in os.listdir(frames_dir) if f.startswith("manual_")]
    frame_path = os.path.join(frames_dir, f"manual_{len(existing):04d}.jpg")

    success = await capture_frame(cfg.camera.rtsp_url, frame_path)
    if success:
        current_print.frame_count += 1
        _save_meta(current_print)
        return {
            "status": "captured",
            "frame_count": current_print.frame_count,
            "url": f"/api/prints/{current_print.id}/latest-frame",
        }
    raise HTTPException(500, "Capture failed")


# Static files — must be last
app.mount("/", StaticFiles(directory="static", html=True), name="static")
