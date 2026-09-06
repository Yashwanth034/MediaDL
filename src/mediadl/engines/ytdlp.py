"""Isolated yt-dlp adapter.

Nothing outside this module should need to depend on yt-dlp's concrete API.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from mediadl.auth.options import AuthConfig
from mediadl.core.errors import DependencyError, DownloadError, InputError
from mediadl.core.logging import redact_text
from mediadl.engines.js_runtime import JavaScriptRuntime, detect_js_runtime
from mediadl.engines.network import NetworkPolicy
from mediadl.engines.ytdlp_failures import FailureCategory, FailureInfo, YtDlpFailureClassifier

try:
    from yt_dlp import YoutubeDL as _YoutubeDL
    from yt_dlp.utils import DownloadError as _YtDlpDownloadError
    from yt_dlp.version import __version__ as _YTDLP_VERSION
except ImportError:  # pragma: no cover - exercised only in broken installations
    _YoutubeDL = None
    _YTDLP_VERSION = None

    class _YtDlpDownloadError(Exception):
        pass


ProgressHook = Callable[[dict[str, Any]], None]
PostprocessorHook = Callable[[dict[str, Any]], None]
YdlFactory = Callable[[dict[str, Any]], Any]
JsRuntimeDetector = Callable[[], JavaScriptRuntime | None]
EjsAvailabilityChecker = Callable[[], bool]

_EXTRACTION_WARNING_MARKERS = (
    "no supported javascript runtime could be found",
    "challenge solver",
    "challenge solving failed",
    "signature solving failed",
    "unable to extract yt initial data",
    "incomplete data received in embedded initial data",
    "no title found in player responses",
)


class _YtDlpLogger:
    """Bridge yt-dlp logging into MediaDL and remember extraction-health warnings."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.extraction_warning = False

    def debug(self, message: str) -> None:
        self._write(logging.DEBUG, message)

    def info(self, message: str) -> None:
        self._write(logging.INFO, message)

    def warning(self, message: str) -> None:
        self._write(logging.WARNING, message)

    def error(self, message: str) -> None:
        self._write(logging.ERROR, message)

    def _write(self, level: int, message: str) -> None:
        redacted = redact_text(message)
        lower = redacted.casefold()
        if any(marker in lower for marker in _EXTRACTION_WARNING_MARKERS):
            self.extraction_warning = True
        self.logger.log(level, "yt-dlp: %s", redacted)


