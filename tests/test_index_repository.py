from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mediadl.core.errors import InputError
from mediadl.index.repository import IndexRepository
from mediadl.sources.models import MediaItemStub, ScanResult, SourceDescriptor, SourceKind
from mediadl.storage.database import Database


@pytest.fixture
def repository(tmp_path: Path) -> IndexRepository:
    database = Database(tmp_path / "index.sqlite3")
    assert database.initialize() == 3
    return IndexRepository(database)


def descriptor(
    key: str = "@Example",
    kind: SourceKind = SourceKind.CHANNEL_VIDEOS,
    *,
    url: str | None = None,
) -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=kind,
        source_key=key,
        url=url or f"https://www.youtube.com/{key}/videos",
        root_url=f"https://www.youtube.com/{key}" if key.startswith("@") else None,
    )


def item(
    media_key: str,
    *,
    title: str | None = None,
    views: int | None = None,
    likes: int | None = None,
    comments: int | None = None,
) -> MediaItemStub:
    return MediaItemStub(
        media_key=media_key,
        title=title or f"Video {media_key}",
        url=f"https://www.youtube.com/watch?v={media_key}",
        duration_seconds=60.0,
        upload_date="20250101",
        view_count=views,
        like_count=likes,
        comment_count=comments,
        channel="Example",
        channel_id="UCexample",
        availability="public",
        media_type="video",
    )


def scan(
    source: SourceDescriptor,
    items: list[MediaItemStub],
    *,
    title: str = "Example Videos",
) -> ScanResult:
    return ScanResult(
        source=source,
        title=title,
        items=tuple(items),
        reported_count=len(items),
    )


def test_first_scan_adds_items_and_second_identical_scan_is_unchanged(
    repository: IndexRepository,
) -> None:
    source = descriptor()
    first = repository.index_scan(
        scan(source, [item("a", views=100), item("b", views=200)]),
        complete_scan=True,
    )
    second = repository.index_scan(
        scan(source, [item("a", views=100), item("b", views=200)]),
        complete_scan=True,
    )

    assert first.added_count == 2
    assert first.unchanged_count == 0
    assert second.added_count == 0
    assert second.metadata_updated_count == 0
    assert second.stats_updated_count == 0
    assert second.unchanged_count == 2
    assert second.missing_count == 0


def test_metadata_and_stats_changes_are_counted_separately(repository: IndexRepository) -> None:
    source = descriptor()
    repository.index_scan(
        scan(source, [item("a", title="Old title", views=100, likes=5, comments=2)]),
        complete_scan=True,
    )

    title_change = repository.index_scan(
        scan(source, [item("a", title="New title", views=100, likes=5, comments=2)]),
        complete_scan=True,
    )
    stats_change = repository.index_scan(
        scan(source, [item("a", title="New title", views=150, likes=6, comments=3)]),
        complete_scan=True,
    )

    assert title_change.metadata_updated_count == 1
    assert title_change.stats_updated_count == 0
    assert stats_change.metadata_updated_count == 0
    assert stats_change.stats_updated_count == 1


def test_partial_scan_does_not_mark_unseen_members_missing(repository: IndexRepository) -> None:
    source = descriptor()
    repository.index_scan(
        scan(source, [item("a"), item("b"), item("c")]),
        complete_scan=True,
    )

    partial = repository.index_scan(scan(source, [item("a")]), complete_scan=False)
    cached = repository.list_source_items(source)

    assert partial.missing_count == 0
    assert [entry.media_key for entry in cached] == ["a", "b", "c"]


def test_complete_scan_marks_removed_membership_without_deleting_media(
    repository: IndexRepository,
) -> None:
    source = descriptor()
    repository.index_scan(
        scan(source, [item("a"), item("b"), item("c")]),
        complete_scan=True,
    )

    complete = repository.index_scan(scan(source, [item("a"), item("c")]), complete_scan=True)
    cached = repository.list_source_items(source)

    assert complete.missing_count == 1
    assert [entry.media_key for entry in cached] == ["a", "c"]
    with repository.database.connection() as connection:
        total_media = connection.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        absent = connection.execute(
            "SELECT COUNT(*) FROM source_media WHERE is_present = 0"
        ).fetchone()[0]
    assert total_media == 3
    assert absent == 1


def test_same_video_in_two_sources_uses_one_media_row_and_two_memberships(
    repository: IndexRepository,
) -> None:
    channel = descriptor()
    playlist = descriptor(
        "PLexample",
        SourceKind.PLAYLIST,
        url="https://www.youtube.com/playlist?list=PLexample",
    )
    shared = item("shared")

    repository.index_scan(scan(channel, [shared]), complete_scan=True)
    repository.index_scan(scan(playlist, [shared], title="Playlist"), complete_scan=True)

    with repository.database.connection() as connection:
        media_count = connection.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        membership_count = connection.execute("SELECT COUNT(*) FROM source_media").fetchone()[0]
    assert media_count == 1
    assert membership_count == 2


def test_list_source_items_preserves_collection_position(repository: IndexRepository) -> None:
    source = descriptor()
    repository.index_scan(
        scan(source, [item("c"), item("a"), item("b")]),
        complete_scan=True,
    )

    cached = repository.list_source_items(source)

    assert [entry.media_key for entry in cached] == ["c", "a", "b"]


def test_metadata_refresh_cache_respects_ttl(repository: IndexRepository) -> None:
    source = descriptor()
    repository.index_scan(scan(source, [item("a")]), complete_scan=True)
    now = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)

    assert repository.needs_refresh("a", "statistics", now=now)
    repository.mark_refresh(
        "a",
        "statistics",
        ttl_seconds=3600,
        fetched_at=now,
    )

    assert not repository.needs_refresh(
        "a",
        "statistics",
        now=now + timedelta(minutes=59),
    )
    assert repository.needs_refresh(
        "a",
        "statistics",
        now=now + timedelta(hours=1),
    )


def test_failed_refresh_is_immediately_due(repository: IndexRepository) -> None:
    source = descriptor()
    repository.index_scan(scan(source, [item("a")]), complete_scan=True)
    now = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)

    repository.mark_refresh(
        "a",
        "statistics",
        ttl_seconds=3600,
        fetched_at=now,
        status="failed",
        error_message="temporary failure",
    )

    assert repository.needs_refresh("a", "statistics", now=now + timedelta(seconds=1))


def test_refresh_rejects_unknown_media_and_invalid_inputs(repository: IndexRepository) -> None:
    with pytest.raises(InputError, match="unknown media"):
        repository.mark_refresh("missing", "statistics", ttl_seconds=60)
    with pytest.raises(InputError, match="cannot be negative"):
        repository.mark_refresh("missing", "statistics", ttl_seconds=-1)
    with pytest.raises(InputError, match="cannot be empty"):
        repository.needs_refresh("missing", "   ")
