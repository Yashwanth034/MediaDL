"""Shared user-facing policy enums."""

from enum import StrEnum


class DedupeMode(StrEnum):
    SAFE = "safe"
    AUDIO = "audio"
    OFF = "off"
