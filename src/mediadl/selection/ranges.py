"""Deterministic all/first/last/range/latest/oldest selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from mediadl.core.errors import InputError
from mediadl.sources.models import MediaItemStub


class SelectionMode(StrEnum):
    ALL = "all"
    FIRST = "first"
    LAST = "last"
    RANGE = "range"
    LATEST = "latest"
    OLDEST = "oldest"


@dataclass(frozen=True, slots=True)
class SelectionSpec:
    mode: SelectionMode
    count: int | None = None
    start: int | None = None
    end: int | None = None

    @classmethod
    def all(cls) -> SelectionSpec:
        return cls(SelectionMode.ALL)

    @classmethod
    def first(cls, count: int) -> SelectionSpec:
        return cls(SelectionMode.FIRST, count=_positive(count, "First count"))

    @classmethod
    def last(cls, count: int) -> SelectionSpec:
        return cls(SelectionMode.LAST, count=_positive(count, "Last count"))

    @classmethod
    def latest(cls, count: int) -> SelectionSpec:
        return cls(SelectionMode.LATEST, count=_positive(count, "Latest count"))

    @classmethod
    def oldest(cls, count: int) -> SelectionSpec:
        return cls(SelectionMode.OLDEST, count=_positive(count, "Oldest count"))

    @classmethod
    def parse_range(cls, value: str) -> SelectionSpec:
        text = value.strip()
        if ":" not in text:
            raise InputError("Range must use START:END, for example 101:200")
        left, right = text.split(":", 1)
        if not left.strip() and not right.strip():
            raise InputError("Range must include a start or end position")
        start = _optional_positive(left, "Range start")
        end = _optional_positive(right, "Range end")
        if start is not None and end is not None and start > end:
            raise InputError("Range start cannot be greater than range end")
        return cls(SelectionMode.RANGE, start=start, end=end)


class SelectionEngine:
    """Apply one selection mode without mutating caller-owned media lists."""

    @staticmethod
    def select(
        items: tuple[MediaItemStub, ...] | list[MediaItemStub],
        spec: SelectionSpec,
    ) -> tuple[MediaItemStub, ...]:
        source = tuple(items)
        if spec.mode is SelectionMode.ALL:
            return source
        if spec.mode is SelectionMode.FIRST:
            assert spec.count is not None
            return source[: spec.count]
        if spec.mode is SelectionMode.LAST:
            assert spec.count is not None
            return source[-spec.count :] if source else ()
        if spec.mode is SelectionMode.RANGE:
            return _select_range(source, spec)
        if spec.mode in {SelectionMode.LATEST, SelectionMode.OLDEST}:
            assert spec.count is not None
            ordered = _date_order(source, newest=spec.mode is SelectionMode.LATEST)
            return ordered[: spec.count]
        raise InputError(f"Unsupported selection mode: {spec.mode}")


def _select_range(
    items: tuple[MediaItemStub, ...],
    spec: SelectionSpec,
) -> tuple[MediaItemStub, ...]:
    start_index = 0 if spec.start is None else spec.start - 1
    end_index = None if spec.end is None else spec.end
    return items[start_index:end_index]


def _date_order(items: tuple[MediaItemStub, ...], *, newest: bool) -> tuple[MediaItemStub, ...]:
    dated: list[tuple[date, int, MediaItemStub]] = []
    missing: list[str] = []
    for position, item in enumerate(items):
        parsed = _parse_upload_date(item.upload_date)
        if parsed is None:
            missing.append(item.media_key)
        else:
            dated.append((parsed, position, item))
    if missing:
        preview = ", ".join(missing[:3])
        suffix = "…" if len(missing) > 3 else ""
        raise InputError(
            "Latest/oldest selection requires upload dates for every candidate; "
            f"missing for {preview}{suffix}"
        )
    dated.sort(key=lambda entry: entry[0], reverse=newest)
    return tuple(entry[2] for entry in dated)


def _parse_upload_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InputError(f"Invalid upload date in indexed metadata: {value}") from exc


def _positive(value: int, label: str) -> int:
    if value < 1:
        raise InputError(f"{label} must be at least 1")
    return value


def _optional_positive(value: str, label: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise InputError(f"{label} must be a whole number") from exc
    return _positive(parsed, label)
