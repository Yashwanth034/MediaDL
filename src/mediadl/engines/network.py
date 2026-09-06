"""Typed network/retry policy shared by yt-dlp extraction and downloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mediadl.core.errors import InputError


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    socket_timeout_seconds: float = 30.0
    retries: int = 5
    fragment_retries: int = 5
    extractor_retries: int = 3
    file_access_retries: int = 3
    concurrent_fragment_downloads: int = 4
    request_sleep_seconds: float = 0.0
    rate_limit_bytes_per_second: int | None = None

    def __post_init__(self) -> None:
        if self.socket_timeout_seconds <= 0:
            raise InputError("Network socket timeout must be positive")
        for label, value in (
            ("retries", self.retries),
            ("fragment retries", self.fragment_retries),
            ("extractor retries", self.extractor_retries),
            ("file access retries", self.file_access_retries),
        ):
            if value < 0:
                raise InputError(f"Network {label} cannot be negative")
        if not 1 <= self.concurrent_fragment_downloads <= 16:
            raise InputError("Concurrent fragment downloads must be between 1 and 16")
        if self.request_sleep_seconds < 0:
            raise InputError("Request sleep cannot be negative")
        if self.rate_limit_bytes_per_second is not None and self.rate_limit_bytes_per_second < 1:
            raise InputError("Rate limit must be at least 1 byte per second")

    def ytdlp_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "socket_timeout": self.socket_timeout_seconds,
            "retries": self.retries,
            "fragment_retries": self.fragment_retries,
            "extractor_retries": self.extractor_retries,
            "file_access_retries": self.file_access_retries,
            "concurrent_fragment_downloads": self.concurrent_fragment_downloads,
        }
        if self.request_sleep_seconds:
            options["sleep_interval_requests"] = self.request_sleep_seconds
        if self.rate_limit_bytes_per_second is not None:
            options["ratelimit"] = self.rate_limit_bytes_per_second
        return options
