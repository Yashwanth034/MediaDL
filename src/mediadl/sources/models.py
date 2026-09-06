"""Stable source and scan models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mediadl.core.errors import InputError


class SourceKind(StrEnum):
    VIDEO = "video"
    PLAYLIST = "playlist"
    CHANNEL = "channel"
    CHANNEL_VIDEOS = "channel_videos"
    CHANNEL_SHORTS = "channel_shorts"
    CHANNEL_STREAMS = "channel_streams"
    CHANNEL_PLAYLISTS = "channel_playlists"

    @property
    def is_collection(self) -> bool:
        return self is not SourceKind.VIDEO

    @property
    def is_channel_tab(self) -> bool:
        return self in {
            SourceKind.CHANNEL_VIDEOS,
            SourceKind.CHANNEL_SHORTS,
            SourceKind.CHANNEL_STREAMS,
        }


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    platform: str
    kind: SourceKind
    source_key: str
    url: str
    root_url: str | None = None
    title: str | None = None

    def for_tab(self, kind: SourceKind) -> SourceDescriptor:
        if self.kind is not SourceKind.CHANNEL:
            raise InputError("Only a channel root can be converted to a channel tab")
        if not kind.is_channel_tab:
            raise InputError("Requested source kind is not a channel tab")
        root = (self.root_url or self.url).rstrip("/")
        suffix = {
            SourceKind.CHANNEL_VIDEOS: "videos",
            SourceKind.CHANNEL_SHORTS: "shorts",
            SourceKind.CHANNEL_STREAMS: "streams",
        }[kind]
        return SourceDescriptor(
            platform=self.platform,
            kind=kind,
            source_key=self.source_key,
            url=f"{root}/{suffix}",
            root_url=root,
            title=self.title,
        )


@dataclass(frozen=True, slots=True)
class MediaItemStub:
    media_key: str
    title: str
    url: str
    duration_seconds: float | None = None
    upload_date: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    channel: str | None = None
    channel_id: str | None = None
    availability: str = "unknown"
    media_type: str = "video"


@dataclass(frozen=True, slots=True)
class ScanResult:
    source: SourceDescriptor
    title: str
    items: tuple[MediaItemStub, ...]
    reported_count: int | None = None
    skipped_entries: int = 0

    @property
    def item_count(self) -> int:
        return len(self.items)
