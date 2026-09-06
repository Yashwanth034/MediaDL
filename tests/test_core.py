import tomllib
from pathlib import Path

import pytest

from mediadl import __version__
from mediadl.core.config import AppConfig, ConfigStore
from mediadl.core.errors import ConfigError, DownloadError, InputError
from mediadl.core.logging import redact_text
from mediadl.core.paths import AppPaths


def make_paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "log",
        default_output_dir=tmp_path / "downloads",
    )


def test_config_defaults_without_creating_files(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = ConfigStore(paths).load()

    assert config.output_path == tmp_path / "downloads"
    assert config.default_format == "mp4"
    assert config.default_quality == "best"
    assert config.dedupe_mode == "safe"
    assert not paths.config_dir.exists()


def test_config_round_trip_is_atomic_and_clean(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = ConfigStore(paths)
    config = AppConfig(
        output_dir=str(tmp_path / "media"),
        default_format="mp3",
        default_quality="best",
        dedupe_mode="audio",
        verbose=True,
    )

    store.save(config)
    loaded = store.load()

    assert loaded == config
    assert paths.config_file.exists()
    assert not paths.config_file.with_suffix(".tmp").exists()


def test_config_rejects_invalid_values(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    with pytest.raises(ConfigError):
        ConfigStore(paths).save(AppConfig(output_dir="x", default_format="exe"))


def test_redaction_removes_common_secret_values() -> None:
    text = redact_text("Authorization: Bearer-secret token=abc123 password=hunter2")

    assert "Bearer-secret" not in text
    assert "abc123" not in text
    assert "hunter2" not in text
    assert "[REDACTED]" in text


def test_stable_error_exit_codes() -> None:
    assert InputError("bad").exit_code == 2
    assert DownloadError("retry", retryable=True).exit_code == 6
    assert DownloadError("final").exit_code == 7


def test_package_and_project_versions_stay_in_sync() -> None:
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))

    assert __version__ == "1.0.0"
    assert project["project"]["version"] == __version__
