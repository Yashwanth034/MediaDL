from __future__ import annotations

from typing import Any

import pytest

from mediadl.core.errors import DownloadError, InputError
from mediadl.sources.models import SourceDescriptor, SourceKind
from mediadl.sources.scanner import CollectionScanner


class FakeAdapter:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def extract_info(
        self,
        url: str,
        *,
        flat: bool = False,
        playlist_items: str | None = None,
        extra_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "url": url,
                "flat": flat,
                "playlist_items": playlist_items,
                "extra_options": extra_options,
            }
        )
        return self.response


def source(kind: SourceKind = SourceKind.CHANNEL_VIDEOS) -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=kind,
        source_key="@Example",
        url="https://www.youtube.com/@Example/videos",
        root_url="https://www.youtube.com/@Example",
    )


def test_scanner_requests_flat_metadata_and_optional_limit() -> None:
    adapter = FakeAdapter({"title": "Example Videos", "entries": []})
    scanner = CollectionScanner(adapter)  # type: ignore[arg-type]

    result = scanner.scan(source(), max_items=25)

    assert result.item_count == 0
    assert adapter.calls == [
        {
            "url": "https://www.youtube.com/@Example/videos",
            "flat": True,
            "playlist_items": "1:25",
            "extra_options": None,
        }
    ]


def test_scanner_normalizes_sparse_flat_entries() -> None:
    adapter = FakeAdapter(
        {
            "title": "Example Videos",
            "playlist_count": 2,
            "channel": "Example Channel",
            "channel_id": "UCexample",
            "entries": [
                {
                    "id": "abc123",
                    "title": "First video",
                    "url": "abc123",
                    "duration": 61,
                    "view_count": 1_500_000,
                    "like_count": 42_000,
                    "comment_count": 3_200,
                    "upload_date": "20250102",
                    "availability": "public",
                },
                {
                    "id": "def456",
                    "title": None,
                    "webpage_url": "https://www.youtube.com/watch?v=def456",
                    "duration": None,
                    "view_count": None,
                    "availability": "private",
                },
            ],
        }
    )

    result = CollectionScanner(adapter).scan(source())  # type: ignore[arg-type]

    assert result.title == "Example Videos"
    assert result.reported_count == 2
    assert result.item_count == 2
    first, second = result.items
    assert first.media_key == "abc123"
    assert first.url == "https://www.youtube.com/watch?v=abc123"
    assert first.view_count == 1_500_000
    assert first.like_count == 42_000
    assert first.comment_count == 3_200
    assert first.channel == "Example Channel"
    assert first.channel_id == "UCexample"
    assert second.title == "Unavailable video"
    assert second.availability == "private"


def test_scanner_marks_channel_tab_media_types() -> None:
    response = {"entries": [{"id": "abc", "title": "Example"}]}

    shorts = CollectionScanner(FakeAdapter(response)).scan(  # type: ignore[arg-type]
        source(SourceKind.CHANNEL_SHORTS)
    )
    streams = CollectionScanner(FakeAdapter(response)).scan(  # type: ignore[arg-type]
        source(SourceKind.CHANNEL_STREAMS)
    )

    assert shorts.items[0].media_type == "short"
    assert streams.items[0].media_type == "stream"


def test_scanner_detects_live_entries_in_generic_playlist() -> None:
    descriptor = SourceDescriptor(
        platform="youtube",
        kind=SourceKind.PLAYLIST,
        source_key="PLexample",
        url="https://www.youtube.com/playlist?list=PLexample",
    )
    adapter = FakeAdapter(
        {"entries": [{"id": "live1", "title": "Replay", "live_status": "was_live"}]}
    )

    result = CollectionScanner(adapter).scan(descriptor)  # type: ignore[arg-type]

    assert result.items[0].media_type == "stream"


def test_scanner_skips_malformed_entries_but_keeps_collection_running() -> None:
    adapter = FakeAdapter(
        {
            "entries": [
                None,
                "bad-entry",
                {"title": "Missing ID"},
                {"id": "good", "title": "Good"},
            ]
        }
    )

    result = CollectionScanner(adapter).scan(source())  # type: ignore[arg-type]

    assert result.item_count == 1
    assert result.items[0].media_key == "good"
    assert result.skipped_entries == 3


def test_scanner_rejects_video_channel_root_and_bad_limits() -> None:
    scanner = CollectionScanner(FakeAdapter({"entries": []}))  # type: ignore[arg-type]

    with pytest.raises(InputError, match="Single videos"):
        scanner.scan(source(SourceKind.VIDEO))
    with pytest.raises(InputError, match="Choose Videos"):
        scanner.scan(source(SourceKind.CHANNEL))
    with pytest.raises(InputError, match="at least 1"):
        scanner.scan(source(), max_items=0)


def test_scanner_rejects_missing_or_invalid_entry_list() -> None:
    for response in ({}, {"entries": "not-a-list"}):
        scanner = CollectionScanner(FakeAdapter(response))  # type: ignore[arg-type]
        with pytest.raises(DownloadError):
            scanner.scan(source())
