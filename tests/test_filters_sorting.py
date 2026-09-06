from datetime import date

import pytest

from mediadl.core.errors import InputError
from mediadl.core.numbers import NumericPredicate
from mediadl.selection.filters import (
    FilterSpec,
    MediaFilterEngine,
    MediaSortEngine,
    SortMode,
    parse_duration_seconds,
    parse_filter_date,
)
from mediadl.sources.models import MediaItemStub


def media(
    key: str,
    *,
    title: str | None = None,
    views: int | None = None,
    likes: int | None = None,
    duration: float | None = None,
    uploaded: str | None = None,
) -> MediaItemStub:
    return MediaItemStub(
        media_key=key,
        title=title or f"Video {key}",
        url=f"https://www.youtube.com/watch?v={key}",
        view_count=views,
        like_count=likes,
        duration_seconds=duration,
        upload_date=uploaded,
    )


def keys(items: tuple[MediaItemStub, ...]) -> list[str]:
    return [item.media_key for item in items]


def test_views_above_below_between_with_indian_shorthand() -> None:
    items = [
        media("low", views=500_000),
        media("ten-lakh", views=1_000_000),
        media("middle", views=5_000_000),
        media("crore", views=10_000_000),
        media("high", views=20_000_000),
    ]

    above = MediaFilterEngine.apply(items, FilterSpec(views=NumericPredicate.above("10L")))
    below = MediaFilterEngine.apply(items, FilterSpec(views=NumericPredicate.below("1Cr")))
    between = MediaFilterEngine.apply(
        items,
        FilterSpec(views=NumericPredicate.between("10L:1Cr")),
    )

    assert keys(above) == ["middle", "crore", "high"]
    assert keys(below) == ["low", "ten-lakh", "middle"]
    assert keys(between) == ["ten-lakh", "middle", "crore"]


def test_likes_filter_uses_same_threshold_engine() -> None:
    items = [
        media("a", likes=99_999),
        media("b", likes=100_000),
        media("c", likes=500_000),
    ]

    selected = MediaFilterEngine.apply(
        items,
        FilterSpec(likes=NumericPredicate.above("1L")),
    )

    assert keys(selected) == ["c"]


def test_compound_filters_use_and_semantics() -> None:
    items = [
        media(
            "keep",
            title="Linux Guide",
            views=2_000_000,
            likes=200_000,
            duration=600,
            uploaded="20250115",
        ),
        media(
            "views",
            title="Linux Guide",
            views=500_000,
            likes=200_000,
            duration=600,
            uploaded="20250115",
        ),
        media(
            "title",
            title="Windows Guide",
            views=2_000_000,
            likes=200_000,
            duration=600,
            uploaded="20250115",
        ),
        media(
            "date",
            title="Linux Guide",
            views=2_000_000,
            likes=200_000,
            duration=600,
            uploaded="20240115",
        ),
    ]
    spec = FilterSpec(
        views=NumericPredicate.above("10L"),
        likes=NumericPredicate.between("1L:5L"),
        duration_min_seconds=parse_duration_seconds("5m"),
        duration_max_seconds=parse_duration_seconds("15m"),
        uploaded_after=parse_filter_date("2025-01-01"),
        uploaded_before=parse_filter_date("2025-12-31"),
        title_contains="linux",
    )

    selected = MediaFilterEngine.apply(items, spec)

    assert keys(selected) == ["keep"]


def test_filters_exclude_items_whose_required_metadata_remains_unknown() -> None:
    cases = [
        ([media("a", views=None)], FilterSpec(views=NumericPredicate.above("10L"))),
        ([media("a", likes=None)], FilterSpec(likes=NumericPredicate.above("1L"))),
        ([media("a", duration=None)], FilterSpec(duration_min_seconds=60)),
        ([media("a", uploaded=None)], FilterSpec(uploaded_after=date(2025, 1, 1))),
    ]
    for items, spec in cases:
        assert MediaFilterEngine.apply(items, spec) == ()


