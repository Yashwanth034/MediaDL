"""Persistent application configuration."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mediadl.core.errors import ConfigError
from mediadl.core.paths import AppPaths, get_app_paths


@dataclass(slots=True)
class AppConfig:
    output_dir: str
    default_format: str = "mp4"
    default_quality: str = "best"
    dedupe_mode: str = "safe"
    verbose: bool = False

    @classmethod
    def defaults(cls, paths: AppPaths | None = None) -> AppConfig:
        paths = paths or get_app_paths()
        return cls(output_dir=str(paths.default_output_dir))

    def validate(self) -> None:
        supported_formats = {
            "mp4",
            "webm",
            "mkv",
            "original",
            "mp3",
            "m4a",
            "opus",
            "flac",
            "wav",
        }
        if self.default_format not in supported_formats:
            raise ConfigError(f"Unsupported default format: {self.default_format}")
        if self.dedupe_mode not in {"safe", "audio", "off"}:
            raise ConfigError(f"Unsupported duplicate mode: {self.dedupe_mode}")
        if not self.output_dir.strip():
            raise ConfigError("Output directory cannot be empty")

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser()


class ConfigStore:
    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or get_app_paths()

    def load(self) -> AppConfig:
        path = self.paths.config_file
        if not path.exists():
            config = AppConfig.defaults(self.paths)
            config.validate()
            return config
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Could not read configuration: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("Configuration root must be an object")
        allowed = {"output_dir", "default_format", "default_quality", "dedupe_mode", "verbose"}
        clean = {key: value for key, value in data.items() if key in allowed}
        defaults = asdict(AppConfig.defaults(self.paths))
        defaults.update(clean)
        try:
            config = AppConfig(**defaults)
        except TypeError as exc:
            raise ConfigError(f"Invalid configuration fields: {exc}") from exc
        config.validate()
        return config

    def save(self, config: AppConfig) -> None:
        config.validate()
        self.paths.ensure_runtime_dirs()
        target = self.paths.config_file
        temp = target.with_suffix(".tmp")
        try:
            payload = json.dumps(asdict(config), indent=2, sort_keys=True) + "\n"
            temp.write_text(payload, encoding="utf-8")
            temp.replace(target)
        except OSError as exc:
            raise ConfigError(f"Could not save configuration: {exc}") from exc
