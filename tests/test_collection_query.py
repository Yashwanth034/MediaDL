import pytest

from mediadl.cli.collection import CollectionQueryInput, compile_collection_query
from mediadl.core.errors import InputError
from mediadl.index.enrichment import MetadataEnricher, MetadataField
from mediadl.selection.filters import SortMode
from mediadl.sources.models import MediaItemStub


def media(
    key: str,
    *,
    views: int | None = None,
    likes: int | None = None,
    uploaded: str | None = None,
    duration: float | None = None,
) -> MediaItemStub:
    return MediaItemStub(
        media_key=key,
        title=f"Video {key}",
        url=f"https://www.youtube.com/watch?v={key}",
        view_count=views,
        like_count=likes,
        upload_date=uploaded,
        duration_seconds=duration,
    )


def keys(items: tuple[MediaItemStub, ...]) -> list[str]:
    return [item.media_key for item in items]


def test_default_collection_query_is_all_source_order() -> None:
    query = compile_collection_query(CollectionQueryInput())
    items = [media("a"), media("b"), media("c")]

    assert query.selection_label == "All"
    assert query.sort_mode is SortMode.SOURCE
    assert query.required_metadata == frozenset()
    assert keys(query.apply(items)) == ["a", "b", "c"]


def test_most_viewed_shortcut_implies_deep_views_sort_and_limit() -> None:
    query = compile_collection_query(CollectionQueryInput(most_viewed=2))
    items = [media("a", views=10), media("b", views=30), media("c", views=20)]

    assert query.selection_label == "Most viewed 2"
    assert query.sort_mode is SortMode.MOST_VIEWED
    assert query.required_metadata == frozenset({MetadataField.VIEWS})
    assert keys(query.apply(items)) == ["b", "c"]


def test_above_below_can_combine_into_open_interval() -> None:
    query = compile_collection_query(CollectionQueryInput(views_above="10L", views_below="1Cr"))
    items = [
        media("below", views=999_999),
        media("edge-low", views=1_000_000),
        media("inside", views=5_000_000),
        media("edge-high", views=10_000_000),
    ]

    assert keys(query.apply(items)) == ["inside"]


def test_between_is_inclusive_and_cannot_mix_with_other_threshold_flags() -> None:
    query = compile_collection_query(CollectionQueryInput(likes_between="1L:5L"))
    items = [
        media("a", likes=100_000),
        media("b", likes=500_000),
        media("c", likes=500_001),
    ]
    assert keys(query.apply(items)) == ["a", "b"]

    with pytest.raises(InputError, match="either"):
        compile_collection_query(CollectionQueryInput(likes_between="1L:5L", likes_above="2L"))


def test_latest_and_oldest_shortcuts_imply_date_metadata() -> None:
    latest = compile_collection_query(CollectionQueryInput(latest=2))
    oldest = compile_collection_query(CollectionQueryInput(oldest=2))
    items = [
        media("mid", uploaded="20250102"),
        media("new", uploaded="20250201"),
        media("old", uploaded="20240101"),
    ]

    assert latest.required_metadata == frozenset({MetadataField.UPLOAD_DATE})
    assert keys(latest.apply(items)) == ["new", "mid"]
    assert keys(oldest.apply(items)) == ["old", "mid"]


def test_compound_filter_requires_all_relevant_metadata() -> None:
    query = compile_collection_query(
        CollectionQueryInput(
            views_above="10L",
            likes_above="1L",
            after="2025-01-01",
            duration_min="5m",
        )
    )
    assert query.required_metadata == frozenset(
        {
            MetadataField.VIEWS,
            MetadataField.LIKES,
            MetadataField.UPLOAD_DATE,
            MetadataField.DURATION,
        }
    )


def test_conflicting_selectors_and_sort_are_rejected() -> None:
    with pytest.raises(InputError, match="only one"):
        compile_collection_query(CollectionQueryInput(first=10, latest=10))
    with pytest.raises(InputError, match="conflicting"):
        compile_collection_query(CollectionQueryInput(latest=10, sort_mode=SortMode.MOST_VIEWED))


def test_invalid_open_interval_is_rejected() -> None:
    with pytest.raises(InputError, match="below upper"):
        compile_collection_query(CollectionQueryInput(views_above="1Cr", views_below="10L"))


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def extract_info(self, url: str) -> dict[str, object]:
        self.calls.append(url)
        key = url.rsplit("=", 1)[-1]
        return {
            "id": key,
            "title": f"Enriched {key}",
            "view_count": 2_000_000,
            "like_count": 200_000,
            "upload_date": "20250115",
            "duration": 600,
            "channel": "Example Channel",
            "availability": "public",
        }


