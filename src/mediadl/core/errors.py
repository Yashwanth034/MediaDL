"""Application error hierarchy and stable CLI exit codes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MediaDLError(Exception):
    """Base exception for expected, user-facing MediaDL failures."""

    message: str
    exit_code: int = 1

    def __str__(self) -> str:
        return self.message


class InputError(MediaDLError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 2)


class ConfigError(MediaDLError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 3)


class DependencyError(MediaDLError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 4)


class DatabaseError(MediaDLError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 5)


class UserCancelledError(MediaDLError):
    """User-requested cancellation with the conventional SIGINT exit code."""

    def __init__(self, message: str = "Cancelled by user.") -> None:
        super().__init__(message, 130)


class DownloadError(MediaDLError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        category: str = "download",
    ) -> None:
        super().__init__(message, 6 if retryable else 7)
        self.retryable = retryable
        self.category = category
