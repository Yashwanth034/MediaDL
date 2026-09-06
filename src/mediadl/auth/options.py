"""Typed yt-dlp authentication options without storing credentials."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediadl.core.errors import InputError

_SUPPORTED_BROWSERS = {
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "safari",
    "vivaldi",
    "whale",
}


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Authentication source. MediaDL never stores browser passwords or raw cookies."""

    browser: str | None = None
    browser_profile: str | None = None
    browser_container: str | None = None
    cookie_file: Path | None = None

    def __post_init__(self) -> None:
        if self.browser is not None and self.cookie_file is not None:
            raise InputError("Choose browser cookies or a cookie file, not both")
        if self.browser is None and (
            self.browser_profile is not None or self.browser_container is not None
        ):
            raise InputError("Browser profile/container requires a browser cookie source")
        if self.browser is not None and self.browser.strip().lower() not in _SUPPORTED_BROWSERS:
            supported = ", ".join(sorted(_SUPPORTED_BROWSERS))
            raise InputError(f"Unsupported cookie browser. Choose one of: {supported}")
        if self.browser_profile is not None and not self.browser_profile.strip():
            raise InputError("Browser profile cannot be empty")
        if self.browser_container is not None and not self.browser_container.strip():
            raise InputError("Browser container cannot be empty")

    @property
    def enabled(self) -> bool:
        return self.browser is not None or self.cookie_file is not None

    def ytdlp_options(self) -> dict[str, Any]:
        if self.browser is not None:
            return {
                "cookiesfrombrowser": (
                    self.browser.strip().lower(),
                    self.browser_profile.strip() if self.browser_profile else None,
                    None,
                    self.browser_container.strip() if self.browser_container else None,
                )
            }
        if self.cookie_file is not None:
            path = self.cookie_file.expanduser()
            if not path.is_file():
                raise InputError(f"Cookie file does not exist or is not a file: {path}")
            return {"cookiefile": str(path.resolve(strict=True))}
        return {}
