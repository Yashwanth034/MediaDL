"""Flat collection scanning through the isolated yt-dlp adapter."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from mediadl.core.errors import DownloadError, InputError
from mediadl.engines.ytdlp import YtDlpAdapter
from mediadl.sources.models import MediaItemStub, ScanResult, SourceDescriptor, SourceKind


class CollectionScanner:
    """Enumerate playlist/channel-tab items without downloading media bytes."""

    def __init__(self, adapter: YtDlpAdapter) -> None:
        self.adapter = adapter

    def scan(
        self,
        source: SourceDescriptor,
        *,
        max_items: int | None = None,
    ) -> ScanResult:
        if source.kind is SourceKind.VIDEO:
            raise InputError("Single videos do not need collection scanning")
        if source.kind is SourceKind.CHANNEL:
            raise InputError("Choose Videos, Shorts, or Streams before scanning a channel")
        if max_items is not None and max_items < 1:
            raise InputError("Scan item limit must be at least 1")

        playlist_items = f"1:{max_items}" if max_items is not None else None
        info = self.adapter.extract_info(
            source.url,
            flat=True,
            playlist_items=playlist_items,
        )
        entries = info.get("entries")
        if entries is None:
            raise DownloadError("Source did not return a scannable collection")
        if isinstance(entries, (str, bytes, Mapping)) or not isinstance(entries, Iterable):
            raise DownloadError("Source returned an invalid collection entry list")

        parent_channel = _optional_text(info.get("channel") or info.get("uploader"))
        parent_channel_id = _optional_text(info.get("channel_id") or info.get("uploader_id"))

        items: list[MediaItemStub] = []
        skipped = 0
        for entry in entries:
            if not isinstance(entry, Mapping):
                skipped += 1
                continue
            stub = self._entry_to_stub(
                source,
                entry,
                parent_channel=parent_channel,
                parent_channel_id=parent_channel_id,
            )
            if stub is None:
                skipped += 1
                continue
            items.append(stub)

        title = _optional_text(info.get("title")) or source.title or source.source_key
        reported_count = _first_nonnegative_int(
            info.get("playlist_count"),
            info.get("n_entries"),
        )
        return ScanResult(
            source=source,
            title=title,
            items=tuple(items),
            reported_count=reported_count,
            skipped_entries=skipped,
        )

    @staticmethod
    def _entry_to_stub(
        source: SourceDescriptor,
        entry: Mapping[str, Any],
        *,
        parent_channel: str | None,
        parent_channel_id: str | None,
    ) -> MediaItemStub | None:
        media_key = _optional_text(entry.get("id"))
        if not media_key:
            return None

        title = _optional_text(entry.get("title")) or "Unavailable video"
        url = _entry_url(entry, media_key)
        media_type = _media_type(source, entry)

        return MediaItemStub(
            media_key=media_key,
            title=title,
            url=url,
            duration_seconds=_nonnegative_float(entry.get("duration")),
            upload_date=_optional_text(entry.get("upload_date")),
            view_count=_nonnegative_int(entry.get("view_count")),
            like_count=_nonnegative_int(entry.get("like_count")),
            comment_count=_nonnegative_int(entry.get("comment_count")),
            channel=_optional_text(entry.get("channel") or entry.get("uploader")) or parent_channel,
            channel_id=_optional_text(entry.get("channel_id") or entry.get("uploader_id"))
            or parent_channel_id,
            availability=_optional_text(entry.get("availability")) or "unknown",
            media_type=media_type,
        )


def _entry_url(entry: Mapping[str, Any], media_key: str) -> str:
    for field in ("webpage_url", "url"):
        value = _optional_text(entry.get(field))
        if value and value.startswith(("https://", "http://")):
            return value
    return f"https://www.youtube.com/watch?v={media_key}"


def _media_type(source: SourceDescriptor, entry: Mapping[str, Any]) -> str:
    if source.kind is SourceKind.CHANNEL_SHORTS:
        return "short"
    if source.kind is SourceKind.CHANNEL_STREAMS:
        return "stream"
    live_status = _optional_text(entry.get("live_status"))
    if live_status in {"is_live", "was_live", "post_live"}:
        return "stream"
    return "video"


def _optional_text(value: object) -> str | None:
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


def _first_nonnegative_int(*values: object) -> int | None:
    for value in values:
        parsed = _nonnegative_int(value)
        if parsed is not None:
            return parsed
    return None


def _nonnegative_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
