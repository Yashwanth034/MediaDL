"""Channel playlist discovery for the guided MediaDL workflow."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from mediadl.core.errors import DownloadError, InputError
from mediadl.engines.ytdlp import YtDlpAdapter
from mediadl.sources.models import SourceDescriptor, SourceKind


@dataclass(frozen=True, slots=True)
class PlaylistSummary:
    """One playlist exposed by a YouTube channel's Playlists tab."""

    playlist_id: str
    title: str
    url: str
    item_count: int | None = None


class ChannelPlaylistCatalog:
    """Discover playlists without downloading media bytes."""

    def __init__(self, adapter: YtDlpAdapter) -> None:
        self.adapter = adapter

    def discover(self, channel: SourceDescriptor) -> tuple[PlaylistSummary, ...]:
        if channel.kind is not SourceKind.CHANNEL:
            raise InputError("Playlist discovery requires a channel root")
        root = (channel.root_url or channel.url).rstrip("/")
        info = self.adapter.extract_info(f"{root}/playlists", flat=True)
        entries = info.get("entries")
        if entries is None:
            return ()
        if isinstance(entries, (str, bytes, Mapping)) or not isinstance(entries, Iterable):
            raise DownloadError("Channel returned an invalid playlist list")

        playlists: list[PlaylistSummary] = []
        seen_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            playlist_id = _text(entry.get("id"))
            if not playlist_id or playlist_id in seen_ids:
                continue
            seen_ids.add(playlist_id)
            title = _text(entry.get("title")) or f"Playlist {playlist_id}"
            url = _playlist_url(entry, playlist_id)
            playlists.append(
                PlaylistSummary(
                    playlist_id=playlist_id,
                    title=title,
                    url=url,
                    item_count=_nonnegative_int(
                        entry.get("playlist_count") or entry.get("n_entries")
                    ),
                )
            )
        return tuple(playlists)

    def with_item_count(self, playlist: PlaylistSummary) -> PlaylistSummary:
        """Fill one selected playlist's item count with one cheap metadata probe."""

        if playlist.item_count is not None:
            return playlist
        info = self.adapter.extract_info(playlist.url, flat=True, playlist_items="1:1")
        count = _nonnegative_int(info.get("playlist_count") or info.get("n_entries"))
        if count is None:
            return playlist
        return PlaylistSummary(
            playlist_id=playlist.playlist_id,
            title=playlist.title,
            url=playlist.url,
            item_count=count,
        )


def _playlist_url(entry: Mapping[str, object], playlist_id: str) -> str:
    for key in ("webpage_url", "url"):
        value = _text(entry.get(key))
        if value and value.startswith(("https://", "http://")):
            return value
    return f"https://www.youtube.com/playlist?{urlencode({'list': playlist_id})}"


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
