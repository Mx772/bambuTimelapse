import ssl
import json
import logging
import threading
import asyncio
from typing import Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

RECONNECT_DELAY = 30  # seconds


class BambuMQTTClient:
    def __init__(self, ip: str, serial: str, access_code: str):
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        self.topic_report = f"device/{serial}/report"
        self.topic_request = f"device/{serial}/request"

        self._client: Optional[mqtt.Client] = None
        self._connected = False
        self._should_reconnect = True
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event_queue: Optional[asyncio.Queue] = None
        self._reconnect_timer: Optional[threading.Timer] = None

        # Printer state
        self.current_layer: int = 0
        self.total_layers: int = 0
        self.gcode_state: str = ""
        self.mc_percent: int = 0
        self.mc_remaining_time: int = 0
        self.nozzle_temp: float = 0.0
        self.bed_temp: float = 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self, event_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._event_queue = event_queue
        self._loop = loop
        self._should_reconnect = True
        self._do_connect()

    def stop(self):
        self._should_reconnect = False
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
        if self._client:
            try:
                self._client.disconnect()
                self._client.loop_stop()
            except Exception:
                pass

    def _do_connect(self):
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

        client = mqtt.Client(client_id="bambu_timelapse", protocol=mqtt.MQTTv311)
        client.username_pw_set("bblp", self.access_code)

        # Bambu uses TLS with self-signed certificates
        client.tls_set(
            ca_certs=None,
            certfile=None,
            keyfile=None,
            cert_reqs=ssl.CERT_NONE,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        client.tls_insecure_set(True)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        self._client = client

        try:
            logger.info(f"Connecting to Bambu MQTT at {self.ip}:8883")
            client.connect_async(self.ip, 8883, keepalive=60)
            client.loop_start()
        except Exception as e:
            logger.error(f"MQTT connect failed: {e}")
            self._emit({"type": "connection", "connected": False, "error": str(e)})

    def _on_connect(self, client, userdata, flags, rc):
        RC_MESSAGES = {
            1: "Incorrect protocol version",
            2: "Invalid client ID",
            3: "Broker unavailable",
            4: "Bad username or password",
            5: "Not authorized",
        }
        if rc == 0:
            self._connected = True
            logger.info("Connected to Bambu MQTT broker")
            client.subscribe(self.topic_report)
            # Request full status dump
            client.publish(
                self.topic_request,
                json.dumps({
                    "pushing": {
                        "sequence_id": "0",
                        "command": "pushall",
                        "version": 1,
                        "push_target": 1,
                    }
                }),
            )
            self._emit({"type": "connection", "connected": True})
        else:
            msg = RC_MESSAGES.get(rc, f"Unknown error (rc={rc})")
            logger.error(f"MQTT connection refused: {msg}")
            self._emit({"type": "connection", "connected": False, "error": msg})

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        logger.info(f"MQTT disconnected (rc={rc})")
        self._emit({"type": "connection", "connected": False})

        if self._should_reconnect and rc != 0:
            logger.info(f"Scheduling MQTT reconnect in {RECONNECT_DELAY}s")
            self._reconnect_timer = threading.Timer(RECONNECT_DELAY, self._do_connect)
            self._reconnect_timer.daemon = True
            self._reconnect_timer.start()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
            self._process(payload)
        except Exception as e:
            logger.error(f"Failed to parse MQTT message: {e}")

    def _process(self, payload: dict):
        if "print" not in payload:
            return

        p = payload["print"]
        prev_layer = self.current_layer
        prev_state = self.gcode_state

        # Update state fields only if present in this message
        if "layer_num" in p:
            self.current_layer = int(p["layer_num"])
        if "total_layer_num" in p:
            self.total_layers = int(p["total_layer_num"])
        if "gcode_state" in p:
            self.gcode_state = str(p["gcode_state"])
        if "mc_percent" in p:
            self.mc_percent = int(p["mc_percent"])
        if "mc_remaining_time" in p:
            self.mc_remaining_time = int(p["mc_remaining_time"])
        if "nozzle_temper" in p:
            self.nozzle_temp = float(p["nozzle_temper"])
        if "bed_temper" in p:
            self.bed_temp = float(p["bed_temper"])

        # Always emit current status
        self._emit({
            "type": "status",
            "layer": self.current_layer,
            "total_layers": self.total_layers,
            "gcode_state": self.gcode_state,
            "mc_percent": self.mc_percent,
            "mc_remaining_time": self.mc_remaining_time,
            "nozzle_temp": self.nozzle_temp,
            "bed_temp": self.bed_temp,
        })

        # Detect new print start: transition to active state from idle/finished
        if (
            self.gcode_state in ("PREPARE", "RUNNING")
            and prev_state in ("IDLE", "FINISH", "FAILED", "")
            and self.current_layer <= 1
        ):
            self._emit({"type": "print_start"})

        # Detect layer advance while printing
        if (
            self.current_layer > prev_layer
            and self.gcode_state in ("RUNNING", "PREPARE")
            and self.current_layer > 0
        ):
            self._emit({
                "type": "layer_change",
                "layer": self.current_layer,
                "total_layers": self.total_layers,
            })

        # Detect print completion
        if self.gcode_state == "FINISH" and prev_state not in ("FINISH", "", "IDLE"):
            self._emit({"type": "print_finish"})

    def _emit(self, event: dict):
        if self._event_queue and self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._event_queue.put(event), self._loop
            )

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "layer": self.current_layer,
            "total_layers": self.total_layers,
            "gcode_state": self.gcode_state,
            "mc_percent": self.mc_percent,
            "mc_remaining_time": self.mc_remaining_time,
            "nozzle_temp": self.nozzle_temp,
            "bed_temp": self.bed_temp,
        }
