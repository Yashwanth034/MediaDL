from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import mediadl.cli.app as cli_app
from mediadl.core.config import AppConfig
from mediadl.core.errors import DependencyError, InputError
from mediadl.core.formats import OutputFormat
from mediadl.downloads.single import (
    DownloadReceipt,
    SingleDownloadRequest,
    SingleDownloadService,
    _find_output_path,
)


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    def download(
        self,
        url: str,
        *,
        options: dict[str, Any],
        progress_hook: Any = None,
    ) -> dict[str, Any]:
        self.calls.append((url, options, progress_hook))
        return {"id": "abc123", "title": "Example Video"}


def found_dependency(_: str) -> str:
    return "/usr/bin/ffmpeg"


def test_mp4_single_download_builds_quality_policy(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    service = SingleDownloadService(adapter, dependency_finder=found_dependency)  # type: ignore[arg-type]
    request = SingleDownloadRequest(
        url="https://example.invalid/watch?v=abc123",
        output_dir=tmp_path / "out",
        output_format=OutputFormat.MP4,
        quality="1080p",
    )

    receipt = service.download(request)

    assert receipt.media_id == "abc123"
    assert receipt.title == "Example Video"
    assert receipt.quality == "1080p"
    assert request.output_dir.is_dir()
    _, options, _ = adapter.calls[-1]
    assert options["noplaylist"] is True
    assert options["continuedl"] is True
    assert options["merge_output_format"] == "mp4"
    assert "[height<=1080]" in options["format"]
    assert "%(title).160s" in options["outtmpl"]
    assert "[%(id)s].%(ext)s" in options["outtmpl"]


def test_final_output_resolver_prefers_requested_extension_and_ignores_sidecars(
    tmp_path: Path,
) -> None:
    mp4 = tmp_path / "Example [abc123].mp4"
    webm = tmp_path / "Example [abc123].webm"
    part = tmp_path / "Example [abc123].mp4.part"
    thumbnail = tmp_path / "Example [abc123].webp"
    for path, payload in (
        (mp4, b"mp4"),
        (webm, b"webm"),
        (part, b"partial"),
        (thumbnail, b"image"),
    ):
        path.write_bytes(payload)

    assert _find_output_path(tmp_path, "abc123", OutputFormat.MP4) == mp4
    assert _find_output_path(tmp_path, "abc123", OutputFormat.WEBM) == webm
    assert _find_output_path(tmp_path, "missing", OutputFormat.MP4) is None


def test_mp3_single_download_uses_requested_audio_quality(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    service = SingleDownloadService(adapter, dependency_finder=found_dependency)  # type: ignore[arg-type]
    request = SingleDownloadRequest(
        url="https://example.invalid/watch?v=abc123",
        output_dir=tmp_path,
        output_format=OutputFormat.MP3,
        quality="320",
    )

    receipt = service.download(request)

    _, options, _ = adapter.calls[-1]
    assert options["format"].startswith("bestaudio[protocol^=m3u8]/")
    assert options["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert options["postprocessors"][0]["preferredcodec"] == "mp3"
    assert options["postprocessors"][0]["preferredquality"] == "320"
    assert receipt.quality == "320k"


def test_staged_mp3_download_keeps_source_bytes_and_removes_ytdlp_postprocessor(
    tmp_path: Path,
) -> None:
    class StagedAdapter:
        def __init__(self) -> None:
            self.options: dict[str, Any] | None = None

        def download(
            self,
            url: str,
            *,
            options: dict[str, Any],
            progress_hook: Any = None,
        ) -> dict[str, Any]:
            self.options = options
            source_path = tmp_path / "Example [abc123].webm"
            source_path.write_bytes(b"source audio")
            return {
                "id": "abc123",
                "title": "Example Video",
                "requested_downloads": [{"filepath": str(source_path)}],
            }

    adapter = StagedAdapter()
    service = SingleDownloadService(adapter, dependency_finder=found_dependency)  # type: ignore[arg-type]
    staged = service.download_source(
        SingleDownloadRequest(
            url="https://example.invalid/watch?v=abc123",
            output_dir=tmp_path,
            output_format=OutputFormat.MP3,
            quality="320",
        )
    )

    assert staged.source_path == tmp_path / "Example [abc123].webm"
    assert staged.source_path.read_bytes() == b"source audio"
    assert staged.output_format is OutputFormat.MP3
    assert staged.quality == "320k"
    assert adapter.options is not None
    assert adapter.options["format"].startswith("bestaudio[protocol^=m3u8]/")
    assert "postprocessors" not in adapter.options


def test_all_locked_formats_are_enabled(tmp_path: Path) -> None:
    for output_format in OutputFormat:
        adapter = FakeAdapter()
        service = SingleDownloadService(adapter, dependency_finder=found_dependency)  # type: ignore[arg-type]
        receipt = service.download(
            SingleDownloadRequest(
                url="https://example.invalid/watch?v=abc123",
                output_dir=tmp_path / output_format.value,
                output_format=output_format,
                quality="best",
            )
        )
        assert receipt.output_format is output_format
        assert adapter.calls


def test_missing_ffmpeg_fails_before_engine_for_conversion(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    service = SingleDownloadService(adapter, dependency_finder=lambda _: None)  # type: ignore[arg-type]

    with pytest.raises(DependencyError, match="FFmpeg"):
        service.download(
            SingleDownloadRequest(
                url="https://example.invalid/watch?v=abc123",
                output_dir=tmp_path,
                output_format=OutputFormat.WEBM,
            )
        )

    assert adapter.calls == []


def test_original_format_requires_ffmpeg_for_modern_separate_streams(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    service = SingleDownloadService(adapter, dependency_finder=lambda _: None)  # type: ignore[arg-type]

    with pytest.raises(DependencyError, match="FFmpeg"):
        service.download(
            SingleDownloadRequest(
                url="https://example.invalid/watch?v=abc123",
                output_dir=tmp_path,
                output_format=OutputFormat.ORIGINAL,
            )
        )

    assert adapter.calls == []


def test_cli_single_download_routes_format_quality_and_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[SingleDownloadRequest] = []

    class FakeConfigStore:
        def load(self) -> AppConfig:
            return AppConfig(output_dir=str(tmp_path / "default"))

    class FakeService:
        def __init__(self, adapter: Any) -> None:
            self.adapter = adapter

        def download(
            self,
            request: SingleDownloadRequest,
            *,
            progress_hook: object | None = None,
            postprocessor_hook: object | None = None,
        ) -> DownloadReceipt:
            captured.append(request)
            return DownloadReceipt(
                media_id="abc123",
                title="CLI Example",
                source_url=request.url,
                output_format=request.output_format,
                quality=request.quality,
            )

    monkeypatch.setattr(cli_app, "ConfigStore", FakeConfigStore)
    monkeypatch.setattr(cli_app, "SingleDownloadService", FakeService)
    monkeypatch.setattr(
        cli_app,
        "configure_logging",
        lambda **_: logging.getLogger("mediadl-test"),
    )

    result = CliRunner().invoke(
        cli_app.app,
        [
            "download",
            "https://www.youtube.com/watch?v=abc123",
            "--format",
            "webm",
            "--quality",
            "720p",
            "--output",
            str(tmp_path / "chosen"),
        ],
    )

    assert result.exit_code == 0
    assert "Done" in result.stdout
    assert captured[0].output_format is OutputFormat.WEBM
    assert captured[0].quality == "720p"
    assert captured[0].output_dir == tmp_path / "chosen"


def test_cli_shortcuts_remain_simple() -> None:
    config = AppConfig(output_dir="/tmp")

    assert (
        cli_app._single_format(config, mp4=True, mp3=False, format_value=None) is OutputFormat.MP4
    )
    assert (
        cli_app._single_format(config, mp4=False, mp3=True, format_value=None) is OutputFormat.MP3
    )


def test_cli_rejects_multiple_explicit_format_choices() -> None:
    config = AppConfig(output_dir="/tmp")

    with pytest.raises(InputError, match="only one"):
        cli_app._single_format(
            config,
            mp4=True,
            mp3=False,
            format_value=OutputFormat.WEBM,
        )
