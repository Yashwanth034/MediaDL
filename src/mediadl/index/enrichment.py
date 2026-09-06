"""Demand-driven metadata enrichment for collection selection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from enum import StrEnum
from typing import Any

from mediadl.engines.ytdlp import YtDlpAdapter
from mediadl.sources.models import MediaItemStub


class MetadataField(StrEnum):
    VIEWS = "views"
    LIKES = "likes"
    UPLOAD_DATE = "upload_date"
    DURATION = "duration"
    CHANNEL = "channel"
    AVAILABILITY = "availability"


ProgressCallback = Callable[[int, int, MediaItemStub], None]


class MetadataEnricher:
    """Fetch individual metadata only for items missing required fields."""

    def __init__(self, adapter: YtDlpAdapter) -> None:
        self.adapter = adapter

    def enrich(
        self,
        items: tuple[MediaItemStub, ...] | list[MediaItemStub],
        required: set[MetadataField],
        *,
        progress: ProgressCallback | None = None,
        parallelism: int = 1,
    ) -> tuple[MediaItemStub, ...]:
        source = tuple(items)
        missing_items = [item for item in source if _missing_required(item, required)]
        if not missing_items:
            return source
        if parallelism < 1:
            raise ValueError("Metadata enrichment parallelism must be at least 1")

        enriched_by_key: dict[str, MediaItemStub] = {}
        total = len(missing_items)
        if parallelism == 1 or total == 1:
            results = ((item, self.adapter.extract_info(item.url)) for item in missing_items)
            for position, (item, info) in enumerate(results, start=1):
                if progress is not None:
                    progress(position, total, item)
                enriched_by_key[item.media_key] = _merge_info(item, info)
        else:
            worker_count = min(parallelism, total)
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                infos = pool.map(self.adapter.extract_info, (item.url for item in missing_items))
                pairs = zip(missing_items, infos, strict=True)
                for position, (item, info) in enumerate(pairs, start=1):
                    if progress is not None:
                        progress(position, total, item)
                    enriched_by_key[item.media_key] = _merge_info(item, info)

        return tuple(enriched_by_key.get(item.media_key, item) for item in source)


def _missing_required(item: MediaItemStub, required: set[MetadataField]) -> bool:
    checks = {
        MetadataField.VIEWS: item.view_count is None,
        MetadataField.LIKES: item.like_count is None,
        MetadataField.UPLOAD_DATE: item.upload_date is None,
        MetadataField.DURATION: item.duration_seconds is None,
        MetadataField.CHANNEL: item.channel is None or not item.channel.strip(),
        MetadataField.AVAILABILITY: not item.availability.strip()
        or item.availability.casefold() == "unknown",
    }
    return any(checks[field] for field in required)


def _merge_info(item: MediaItemStub, info: Mapping[str, Any]) -> MediaItemStub:
    return replace(
        item,
        title=_text(info.get("title")) or item.title,
        url=_text(info.get("webpage_url")) or item.url,
        duration_seconds=_float_or_none(info.get("duration"), item.duration_seconds),
        upload_date=_text(info.get("upload_date")) or item.upload_date,
        view_count=_int_or_none(info.get("view_count"), item.view_count),
        like_count=_int_or_none(info.get("like_count"), item.like_count),
        comment_count=_int_or_none(info.get("comment_count"), item.comment_count),
        channel=_text(info.get("channel") or info.get("uploader")) or item.channel,
        channel_id=_text(info.get("channel_id") or info.get("uploader_id")) or item.channel_id,
        availability=_text(info.get("availability")) or item.availability,
    )


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: object, fallback: int | None) -> int | None:
    if value is None:
        return fallback
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def _float_or_none(value: object, fallback: float | None) -> float | None:
    if value is None:
        return fallback
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback
