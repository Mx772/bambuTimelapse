import asyncio
import glob
import os
import logging

logger = logging.getLogger(__name__)

CRF_MAP = {"low": 28, "medium": 23, "high": 18}

# Respect FFMPEG_THREADS env var so Docker resource limits have a cooperating
# app-level cap. Defaults to 2 to avoid pegging all cores on large encodes.
_FFMPEG_THREADS = str(max(1, int(os.environ.get("FFMPEG_THREADS", "2"))))


async def generate_timelapse(
    frames_dir: str,
    output_path: str,
    fps: int = 24,
    quality: str = "high",
    timeout: int = 600,
) -> bool:
    """Generate timelapse video from frames directory using ffmpeg."""
    frames = sorted(glob.glob(os.path.join(frames_dir, "*.jpg")))
    if not frames:
        logger.error(f"No frames found in {frames_dir}")
        return False

    crf = CRF_MAP.get(quality, 23)
    logger.info(
        f"Generating timelapse from {len(frames)} frames "
        f"at {fps}fps (crf={crf}, threads={_FFMPEG_THREADS})"
    )

    cmd = [
        "ffmpeg",
        "-threads", _FFMPEG_THREADS,   # global thread limit (decode + filter)
        "-framerate", str(fps),
        "-pattern_type", "glob",
        "-i", os.path.join(frames_dir, "*.jpg"),
        "-c:v", "libx264",
        "-x264-params", f"threads={_FFMPEG_THREADS}",  # encoder thread limit
        "-pix_fmt", "yuv420p",
        "-crf", str(crf),
        "-movflags", "+faststart",
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
            size_mb = os.path.getsize(output_path) / 1_048_576
            logger.info(f"Timelapse ready: {output_path} ({size_mb:.1f} MB)")
            return True

        logger.error(f"ffmpeg failed (rc={proc.returncode}): {stderr.decode()[-500:]}")
        return False

    except asyncio.TimeoutError:
        logger.error("Timelapse generation timed out")
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return False
    except Exception as e:
        logger.error(f"Timelapse error: {e}")
        return False