def test_metric_sorting_is_stable_and_complete() -> None:
    items = [
        media("a", views=10, likes=100),
        media("b", views=30, likes=50),
        media("c", views=30, likes=200),
    ]

    assert keys(MediaSortEngine.sort(items, SortMode.MOST_VIEWED)) == ["b", "c", "a"]
    assert keys(MediaSortEngine.sort(items, SortMode.LEAST_VIEWED)) == ["a", "b", "c"]
    assert keys(MediaSortEngine.sort(items, SortMode.MOST_LIKED)) == ["c", "a", "b"]
    assert keys(MediaSortEngine.sort(items, SortMode.LEAST_LIKED)) == ["b", "a", "c"]


def test_date_duration_and_title_sort_modes() -> None:
    items = [
        media("b", title="beta", duration=120, uploaded="20250102"),
        media("a", title="Alpha", duration=60, uploaded="20240101"),
        media("c", title="charlie", duration=300, uploaded="20251231"),
    ]

    assert keys(MediaSortEngine.sort(items, SortMode.LATEST)) == ["c", "b", "a"]
    assert keys(MediaSortEngine.sort(items, SortMode.OLDEST)) == ["a", "b", "c"]
    assert keys(MediaSortEngine.sort(items, SortMode.LONGEST)) == ["c", "b", "a"]
    assert keys(MediaSortEngine.sort(items, SortMode.SHORTEST)) == ["a", "b", "c"]
    assert keys(MediaSortEngine.sort(items, SortMode.TITLE_AZ)) == ["a", "b", "c"]
    assert keys(MediaSortEngine.sort(items, SortMode.TITLE_ZA)) == ["c", "b", "a"]


def test_sorting_places_unknown_metadata_after_known_values() -> None:
    items = [
        media("unknown"),
        media("low", views=10, likes=20, duration=30, uploaded="20240101"),
        media("high", views=30, likes=40, duration=50, uploaded="20250101"),
    ]

    assert keys(MediaSortEngine.sort(items, SortMode.MOST_VIEWED)) == ["high", "low", "unknown"]
    assert keys(MediaSortEngine.sort(items, SortMode.LEAST_VIEWED)) == ["low", "high", "unknown"]
    assert keys(MediaSortEngine.sort(items, SortMode.MOST_LIKED)) == ["high", "low", "unknown"]
    assert keys(MediaSortEngine.sort(items, SortMode.LATEST)) == ["high", "low", "unknown"]
    assert keys(MediaSortEngine.sort(items, SortMode.LONGEST)) == ["high", "low", "unknown"]


def test_duration_parser_supports_common_forms() -> None:
    assert parse_duration_seconds(90) == 90
    assert parse_duration_seconds("90") == 90
    assert parse_duration_seconds("90s") == 90
    assert parse_duration_seconds("5m") == 300
    assert parse_duration_seconds("1.5h") == 5400
    assert parse_duration_seconds("05:30") == 330
    assert parse_duration_seconds("01:02:03") == 3723


def test_duration_parser_rejects_invalid_forms() -> None:
    for value in ("", "-1", "1d", "01:99", "01:60:00", "a:b", True):
        with pytest.raises(InputError):
            parse_duration_seconds(value)  # type: ignore[arg-type]


def test_filter_date_parser_supports_compact_and_iso() -> None:
    assert parse_filter_date("20250102") == date(2025, 1, 2)
    assert parse_filter_date("2025-01-02") == date(2025, 1, 2)
    with pytest.raises(InputError):
        parse_filter_date("02/01/2025")


def test_filter_spec_validates_ranges() -> None:
    with pytest.raises(InputError):
        FilterSpec(duration_min_seconds=100, duration_max_seconds=50)
    with pytest.raises(InputError):
        FilterSpec(uploaded_after=date(2025, 2, 1), uploaded_before=date(2025, 1, 1))
    with pytest.raises(InputError):
        FilterSpec(title_contains="   ")