class YtDlpAdapter:
    """Small stable boundary around yt-dlp extraction and download calls."""

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        ydl_factory: YdlFactory | None = None,
        auth: AuthConfig | None = None,
        network_policy: NetworkPolicy | None = None,
        js_runtime_detector: JsRuntimeDetector = detect_js_runtime,
        ejs_available: EjsAvailabilityChecker | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger("mediadl.ytdlp")
        self.auth = auth or AuthConfig()
        self.network_policy = network_policy or NetworkPolicy()
        self.js_runtime = js_runtime_detector()
        self.ejs_available = ejs_available or _yt_dlp_ejs_available
        if ydl_factory is None:
            if _YoutubeDL is None:
                raise DependencyError("yt-dlp is not installed")
            ydl_factory = _YoutubeDL
        self._ydl_factory = ydl_factory

    @property
    def version(self) -> str:
        return _YTDLP_VERSION or "unavailable"

    @staticmethod
    def is_available() -> bool:
        return _YoutubeDL is not None

    def base_options(
        self,
        *,
        progress_hook: ProgressHook | None = None,
        postprocessor_hook: PostprocessorHook | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _YtDlpLogger(self.logger),
            **self.network_policy.ytdlp_options(),
            **self.auth.ytdlp_options(),
            **(self.js_runtime.ytdlp_options() if self.js_runtime is not None else {}),
        }
        if progress_hook is not None:
            options["progress_hooks"] = [progress_hook]
        if postprocessor_hook is not None:
            options["postprocessor_hooks"] = [postprocessor_hook]
        return options

    def extract_info(
        self,
        url: str,
        *,
        flat: bool = False,
        playlist_items: str | None = None,
        extra_options: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Extract metadata without downloading media bytes."""

        self._validate_url(url)
        self._validate_youtube_support(url)
        options = self.base_options()
        options.update(
            {
                "skip_download": True,
                "extract_flat": "in_playlist" if flat else False,
            }
        )
        if playlist_items:
            options["playlist_items"] = playlist_items
        self._merge_extra_options(options, extra_options)
        return self._execute(url, options=options, download=False)

    def download(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
        progress_hook: ProgressHook | None = None,
        postprocessor_hook: PostprocessorHook | None = None,
    ) -> Mapping[str, Any]:
        """Execute one yt-dlp download with caller-provided typed policy options."""

        self._validate_url(url)
        self._validate_youtube_support(url)
        merged = self.base_options(
            progress_hook=progress_hook,
            postprocessor_hook=postprocessor_hook,
        )
        self._merge_extra_options(merged, options)
        return self._execute(url, options=merged, download=True)

    def _execute(
        self,
        url: str,
        *,
        options: dict[str, Any],
        download: bool,
    ) -> Mapping[str, Any]:
        try:
            with self._ydl_factory(options) as ydl:
                result = ydl.extract_info(url, download=download)
        except _YtDlpDownloadError as exc:
            raw_message = redact_text(exc)
            failure = YtDlpFailureClassifier.classify(raw_message)
            bridge = options.get("logger")
            if (
                failure.category is FailureCategory.UNAVAILABLE
                and isinstance(bridge, _YtDlpLogger)
                and bridge.extraction_warning
            ):
                failure = FailureInfo(
                    FailureCategory.EXTRACTOR,
                    False,
                    "YouTube extraction failed before availability could be determined.",
                )
            self.logger.debug(
                "yt-dlp failure classified as %s (retryable=%s): %s",
                failure.category.value,
                failure.retryable,
                raw_message,
            )
            raise DownloadError(
                failure.user_message,
                retryable=failure.retryable,
                category=failure.category.value,
            ) from exc
        except OSError as exc:
            raise DownloadError(
                f"yt-dlp could not access a required local file: {exc}",
                category="local_io",
            ) from exc

        if not isinstance(result, Mapping):
            raise DownloadError("yt-dlp returned no usable metadata")
        return result

    def _validate_youtube_support(self, url: str) -> None:
        if not _is_youtube_url(url):
            return
        if self.js_runtime is None:
            raise DependencyError(
                "YouTube extraction requires a supported JavaScript runtime: "
                "Deno >=2.3, Node >=22, QuickJS >=2023-12-9, or supported Bun. "
                "Run 'mdl doctor' for the detected runtime state."
            )
        if not self.ejs_available():
            raise DependencyError(
                "YouTube EJS challenge-solver support is missing from this MediaDL installation. "
                "Update or reinstall MediaDL before downloading YouTube media."
            )

    @staticmethod
    def _validate_url(url: str) -> None:
        if not isinstance(url, str) or not url.strip():
            raise InputError("A non-empty media URL is required")

    @staticmethod
    def _merge_extra_options(
        target: dict[str, Any],
        extra: Mapping[str, Any] | None,
    ) -> None:
        if extra is None:
            return
        protected = {
            "logger",
            "progress_hooks",
            "postprocessor_hooks",
            "cookiefile",
            "cookiesfrombrowser",
            "socket_timeout",
            "retries",
            "fragment_retries",
            "extractor_retries",
            "file_access_retries",
            "concurrent_fragment_downloads",
            "sleep_interval_requests",
            "ratelimit",
            "js_runtimes",
        }
        attempted = protected.intersection(extra)
        if attempted:
            names = ", ".join(sorted(attempted))
            raise InputError(f"Internal yt-dlp options cannot be overridden: {names}")
        target.update(dict(extra))


def _yt_dlp_ejs_available() -> bool:
    return importlib.util.find_spec("yt_dlp_ejs") is not None


def _is_youtube_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"} or host.endswith(
        ".youtube.com"
    )
