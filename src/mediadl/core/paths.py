"""Cross-platform application and output paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_path, user_data_path, user_log_path

APP_NAME = "MediaDL"
APP_AUTHOR = "MediaDL"


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    log_dir: Path
    default_output_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "mediadl.sqlite3"

    @property
    def archive_file(self) -> Path:
        return self.data_dir / "yt-dlp-archive.txt"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "mediadl.log"

    def ensure_runtime_dirs(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def get_app_paths() -> AppPaths:
    """Return platform-correct paths without creating them."""

    return AppPaths(
        config_dir=Path(user_config_path(APP_NAME, APP_AUTHOR)),
        data_dir=Path(user_data_path(APP_NAME, APP_AUTHOR)),
        log_dir=Path(user_log_path(APP_NAME, APP_AUTHOR)),
        default_output_dir=Path.home() / "Downloads",
    )
