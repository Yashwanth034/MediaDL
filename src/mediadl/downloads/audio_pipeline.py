"""Bounded local audio conversion used by collection download pipelines."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from threading import Event

from mediadl.core.errors import DependencyError, DownloadError, InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.quality import AudioQuality
from mediadl.downloads.disk_guard import DiskSpaceGuard
from mediadl.downloads.single import DownloadReceipt, StagedDownloadReceipt

DependencyFinder = Callable[[str], str | None]
PopenFactory = Callable[..., subprocess.Popen[bytes]]

_CONVERSION_FORMATS = {OutputFormat.MP3, OutputFormat.FLAC, OutputFormat.WAV}


class AudioConversionCancelled(Exception):
    """Internal cooperative cancellation for one local FFmpeg conversion."""


class AudioConversionService:
    """Convert staged source audio with a cancellable FFmpeg subprocess.

    The command mirrors yt-dlp's FFmpegExtractAudio behavior for the
    conversion-heavy formats used by the collection pipeline. M4A and Opus do not
    come through this path, so their existing fast/native semantics stay unchanged.
    """

    def __init__(
        self,
        *,
        dependency_finder: DependencyFinder = shutil.which,
        popen_factory: PopenFactory = subprocess.Popen,
        poll_interval: float = 0.10,
    ) -> None:
        if poll_interval <= 0:
            raise InputError("Audio conversion poll interval must be positive")
        self.dependency_finder = dependency_finder
        self.popen_factory = popen_factory
        self.poll_interval = poll_interval

    def convert(
        self,
        staged: StagedDownloadReceipt,
        *,
        disk_guard: DiskSpaceGuard,
        stop_event: Event,
    ) -> DownloadReceipt:
        if staged.output_format not in _CONVERSION_FORMATS:
            raise InputError("Audio conversion pipeline only supports MP3, FLAC, or WAV")
        source_path = staged.source_path
        if not source_path.is_file():
            raise DownloadError(
                f"Staged source media disappeared before conversion: {source_path.name}",
                retryable=False,
                category="local_io",
            )
        ffmpeg = self.dependency_finder("ffmpeg")
        if ffmpeg is None:
            format_name = staged.output_format.value.upper()
            raise DependencyError(
                f"FFmpeg is required for {format_name} output but was not found"
            )
        if stop_event.is_set():
            raise AudioConversionCancelled

        quality = AudioQuality.parse(staged.quality)
        target_path = source_path.with_suffix(f".{staged.output_format.value}")
        temp_path = _temporary_output_path(target_path)
        _remove_if_exists(temp_path)
        disk_guard.ensure_space()
        command = _ffmpeg_command(
            ffmpeg,
            source_path=source_path,
            temp_path=temp_path,
            output_format=staged.output_format,
            quality=quality,
        )

        with tempfile.TemporaryFile() as stderr_file:
            try:
                process = self.popen_factory(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                )
            except OSError as exc:
                raise DownloadError(
                    f"Could not start FFmpeg audio conversion: {exc}",
                    retryable=False,
                    category="local_io",
                ) from exc

            try:
                while process.poll() is None:
                    if stop_event.is_set():
                        _stop_process(process)
                        raise AudioConversionCancelled
                    try:
                        disk_guard.ensure_space()
                    except DownloadError:
                        _stop_process(process)
                        raise
                    time.sleep(self.poll_interval)
            except BaseException:
                if process.poll() is None:
                    _stop_process(process)
                _remove_if_exists(temp_path)
                raise

            if process.returncode != 0:
                _remove_if_exists(temp_path)
                stderr_file.seek(0)
                detail = _last_nonempty_line(
                    stderr_file.read().decode("utf-8", errors="replace")
                )
                suffix = f": {detail}" if detail else ""
                raise DownloadError(
                    f"Audio conversion failed{suffix}",
                    retryable=False,
                    category="postprocessing",
                )

        if stop_event.is_set():
            _remove_if_exists(temp_path)
            raise AudioConversionCancelled
        if not temp_path.is_file():
            raise DownloadError(
                "FFmpeg finished without producing the expected audio output",
                retryable=False,
                category="postprocessing",
            )

        try:
            os.replace(temp_path, target_path)
            if source_path != target_path:
                source_path.unlink(missing_ok=True)
            _restore_output_time(target_path, staged.filetime)
        except OSError as exc:
            _remove_if_exists(temp_path)
            raise DownloadError(
                f"Could not finalize converted audio file: {exc}",
                retryable=False,
                category="local_io",
            ) from exc

        return DownloadReceipt(
            media_id=staged.media_id,
            title=staged.title,
            source_url=staged.source_url,
            output_format=staged.output_format,
            quality=staged.quality,
            output_path=target_path,
        )


def _ffmpeg_command(
    ffmpeg: str,
    *,
    source_path: Path,
    temp_path: Path,
    output_format: OutputFormat,
    quality: AudioQuality,
) -> list[str]:
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "repeat+info",
        "-i",
        str(source_path),
        "-vn",
    ]
    if output_format is OutputFormat.MP3:
        command.extend(("-acodec", "libmp3lame"))
        if quality.bitrate_kbps is None:
            command.extend(("-q:a", "0"))
        else:
            command.extend(("-b:a", f"{quality.bitrate_kbps}k"))
    elif output_format is OutputFormat.FLAC:
        command.extend(("-acodec", "flac"))
    elif output_format is not OutputFormat.WAV:
        raise InputError("Unsupported audio conversion format")
    command.extend(("-movflags", "+faststart", str(temp_path)))
    return command


def _temporary_output_path(target_path: Path) -> Path:
    return target_path.with_name(f"{target_path.stem}.mediadl-temp{target_path.suffix}")


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1.5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _remove_if_exists(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _restore_output_time(path: Path, filetime: float | None) -> None:
    if filetime is None:
        return
    with suppress(OSError):
        os.utime(path, (time.time(), filetime))


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
