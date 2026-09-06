import pytest

from mediadl.core.errors import InputError
from mediadl.selection.ranges import SelectionEngine, SelectionSpec
from mediadl.sources.models import MediaItemStub


def media(key: str, upload_date: str | None = None) -> MediaItemStub:
    return MediaItemStub(
        media_key=key,
        title=f"Video {key}",
        url=f"https://www.youtube.com/watch?v={key}",
        upload_date=upload_date,
    )


def keys(items: tuple[MediaItemStub, ...]) -> list[str]:
    return [item.media_key for item in items]


def test_all_first_and_last_preserve_source_order() -> None:
    items = [media("a"), media("b"), media("c"), media("d")]

    assert keys(SelectionEngine.select(items, SelectionSpec.all())) == ["a", "b", "c", "d"]
    assert keys(SelectionEngine.select(items, SelectionSpec.first(2))) == ["a", "b"]
    assert keys(SelectionEngine.select(items, SelectionSpec.last(2))) == ["c", "d"]
    assert keys(SelectionEngine.select(items, SelectionSpec.first(99))) == ["a", "b", "c", "d"]
    assert keys(SelectionEngine.select(items, SelectionSpec.last(99))) == ["a", "b", "c", "d"]


def test_range_is_one_based_and_inclusive() -> None:
    items = [media("a"), media("b"), media("c"), media("d"), media("e")]

    assert keys(SelectionEngine.select(items, SelectionSpec.parse_range("2:4"))) == ["b", "c", "d"]
    assert keys(SelectionEngine.select(items, SelectionSpec.parse_range("3:"))) == ["c", "d", "e"]
    assert keys(SelectionEngine.select(items, SelectionSpec.parse_range(":2"))) == ["a", "b"]
    assert keys(SelectionEngine.select(items, SelectionSpec.parse_range("20:30"))) == []


def test_latest_and_oldest_sort_by_actual_upload_date() -> None:
    items = [
        media("middle", "20250102"),
        media("newest", "2025-03-01"),
        media("oldest", "20240101"),
        media("newer", "20250210"),
    ]

    latest = SelectionEngine.select(items, SelectionSpec.latest(2))
    oldest = SelectionEngine.select(items, SelectionSpec.oldest(2))

    assert keys(latest) == ["newest", "newer"]
    assert keys(oldest) == ["oldest", "middle"]


def test_same_date_keeps_original_order() -> None:
    items = [media("a", "20250101"), media("b", "20250101"), media("c", "20250101")]

    assert keys(SelectionEngine.select(items, SelectionSpec.latest(3))) == ["a", "b", "c"]
    assert keys(SelectionEngine.select(items, SelectionSpec.oldest(3))) == ["a", "b", "c"]


def test_latest_oldest_refuse_to_guess_when_dates_are_missing() -> None:
    items = [media("dated", "20250101"), media("unknown")]

    with pytest.raises(InputError, match="requires upload dates"):
        SelectionEngine.select(items, SelectionSpec.latest(1))
    with pytest.raises(InputError, match="unknown"):
        SelectionEngine.select(items, SelectionSpec.oldest(1))


def test_invalid_index_specs_are_rejected() -> None:
    for factory in (
        lambda: SelectionSpec.first(0),
        lambda: SelectionSpec.last(-1),
        lambda: SelectionSpec.latest(0),
        lambda: SelectionSpec.oldest(0),
        lambda: SelectionSpec.parse_range(""),
        lambda: SelectionSpec.parse_range(":"),
        lambda: SelectionSpec.parse_range("4:2"),
        lambda: SelectionSpec.parse_range("a:2"),
        lambda: SelectionSpec.parse_range("0:2"),
    ):
        with pytest.raises(InputError):
            factory()


def test_selection_does_not_mutate_input_list() -> None:
    items = [media("a", "20250101"), media("b", "20250201")]
    original = list(items)

    SelectionEngine.select(items, SelectionSpec.latest(1))

    assert items == original
