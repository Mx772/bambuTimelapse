import asyncio
import os
import logging

logger = logging.getLogger(__name__)


async def capture_frame(rtsp_url: str, output_path: str, timeout: int = 30) -> bool:
    """Capture a single frame from RTSP stream using ffmpeg."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    cmd = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-q:v", "2",
        "-f", "image2",
        output_path,
        "-y",
    ]

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode == 0 and os.path.exists(output_path):
            return True
        logger.error(f"ffmpeg capture failed (rc={proc.returncode}): {stderr.decode()[-300:]}")
        return False

    except asyncio.TimeoutError:
        logger.error(f"RTSP capture timed out after {timeout}s")
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return False
    except Exception as e:
        logger.error(f"RTSP capture error: {e}")
        return False
