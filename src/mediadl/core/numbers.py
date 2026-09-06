"""Human-friendly integer count parsing for views, likes, and similar metrics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from mediadl.core.errors import InputError

_COUNT_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>k|l|m|cr|b|lakh|lac|crore)?$",
    re.IGNORECASE,
)
_MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
_SUFFIX_MULTIPLIERS = {
    "": 1,
    "k": 1_000,
    "l": 100_000,
    "lakh": 100_000,
    "lac": 100_000,
    "m": 1_000_000,
    "cr": 10_000_000,
    "crore": 10_000_000,
    "b": 1_000_000_000,
}


def parse_count(value: str | int) -> int:
    """Parse non-negative integer counts including K/L/M/Cr/B shorthand exactly."""

    if isinstance(value, bool):
        raise InputError("Count value must be a number, not a boolean")
    if isinstance(value, int):
        return _validate_integer(value)

    text = str(value).strip()
    if not text:
        raise InputError("Count value cannot be empty")

    match = _COUNT_RE.fullmatch(text)
    if match is None:
        raise InputError("Invalid count. Use values like 1000000, 10L, 1Cr, 2.5Cr, 1M, or 500K.")

    suffix = (match.group("suffix") or "").lower()
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation as exc:  # pragma: no cover - regex guards this path
        raise InputError(f"Invalid numeric count: {value}") from exc

    result = number * _SUFFIX_MULTIPLIERS[suffix]
    if result != result.to_integral_value():
        raise InputError("Count shorthand must resolve to a whole number")
    return _validate_integer(int(result))


def parse_between(value: str) -> tuple[int, int]:
    """Parse inclusive LOW:HIGH human-count range."""

    text = value.strip()
    if text.count(":") != 1:
        raise InputError("Between range must use LOW:HIGH, for example 10L:1Cr")
    low_text, high_text = text.split(":", 1)
    if not low_text.strip() or not high_text.strip():
        raise InputError("Between range requires both a lower and upper value")
    low = parse_count(low_text)
    high = parse_count(high_text)
    if low > high:
        raise InputError("Between range lower value cannot be greater than upper value")
    return low, high


@dataclass(frozen=True, slots=True)
class NumericPredicate:
    """Integer predicate with explicit boundary semantics."""

    lower: int | None = None
    upper: int | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    @classmethod
    def above(cls, value: str | int) -> NumericPredicate:
        return cls(lower=parse_count(value), lower_inclusive=False)

    @classmethod
    def below(cls, value: str | int) -> NumericPredicate:
        return cls(upper=parse_count(value), upper_inclusive=False)

    @classmethod
    def between(cls, value: str) -> NumericPredicate:
        low, high = parse_between(value)
        return cls(lower=low, upper=high, lower_inclusive=True, upper_inclusive=True)

    def matches(self, value: int) -> bool:
        if value < 0:
            return False
        if self.lower is not None:
            if self.lower_inclusive:
                if value < self.lower:
                    return False
            elif value <= self.lower:
                return False
        if self.upper is not None:
            if self.upper_inclusive:
                if value > self.upper:
                    return False
            elif value >= self.upper:
                return False
        return True


def _validate_integer(value: int) -> int:
    if value < 0:
        raise InputError("Count value cannot be negative")
    if value > _MAX_SQLITE_INTEGER:
        raise InputError("Count value is too large")
    return value