def test_metadata_enricher_fetches_only_items_missing_required_fields() -> None:
    adapter = FakeAdapter()
    enricher = MetadataEnricher(adapter)  # type: ignore[arg-type]
    items = [
        media("complete", views=5, likes=2),
        media("missing", views=None, likes=2),
    ]

    result = enricher.enrich(items, {MetadataField.VIEWS})

    assert adapter.calls == ["https://www.youtube.com/watch?v=missing"]
    assert result[0] == items[0]
    assert result[1].view_count == 2_000_000
    assert result[1].title == "Enriched missing"


def test_metadata_enricher_does_nothing_when_no_fields_required() -> None:
    adapter = FakeAdapter()
    items = [media("a"), media("b")]

    result = MetadataEnricher(adapter).enrich(items, set())  # type: ignore[arg-type]

    assert result == tuple(items)
    assert adapter.calls == []


def test_metadata_enricher_parallelism_preserves_source_order() -> None:
    adapter = FakeAdapter()
    items = [media(str(index), views=None) for index in range(8)]

    result = MetadataEnricher(adapter).enrich(  # type: ignore[arg-type]
        items,
        {MetadataField.VIEWS},
        parallelism=4,
    )

    assert keys(result) == [str(index) for index in range(8)]
    assert all(item.view_count == 2_000_000 for item in result)
    assert set(adapter.calls) == {item.url for item in items}


def test_channel_and_availability_where_filters_trigger_sparse_metadata_enrichment() -> None:
    adapter = FakeAdapter()
    query = compile_collection_query(
        CollectionQueryInput(where=("channel~example", "availability=public"))
    )
    item = MediaItemStub(
        media_key="sparse",
        title="Sparse playlist entry",
        url="https://www.youtube.com/watch?v=sparse",
        channel=None,
        availability="unknown",
    )

    enriched = MetadataEnricher(adapter).enrich(  # type: ignore[arg-type]
        [item],
        set(query.required_metadata),
    )

    assert query.required_metadata == frozenset({MetadataField.CHANNEL, MetadataField.AVAILABILITY})
    assert adapter.calls == ["https://www.youtube.com/watch?v=sparse"]
    assert enriched[0].channel == "Example Channel"
    assert enriched[0].availability == "public"
    assert keys(query.apply(enriched)) == ["sparse"]


def test_repeatable_where_filters_accept_user_defined_values_across_metadata_types() -> None:
    query = compile_collection_query(
        CollectionQueryInput(
            where=(
                "views>=12.5L",
                "likes<3L",
                "duration>=4.5m",
                "date>=2025-01-01",
                "title~video",
            )
        )
    )
    items = [
        MediaItemStub(
            media_key="match",
            title="Useful Video",
            url="https://www.youtube.com/watch?v=match",
            view_count=1_250_000,
            like_count=299_999,
            duration_seconds=270,
            upload_date="20250101",
        ),
        MediaItemStub(
            media_key="lowviews",
            title="Useful Video",
            url="https://www.youtube.com/watch?v=lowviews",
            view_count=1_249_999,
            like_count=100_000,
            duration_seconds=300,
            upload_date="20250201",
        ),
    ]

    assert query.required_metadata == frozenset(
        {
            MetadataField.VIEWS,
            MetadataField.LIKES,
            MetadataField.DURATION,
            MetadataField.UPLOAD_DATE,
        }
    )
    assert keys(query.apply(items)) == ["match"]
    assert query.scan_limit is None


def test_where_type_and_negated_text_filters_do_not_require_deep_metadata() -> None:
    query = compile_collection_query(CollectionQueryInput(where=("type=short", "title!~trailer")))
    items = [
        MediaItemStub(
            media_key="a",
            title="Quick tip",
            url="https://www.youtube.com/watch?v=a",
            media_type="short",
        ),
        MediaItemStub(
            media_key="b",
            title="Quick trailer",
            url="https://www.youtube.com/watch?v=b",
            media_type="short",
        ),
        MediaItemStub(
            media_key="c",
            title="Long video",
            url="https://www.youtube.com/watch?v=c",
            media_type="video",
        ),
    ]

    assert query.required_metadata == frozenset()
    assert keys(query.apply(items)) == ["a"]


def test_comments_are_not_a_user_facing_generic_filter() -> None:
    with pytest.raises(InputError, match="Unknown --where field"):
        compile_collection_query(CollectionQueryInput(where=("comments>=3K",)))


def test_invalid_where_field_or_operator_is_rejected() -> None:
    with pytest.raises(InputError, match="Unknown --where field"):
        compile_collection_query(CollectionQueryInput(where=("subscribers>1M",)))
    with pytest.raises(InputError, match="supports"):
        compile_collection_query(CollectionQueryInput(where=("duration~5m",)))
