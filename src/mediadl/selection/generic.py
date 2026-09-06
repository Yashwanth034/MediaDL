"""User-defined metadata filter expressions for extensible collection selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from mediadl.core.errors import InputError
from mediadl.core.numbers import parse_count
from mediadl.index.enrichment import MetadataField
from mediadl.selection.filters import parse_duration_seconds, parse_filter_date
from mediadl.sources.models import MediaItemStub

_EXPR_RE = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_-]*)\s*(?P<op>>=|<=|!=|!~|=|>|<|~)\s*(?P<value>.+?)\s*$"
)


class GenericField(StrEnum):
    VIEWS = "views"
    LIKES = "likes"
    DURATION = "duration"
    UPLOAD_DATE = "date"
    TITLE = "title"
    CHANNEL = "channel"
    AVAILABILITY = "availability"
    MEDIA_TYPE = "type"


_FIELD_ALIASES = {
    "view": GenericField.VIEWS,
    "views": GenericField.VIEWS,
    "view_count": GenericField.VIEWS,
    "like": GenericField.LIKES,
    "likes": GenericField.LIKES,
    "like_count": GenericField.LIKES,
    "duration": GenericField.DURATION,
    "length": GenericField.DURATION,
    "date": GenericField.UPLOAD_DATE,
    "uploaded": GenericField.UPLOAD_DATE,
    "upload_date": GenericField.UPLOAD_DATE,
    "title": GenericField.TITLE,
    "channel": GenericField.CHANNEL,
    "uploader": GenericField.CHANNEL,
    "availability": GenericField.AVAILABILITY,
    "status": GenericField.AVAILABILITY,
    "type": GenericField.MEDIA_TYPE,
    "media_type": GenericField.MEDIA_TYPE,
}
_COMPARISON_OPS = {">", ">=", "<", "<=", "=", "!="}
_TEXT_OPS = {"=", "!=", "~", "!~"}


@dataclass(frozen=True, slots=True)
class GenericFilterClause:
    field: GenericField
    operator: str
    value: int | float | date | str

    @property
    def required_metadata(self) -> MetadataField | None:
        return {
            GenericField.VIEWS: MetadataField.VIEWS,
            GenericField.LIKES: MetadataField.LIKES,
            GenericField.DURATION: MetadataField.DURATION,
            GenericField.UPLOAD_DATE: MetadataField.UPLOAD_DATE,
            GenericField.CHANNEL: MetadataField.CHANNEL,
            GenericField.AVAILABILITY: MetadataField.AVAILABILITY,
        }.get(self.field)

    def matches(self, item: MediaItemStub) -> bool:
        actual: int | float | date | str | None
        if self.field is GenericField.VIEWS:
            actual = item.view_count
        elif self.field is GenericField.LIKES:
            actual = item.like_count
        elif self.field is GenericField.DURATION:
            actual = item.duration_seconds
        elif self.field is GenericField.UPLOAD_DATE:
            actual = parse_filter_date(item.upload_date) if item.upload_date else None
        elif self.field is GenericField.TITLE:
            actual = item.title
        elif self.field is GenericField.CHANNEL:
            actual = item.channel
        elif self.field is GenericField.AVAILABILITY:
            actual = item.availability
        else:
            actual = item.media_type

        if actual is None or (isinstance(actual, str) and not actual.strip()):
            return False
        return _compare(actual, self.operator, self.value)


def parse_generic_filter(expression: str) -> GenericFilterClause:
    match = _EXPR_RE.fullmatch(expression)
    if match is None:
        raise InputError(
            "Invalid --where expression. Use forms like views>=10L, likes<2L, "
            "duration>=5m, date>=2025-01-01, title~linux, or type=short."
        )
    raw_field = match.group("field").casefold().replace("-", "_")
    field = _FIELD_ALIASES.get(raw_field)
    if field is None:
        supported = ", ".join(field.value for field in GenericField)
        raise InputError(f"Unknown --where field: {raw_field}. Supported: {supported}")
    operator = match.group("op")
    raw_value = match.group("value").strip()
    if not raw_value:
        raise InputError("--where value cannot be empty")

    if field in {
        GenericField.TITLE,
        GenericField.CHANNEL,
        GenericField.AVAILABILITY,
        GenericField.MEDIA_TYPE,
    }:
        if operator not in _TEXT_OPS:
            raise InputError(f"Field {field.value} supports =, !=, ~, and !~")
        value: int | float | date | str = raw_value
    else:
        if operator not in _COMPARISON_OPS:
            raise InputError(f"Field {field.value} supports >, >=, <, <=, =, and !=")
        if field in {GenericField.VIEWS, GenericField.LIKES}:
            value = parse_count(raw_value)
        elif field is GenericField.DURATION:
            value = parse_duration_seconds(raw_value)
        else:
            value = parse_filter_date(raw_value)
    return GenericFilterClause(field=field, operator=operator, value=value)


def apply_generic_filters(
    items: tuple[MediaItemStub, ...] | list[MediaItemStub],
    clauses: tuple[GenericFilterClause, ...],
) -> tuple[MediaItemStub, ...]:
    if not clauses:
        return tuple(items)
    return tuple(item for item in items if all(clause.matches(item) for clause in clauses))


def _compare(actual: object, operator: str, expected: object) -> bool:
    if isinstance(actual, str) and isinstance(expected, str):
        left = actual.casefold()
        right = expected.casefold()
        if operator == "~":
            return right in left
        if operator == "!~":
            return right not in left
        if operator == "=":
            return left == right
        if operator == "!=":
            return left != right
    if operator == ">":
        return actual > expected  # type: ignore[operator]
    if operator == ">=":
        return actual >= expected  # type: ignore[operator]
    if operator == "<":
        return actual < expected  # type: ignore[operator]
    if operator == "<=":
        return actual <= expected  # type: ignore[operator]
    if operator == "=":
        return actual == expected
    if operator == "!=":
        return actual != expected
    raise InputError(f"Unsupported filter operator: {operator}")
