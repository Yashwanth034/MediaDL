import pytest

from mediadl.core.errors import InputError
from mediadl.core.numbers import NumericPredicate, parse_between, parse_count


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("1000000", 1_000_000),
        ("1K", 1_000),
        ("500k", 500_000),
        ("10L", 1_000_000),
        ("50L", 5_000_000),
        ("1Cr", 10_000_000),
        ("2.5cr", 25_000_000),
        ("1M", 1_000_000),
        ("1B", 1_000_000_000),
        ("10 lakh", 1_000_000),
        ("1 crore", 10_000_000),
    ],
)
def test_parse_count_supports_international_and_indian_shorthand(
    text: str,
    expected: int,
) -> None:
    assert parse_count(text) == expected


def test_parse_count_is_exact_for_decimal_shorthand() -> None:
    assert parse_count("0.1L") == 10_000
    assert parse_count("0.01Cr") == 100_000


def test_parse_count_rejects_fractional_base_counts_and_invalid_forms() -> None:
    for value in ("1.5", "-1", "1e6", "1,000", "Cr", "1CC", "", "1.2345K"):
        with pytest.raises(InputError):
            parse_count(value)


def test_count_rejects_negative_boolean_and_sqlite_overflow() -> None:
    with pytest.raises(InputError):
        parse_count(-1)
    with pytest.raises(InputError):
        parse_count(True)
    with pytest.raises(InputError, match="too large"):
        parse_count("10000000000B")


def test_between_is_inclusive_and_validated() -> None:
    assert parse_between("10L:1Cr") == (1_000_000, 10_000_000)

    predicate = NumericPredicate.between("10L:1Cr")
    assert predicate.matches(1_000_000)
    assert predicate.matches(5_000_000)
    assert predicate.matches(10_000_000)
    assert not predicate.matches(999_999)
    assert not predicate.matches(10_000_001)


def test_above_and_below_are_strict() -> None:
    above = NumericPredicate.above("10L")
    below = NumericPredicate.below("1Cr")

    assert not above.matches(1_000_000)
    assert above.matches(1_000_001)
    assert below.matches(9_999_999)
    assert not below.matches(10_000_000)


def test_between_rejects_bad_ranges() -> None:
    for value in ("", ":", "10L:", ":1Cr", "1Cr:10L", "10L-1Cr", "1:2:3"):
        with pytest.raises(InputError):
            parse_between(value)
