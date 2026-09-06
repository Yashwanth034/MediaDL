"""Single-video download service for the simplest MediaDL workflow."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediadl.core.errors import DependencyError, DownloadError, InputError
from mediadl.core.formats import OutputFormat
from mediadl.downloads.format_policy import FormatPolicy, FormatPolicyBuilder
from mediadl.engines.ytdlp import PostprocessorHook, ProgressHook, YtDlpAdapter
from mediadl.storage.filenames import FilenamePolicy

DependencyFinder = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class SingleDownloadRequest:
    url: str
    output_dir: Path
    output_format: OutputFormat = OutputFormat.MP4
    quality: str = "best"


@dataclass(frozen=True, slots=True)
class DownloadReceipt:
    media_id: str | None
    title: str
    source_url: str
    output_format: OutputFormat
    quality: str = "best"
    output_path: Path | None = None


@dataclass(frozen=True, slots=True)
class StagedDownloadReceipt:
    """Downloaded source media waiting for bounded local audio conversion."""

    media_id: str | None
    title: str
    source_url: str
    output_format: OutputFormat
    quality: str
    source_path: Path
    filetime: float | None = None


class SingleDownloadService:
    def __init__(
        self,
        adapter: YtDlpAdapter,
        *,
        dependency_finder: DependencyFinder = shutil.which,
    ) -> None:
        self.adapter = adapter
        self.dependency_finder = dependency_finder

    def download(
        self,
        request: SingleDownloadRequest,
        *,
        progress_hook: ProgressHook | None = None,
        postprocessor_hook: PostprocessorHook | None = None,
    ) -> DownloadReceipt:
        self._validate_request(request)
        policy = FormatPolicyBuilder.build(request.output_format, request.quality)
        self._ensure_required_tools(policy)
        request.output_dir.mkdir(parents=True, exist_ok=True)

        options = self._build_options(request, policy)
        if postprocessor_hook is None:
            info = self.adapter.download(
                request.url,
                options=options,
                progress_hook=progress_hook,
            )
        else:
            info = self.adapter.download(
                request.url,
                options=options,
                progress_hook=progress_hook,
                postprocessor_hook=postprocessor_hook,
            )
        return self._receipt(request, policy, info)

    def download_source(
        self,
        request: SingleDownloadRequest,
        *,
        progress_hook: ProgressHook | None = None,
    ) -> StagedDownloadReceipt:
        """Download source audio bytes without running yt-dlp postprocessors yet.

        This is intentionally separate from ``download`` so single-video and native-audio
        paths keep their existing semantics. Collection execution uses this only for
        conversion-heavy MP3/FLAC/WAV pipelines.
        """

        self._validate_request(request)
        if request.output_format not in {OutputFormat.MP3, OutputFormat.FLAC, OutputFormat.WAV}:
            raise InputError("Staged source downloads are only valid for MP3, FLAC, or WAV")
        policy = FormatPolicyBuilder.build(request.output_format, request.quality)
        self._ensure_required_tools(policy)
        request.output_dir.mkdir(parents=True, exist_ok=True)

        options = self._build_options(request, policy)
        options.pop("postprocessors", None)
        info = self.adapter.download(
            request.url,
            options=options,
            progress_hook=progress_hook,
        )
        media_id = info.get("id")
        media_id_text = str(media_id) if media_id is not None else None
        source_path = _source_path_from_info(info, request.output_dir, media_id_text)
        if source_path is None:
            raise DownloadError(
                "Downloaded source media could not be located for audio conversion",
                retryable=False,
                category="local_io",
            )
        title = info.get("title")
        filetime = info.get("filetime")
        return StagedDownloadReceipt(
            media_id=media_id_text,
            title=str(title) if title else "Downloaded media",
            source_url=request.url,
            output_format=request.output_format,
            quality=policy.quality_label,
            source_path=source_path,
            filetime=float(filetime) if isinstance(filetime, (int, float)) else None,
        )

    @staticmethod
    def _validate_request(request: SingleDownloadRequest) -> None:
        if not request.url.strip():
            raise InputError("A non-empty media URL is required")
        if not request.quality.strip():
            raise InputError("Quality cannot be empty")

    def _ensure_required_tools(self, policy: FormatPolicy) -> None:
        if policy.requires_ffmpeg and self.dependency_finder("ffmpeg") is None:
            format_name = policy.output_format.value.upper()
            raise DependencyError(f"FFmpeg is required for {format_name} output but was not found")

    @staticmethod
    def _build_options(
        request: SingleDownloadRequest,
        policy: FormatPolicy,
    ) -> dict[str, Any]:
        naming_options = FilenamePolicy().ytdlp_options(request.output_dir)
        return {
            "noplaylist": True,
            "continuedl": True,
            "nopart": False,
            **naming_options,
            **policy.yt_dlp_options,
        }

    @staticmethod
    def _receipt(
        request: SingleDownloadRequest,
        policy: FormatPolicy,
        info: Mapping[str, Any],
    ) -> DownloadReceipt:
        media_id = info.get("id")
        media_id_text = str(media_id) if media_id is not None else None
        title = info.get("title")
        return DownloadReceipt(
            media_id=media_id_text,
            title=str(title) if title else "Downloaded media",
            source_url=request.url,
            output_format=request.output_format,
            quality=policy.quality_label,
            output_path=_find_output_path(
                request.output_dir,
                media_id_text,
                request.output_format,
            ),
        )


def _source_path_from_info(
    info: Mapping[str, Any],
    output_dir: Path,
    media_id: str | None,
) -> Path | None:
    """Prefer yt-dlp's exact downloaded filepath, then fall back to ID-based discovery."""

    candidates: list[object] = []
    requested = info.get("requested_downloads")
    if isinstance(requested, (list, tuple)):
        for entry in requested:
            if isinstance(entry, Mapping):
                candidates.extend((entry.get("filepath"), entry.get("_filename")))
    candidates.extend((info.get("filepath"), info.get("_filename")))
    for value in candidates:
        if not isinstance(value, (str, Path)):
            continue
        path = Path(value)
        if path.is_file():
            return path
    return _find_output_path(output_dir, media_id, OutputFormat.ORIGINAL)


def _find_output_path(
    output_dir: Path,
    media_id: str | None,
    output_format: OutputFormat,
) -> Path | None:
    if media_id is None or not output_dir.is_dir():
        return None
    marker = f"[{media_id}]"
    ignored_suffixes = {
        ".part",
        ".ytdl",
        ".tmp",
        ".json",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".vtt",
        ".srt",
        ".ass",
        ".lrc",
        ".description",
    }
    candidates = [
        path
        for path in output_dir.iterdir()
        if path.is_file() and marker in path.name and path.suffix.casefold() not in ignored_suffixes
    ]
    if not candidates:
        return None
    if output_format is not OutputFormat.ORIGINAL:
        expected_suffix = f".{output_format.value}".casefold()
        exact = [path for path in candidates if path.suffix.casefold() == expected_suffix]
        if exact:
            candidates = exact
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.stat().st_size))
