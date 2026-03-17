import json
import os
from .models import Config, AppConfig

DATA_DIR = os.environ.get("DATA_DIR", "./data")


class ConfigManager:
    def __init__(self):
        self._config = Config(app=AppConfig(data_dir=DATA_DIR))
        self.load()

    @property
    def config_file(self) -> str:
        return os.path.join(DATA_DIR, "config.json")

    def load(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file) as f:
                    data = json.load(f)
                self._config = Config(**data)
            except Exception as e:
                print(f"[config] Failed to load config: {e}")

    def save(self):
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, "w") as f:
            json.dump(self._config.model_dump(), f, indent=2)

    @property
    def config(self) -> Config:
        return self._config

    def update(self, new_config: Config):
        self._config = new_config
        self.save()
