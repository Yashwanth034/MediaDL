"""Compound metadata filtering and deterministic sorting."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from mediadl.core.errors import InputError
from mediadl.core.numbers import NumericPredicate
from mediadl.sources.models import MediaItemStub

_DURATION_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>s|m|h)?$", re.IGNORECASE)


class SortMode(StrEnum):
    SOURCE = "source"
    MOST_VIEWED = "most_viewed"
    LEAST_VIEWED = "least_viewed"
    MOST_LIKED = "most_liked"
    LEAST_LIKED = "least_liked"
    LATEST = "latest"
    OLDEST = "oldest"
    LONGEST = "longest"
    SHORTEST = "shortest"
    TITLE_AZ = "title_az"
    TITLE_ZA = "title_za"


@dataclass(frozen=True, slots=True)
class FilterSpec:
    views: NumericPredicate | None = None
    likes: NumericPredicate | None = None
    duration_min_seconds: float | None = None
    duration_max_seconds: float | None = None
    uploaded_after: date | None = None
    uploaded_before: date | None = None
    title_contains: str | None = None

    def __post_init__(self) -> None:
        if self.duration_min_seconds is not None and self.duration_min_seconds < 0:
            raise InputError("Minimum duration cannot be negative")
        if self.duration_max_seconds is not None and self.duration_max_seconds < 0:
            raise InputError("Maximum duration cannot be negative")
        if (
            self.duration_min_seconds is not None
            and self.duration_max_seconds is not None
            and self.duration_min_seconds > self.duration_max_seconds
        ):
            raise InputError("Minimum duration cannot be greater than maximum duration")
        if (
            self.uploaded_after is not None
            and self.uploaded_before is not None
            and self.uploaded_after > self.uploaded_before
        ):
            raise InputError("Upload start date cannot be later than upload end date")
        if self.title_contains is not None and not self.title_contains.strip():
            raise InputError("Title filter cannot be empty")


class MediaFilterEngine:
    """Apply compound AND filters without inventing values for unavailable metadata."""

    @classmethod
    def apply(
        cls,
        items: tuple[MediaItemStub, ...] | list[MediaItemStub],
        spec: FilterSpec,
    ) -> tuple[MediaItemStub, ...]:
        return tuple(item for item in items if cls._matches(item, spec))

    @staticmethod
    def _matches(item: MediaItemStub, spec: FilterSpec) -> bool:
        if spec.views is not None and (
            item.view_count is None or not spec.views.matches(item.view_count)
        ):
            return False
        if spec.likes is not None and (
            item.like_count is None or not spec.likes.matches(item.like_count)
        ):
            return False
        if spec.duration_min_seconds is not None and (
            item.duration_seconds is None or item.duration_seconds < spec.duration_min_seconds
        ):
            return False
        if spec.duration_max_seconds is not None and (
            item.duration_seconds is None or item.duration_seconds > spec.duration_max_seconds
        ):
            return False
        if spec.uploaded_after is not None and (
            item.upload_date is None
            or _parse_upload_date(item.upload_date) < spec.uploaded_after
        ):
            return False
        if spec.uploaded_before is not None and (
            item.upload_date is None
            or _parse_upload_date(item.upload_date) > spec.uploaded_before
        ):
            return False
        if spec.title_contains is not None:
            needle = spec.title_contains.strip().casefold()
            if needle not in item.title.casefold():
                return False
        return True


class MediaSortEngine:
    """Stable sort modes used by preview and download planning."""

    @staticmethod
    def sort(
        items: tuple[MediaItemStub, ...] | list[MediaItemStub],
        mode: SortMode,
    ) -> tuple[MediaItemStub, ...]:
        source = tuple(items)
        if mode is SortMode.SOURCE:
            return source
        if mode in {SortMode.MOST_VIEWED, SortMode.LEAST_VIEWED}:
            return _sort_known_last(
                source,
                getter=lambda item: item.view_count,
                reverse=mode is SortMode.MOST_VIEWED,
            )
        if mode in {SortMode.MOST_LIKED, SortMode.LEAST_LIKED}:
            return _sort_known_last(
                source,
                getter=lambda item: item.like_count,
                reverse=mode is SortMode.MOST_LIKED,
            )
        if mode in {SortMode.LATEST, SortMode.OLDEST}:
            return _sort_known_last(
                source,
                getter=lambda item: item.upload_date,
                reverse=mode is SortMode.LATEST,
                transform=_parse_upload_date,
            )
        if mode in {SortMode.LONGEST, SortMode.SHORTEST}:
            return _sort_known_last(
                source,
                getter=lambda item: item.duration_seconds,
                reverse=mode is SortMode.LONGEST,
            )
        if mode in {SortMode.TITLE_AZ, SortMode.TITLE_ZA}:
            return tuple(
                sorted(
                    source,
                    key=lambda item: item.title.casefold(),
                    reverse=mode is SortMode.TITLE_ZA,
                )
            )
        raise InputError(f"Unsupported sort mode: {mode}")


def parse_duration_seconds(value: str | int | float) -> float:
    """Parse seconds, 5m/1.5h forms, MM:SS, or HH:MM:SS."""

    if isinstance(value, bool):
        raise InputError("Duration must be a number or duration string")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise InputError("Duration cannot be negative")
        return seconds

    text = str(value).strip()
    if not text:
        raise InputError("Duration cannot be empty")
    if ":" in text:
        return _parse_colon_duration(text)

    match = _DURATION_RE.fullmatch(text)
    if match is None:
        raise InputError("Invalid duration. Use seconds, 5m, 1.5h, MM:SS, or HH:MM:SS.")
    number = float(match.group("number"))
    unit = (match.group("unit") or "s").lower()
    multiplier = {"s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    return number * multiplier


def parse_filter_date(value: str) -> date:
    text = value.strip()
    if not text:
        raise InputError("Date cannot be empty")
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InputError(f"Invalid date: {value}. Use YYYY-MM-DD or YYYYMMDD.") from exc


def _parse_upload_date(value: str | None) -> date:
    if value is None:
        raise InputError("Upload date metadata is missing")
    return parse_filter_date(value)


def _parse_colon_duration(value: str) -> float:
    parts = value.split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        raise InputError("Colon duration must use MM:SS or HH:MM:SS")
    numbers = [int(part) for part in parts]
    if numbers[-1] >= 60:
        raise InputError("Duration seconds component must be below 60")
    if len(numbers) == 2:
        minutes, seconds = numbers
        return float(minutes * 60 + seconds)
    hours, minutes, seconds = numbers
    if minutes >= 60:
        raise InputError("Duration minutes component must be below 60 in HH:MM:SS")
    return float(hours * 3600 + minutes * 60 + seconds)


def _sort_known_last(
    items: tuple[MediaItemStub, ...],
    *,
    getter: Callable[[MediaItemStub], object | None],
    reverse: bool,
    transform: Callable[[object], object] | None = None,
) -> tuple[MediaItemStub, ...]:
    known: list[tuple[MediaItemStub, object]] = []
    unknown: list[MediaItemStub] = []
    for item in items:
        value = getter(item)
        if value is None or (isinstance(value, str) and not value.strip()):
            unknown.append(item)
        else:
            known.append((item, value))

    if transform is None:
        ordered = sorted(known, key=lambda pair: pair[1], reverse=reverse)
    else:
        ordered = sorted(known, key=lambda pair: transform(pair[1]), reverse=reverse)
    return tuple(item for item, _ in ordered) + tuple(unknown)
