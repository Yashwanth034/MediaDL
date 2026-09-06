from __future__ import annotations

from typing import Any

import pytest

from mediadl.core.errors import DownloadError, InputError
from mediadl.sources.models import SourceDescriptor, SourceKind
from mediadl.sources.playlists import ChannelPlaylistCatalog, PlaylistSummary


class FakeAdapter:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = iter(responses)
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
        return next(self.responses)


def _channel() -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=SourceKind.CHANNEL,
        source_key="@Example",
        url="https://www.youtube.com/@Example",
        root_url="https://www.youtube.com/@Example",
    )


def test_catalog_discovers_channel_playlists_and_deduplicates_ids() -> None:
    adapter = FakeAdapter(
        [
            {
                "entries": [
                    {
                        "id": "PLone",
                        "title": "First playlist",
                        "url": "https://www.youtube.com/playlist?list=PLone",
                    },
                    {
                        "id": "PLtwo",
                        "title": "Second playlist",
                    },
                    {"id": "PLone", "title": "Duplicate"},
                    None,
                ]
            }
        ]
    )

    result = ChannelPlaylistCatalog(adapter).discover(_channel())  # type: ignore[arg-type]

    assert result == (
        PlaylistSummary(
            playlist_id="PLone",
            title="First playlist",
            url="https://www.youtube.com/playlist?list=PLone",
        ),
        PlaylistSummary(
            playlist_id="PLtwo",
            title="Second playlist",
            url="https://www.youtube.com/playlist?list=PLtwo",
        ),
    )
    assert adapter.calls[0]["url"] == "https://www.youtube.com/@Example/playlists"
    assert adapter.calls[0]["flat"] is True


def test_catalog_can_fill_selected_playlist_item_count_cheaply() -> None:
    adapter = FakeAdapter([{"playlist_count": 12, "entries": [{"id": "x"}]}])
    catalog = ChannelPlaylistCatalog(adapter)  # type: ignore[arg-type]
    playlist = PlaylistSummary("PLone", "One", "https://www.youtube.com/playlist?list=PLone")

    result = catalog.with_item_count(playlist)

    assert result.item_count == 12
    assert adapter.calls[0]["playlist_items"] == "1:1"


def test_catalog_rejects_non_channel_and_invalid_entries() -> None:
    adapter = FakeAdapter([{"entries": "bad"}])
    catalog = ChannelPlaylistCatalog(adapter)  # type: ignore[arg-type]
    playlist = SourceDescriptor(
        platform="youtube",
        kind=SourceKind.PLAYLIST,
        source_key="PL",
        url="https://www.youtube.com/playlist?list=PL",
    )

    with pytest.raises(InputError, match="channel root"):
        catalog.discover(playlist)
    with pytest.raises(DownloadError, match="invalid playlist list"):
        catalog.discover(_channel())
