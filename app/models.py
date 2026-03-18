import os
from pydantic import BaseModel
from typing import Optional


class PrinterConfig(BaseModel):
    ip: str = ""
    serial: str = ""
    access_code: str = ""


class CameraConfig(BaseModel):
    rtsp_url: str = ""


class TimelapseConfig(BaseModel):
    fps: int = 24
    quality: str = "high"  # low, medium, high
    auto_generate: bool = True
    capture_every_n_layers: int = 1


class AppConfig(BaseModel):
    data_dir: str = "/data"
    timezone: str = os.environ.get("TZ", "America/New_York")


class Config(BaseModel):
    printer: PrinterConfig = PrinterConfig()
    camera: CameraConfig = CameraConfig()
    timelapse: TimelapseConfig = TimelapseConfig()
    app: AppConfig = AppConfig()


class PrintMeta(BaseModel):
    id: str
    label: str = ""
    file_name: str = ""       # original gcode/subtask name from printer
    start_time: str = ""
    end_time: Optional[str] = None
    total_layers: int = 0
    current_layer: int = 0
    frame_count: int = 0
    gcode_state: str = "RUNNING"
    timelapse_generated: bool = False
