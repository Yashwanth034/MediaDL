"""Quality parsing shared by all MediaDL download flows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from mediadl.core.errors import InputError

_HEIGHT_RE = re.compile(r"^(?P<height>\d{2,5})(?:p)?$", re.IGNORECASE)
_EXACT_RE = re.compile(r"^exact:(?P<height>\d{2,5})(?:p)?$", re.IGNORECASE)
_BITRATE_RE = re.compile(r"^(?P<bitrate>\d{2,4})(?:k|kbps)?$", re.IGNORECASE)


class VideoQualityMode(StrEnum):
    BEST = "best"
    AT_MOST = "at_most"
    EXACT = "exact"
    LOWEST = "lowest"


@dataclass(frozen=True, slots=True)
class VideoQuality:
    mode: VideoQualityMode
    height: int | None = None

    @classmethod
    def parse(cls, value: str) -> VideoQuality:
        normalized = value.strip().lower()
        if normalized == "best":
            return cls(VideoQualityMode.BEST)
        if normalized in {"lowest", "worst"}:
            return cls(VideoQualityMode.LOWEST)

        exact = _EXACT_RE.fullmatch(normalized)
        if exact:
            height = _validate_height(int(exact.group("height")))
            return cls(VideoQualityMode.EXACT, height)

        limited = _HEIGHT_RE.fullmatch(normalized)
        if limited:
            height = _validate_height(int(limited.group("height")))
            return cls(VideoQualityMode.AT_MOST, height)

        raise InputError(
            "Invalid video quality. Use best, lowest, 1080/1080p, or exact:1080."
        )

    @property
    def label(self) -> str:
        if self.mode is VideoQualityMode.BEST:
            return "best"
        if self.mode is VideoQualityMode.LOWEST:
            return "lowest"
        if self.mode is VideoQualityMode.EXACT:
            return f"exact:{self.height}p"
        return f"{self.height}p"


@dataclass(frozen=True, slots=True)
class AudioQuality:
    bitrate_kbps: int | None = None

    @classmethod
    def parse(cls, value: str) -> AudioQuality:
        normalized = value.strip().lower()
        if normalized in {"best", "0"}:
            return cls(None)

        match = _BITRATE_RE.fullmatch(normalized)
        if not match:
            raise InputError(
                "Invalid audio quality. Use best or a bitrate such as 128, 192, 256, or 320."
            )
        bitrate = int(match.group("bitrate"))
        if not 32 <= bitrate <= 512:
            raise InputError("Audio bitrate must be between 32 and 512 kbps")
        return cls(bitrate)

    @property
    def label(self) -> str:
        return "best" if self.bitrate_kbps is None else f"{self.bitrate_kbps}k"


def _validate_height(height: int) -> int:
    if not 144 <= height <= 16384:
        raise InputError("Video height must be between 144p and 16384p")
    return height
