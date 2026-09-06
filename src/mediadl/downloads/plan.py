"""Immutable download-plan snapshots used by preview and persistent jobs."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.downloads.format_policy import FormatPolicyBuilder
from mediadl.sources.models import MediaItemStub, SourceDescriptor


@dataclass(frozen=True, slots=True)
class DiskSpace:
    total: int
    used: int
    free: int


@dataclass(frozen=True, slots=True)
class PlanSource:
    platform: str
    kind: str
    source_key: str
    url: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class PlanItem:
    media_key: str
    title: str
    url: str
    duration_seconds: float | None = None
    upload_date: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    media_type: str = "video"


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    plan_id: str
    created_at: str
    content_hash: str
    source: PlanSource
    items: tuple[PlanItem, ...]
    output_format: str
    quality: str
    dedupe_mode: str
    output_dir: str
    selection_label: str
    sort_mode: str
    estimated_size_bytes: int | None
    available_disk_bytes: int | None
    disk_sufficient: bool | None

    @property
    def item_count(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["items"] = [asdict(item) for item in self.items]
        payload["source"] = asdict(self.source)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DownloadPlan:
        try:
            source_raw = payload["source"]
            items_raw = payload["items"]
            if not isinstance(source_raw, Mapping):
                raise TypeError("source must be an object")
            if not isinstance(items_raw, list):
                raise TypeError("items must be a list")
            source = PlanSource(**dict(source_raw))
            items = tuple(PlanItem(**dict(item)) for item in items_raw)
            plan = cls(
                plan_id=str(payload["plan_id"]),
                created_at=str(payload["created_at"]),
                content_hash=str(payload["content_hash"]),
                source=source,
                items=items,
                output_format=str(payload["output_format"]),
                quality=str(payload["quality"]),
                dedupe_mode=str(payload["dedupe_mode"]),
                output_dir=str(payload["output_dir"]),
                selection_label=str(payload["selection_label"]),
                sort_mode=str(payload["sort_mode"]),
                estimated_size_bytes=_optional_int(payload.get("estimated_size_bytes")),
                available_disk_bytes=_optional_int(payload.get("available_disk_bytes")),
                disk_sufficient=_optional_bool(payload.get("disk_sufficient")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"Invalid serialized download plan: {exc}") from exc
        expected_hash = _content_hash(_functional_payload(plan))
        if expected_hash != plan.content_hash:
            raise InputError("Serialized download plan content hash does not match its contents")
        return plan

    @classmethod
    def from_json(cls, value: str) -> DownloadPlan:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InputError(f"Invalid download plan JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise InputError("Download plan JSON root must be an object")
        return cls.from_dict(payload)


DiskProbe = Callable[[Path], DiskSpace]
NowProvider = Callable[[], datetime]
IdProvider = Callable[[], str]


class DownloadPlanBuilder:
    def __init__(
        self,
        *,
        disk_probe: DiskProbe | None = None,
        now_provider: NowProvider | None = None,
        id_provider: IdProvider | None = None,
    ) -> None:
        self.disk_probe = disk_probe or _probe_disk
        self.now_provider = now_provider or (lambda: datetime.now(UTC))
        self.id_provider = id_provider or (lambda: uuid4().hex)

    def build(
        self,
        *,
        source: SourceDescriptor,
        items: tuple[MediaItemStub, ...] | list[MediaItemStub],
        output_format: OutputFormat,
        quality: str,
        dedupe_mode: DedupeMode,
        output_dir: Path,
        selection_label: str,
        sort_mode: str,
        estimated_size_bytes: int | None = None,
    ) -> DownloadPlan:
        frozen_items = tuple(_plan_item(item) for item in items)
        if not frozen_items:
            raise InputError("Selection matched no media; there is nothing to download")
        media_keys = [item.media_key for item in frozen_items]
        if len(media_keys) != len(set(media_keys)):
            raise InputError("A download plan cannot contain the same media item more than once")
        if estimated_size_bytes is not None and estimated_size_bytes < 0:
            raise InputError("Estimated download size cannot be negative")
        if not selection_label.strip():
            raise InputError("Download plan selection label cannot be empty")
        if not sort_mode.strip():
            raise InputError("Download plan sort mode cannot be empty")

        policy = FormatPolicyBuilder.build(output_format, quality)
        resolved_output = output_dir.expanduser().resolve(strict=False)
        disk = self.disk_probe(_nearest_existing_path(resolved_output))
        sufficient = (
            None if estimated_size_bytes is None else estimated_size_bytes <= disk.free
        )
        created = _normalize_time(self.now_provider())
        plan_id = self.id_provider().strip()
        if not plan_id:
            raise InputError("Download plan ID provider returned an empty ID")

        source_snapshot = PlanSource(
            platform=source.platform,
            kind=source.kind.value,
            source_key=source.source_key,
            url=source.url,
            title=source.title,
        )
        functional = {
            "source": asdict(source_snapshot),
            "items": [asdict(item) for item in frozen_items],
            "output_format": output_format.value,
            "quality": policy.quality_label,
            "dedupe_mode": dedupe_mode.value,
            "output_dir": str(resolved_output),
            "selection_label": selection_label.strip(),
            "sort_mode": sort_mode.strip(),
        }
        return DownloadPlan(
            plan_id=plan_id,
            created_at=created,
            content_hash=_content_hash(functional),
            source=source_snapshot,
            items=frozen_items,
            output_format=output_format.value,
            quality=policy.quality_label,
            dedupe_mode=dedupe_mode.value,
            output_dir=str(resolved_output),
            selection_label=selection_label.strip(),
            sort_mode=sort_mode.strip(),
            estimated_size_bytes=estimated_size_bytes,
            available_disk_bytes=disk.free,
            disk_sufficient=sufficient,
        )


def _plan_item(item: MediaItemStub) -> PlanItem:
    return PlanItem(
        media_key=item.media_key,
        title=item.title,
        url=item.url,
        duration_seconds=item.duration_seconds,
        upload_date=item.upload_date,
        view_count=item.view_count,
        like_count=item.like_count,
        media_type=item.media_type,
    )


def _probe_disk(path: Path) -> DiskSpace:
    usage = shutil.disk_usage(path)
    return DiskSpace(total=usage.total, used=usage.used, free=usage.free)


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _normalize_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _functional_payload(plan: DownloadPlan) -> dict[str, Any]:
    return {
        "source": asdict(plan.source),
        "items": [asdict(item) for item in plan.items],
        "output_format": plan.output_format,
        "quality": plan.quality,
        "dedupe_mode": plan.dedupe_mode,
        "output_dir": plan.output_dir,
        "selection_label": plan.selection_label,
        "sort_mode": plan.sort_mode,
    }


def _content_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer field")
    return int(value)


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("field must be boolean or null")
    return value
