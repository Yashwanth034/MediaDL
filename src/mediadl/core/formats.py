"""Stable output-format models shared by CLI and download services."""

from __future__ import annotations

from enum import StrEnum


class OutputFormat(StrEnum):
    MP4 = "mp4"
    WEBM = "webm"
    MKV = "mkv"
    ORIGINAL = "original"
    MP3 = "mp3"
    M4A = "m4a"
    OPUS = "opus"
    FLAC = "flac"
    WAV = "wav"

    @property
    def is_audio(self) -> bool:
        return self in {
            OutputFormat.MP3,
            OutputFormat.M4A,
            OutputFormat.OPUS,
            OutputFormat.FLAC,
            OutputFormat.WAV,
        }

    @property
    def is_video(self) -> bool:
        return not self.is_audio
