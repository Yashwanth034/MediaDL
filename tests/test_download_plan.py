from datetime import UTC, datetime
from pathlib import Path

import pytest

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.downloads.plan import (
    DiskSpace,
    DownloadPlan,
    DownloadPlanBuilder,
)
from mediadl.downloads.preview import build_preview
from mediadl.sources.models import MediaItemStub, SourceDescriptor, SourceKind


def source() -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=SourceKind.CHANNEL_VIDEOS,
        source_key="@Example",
        url="https://www.youtube.com/@Example/videos",
        root_url="https://www.youtube.com/@Example",
        title="Example Channel",
    )


def item(key: str, title: str | None = None) -> MediaItemStub:
    return MediaItemStub(
        media_key=key,
        title=title or f"Video {key}",
        url=f"https://www.youtube.com/watch?v={key}",
        duration_seconds=60,
        upload_date="20250101",
        view_count=1_000_000,
        like_count=100_000,
    )


def builder(*, free: int = 100 * 1024**3) -> DownloadPlanBuilder:
    return DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=200 * 1024**3, used=100 * 1024**3, free=free),
        now_provider=lambda: datetime(2026, 9, 5, 1, 2, 3, tzinfo=UTC),
        id_provider=lambda: "plan-fixed-id",
    )


def build_plan(tmp_path: Path, **overrides: object) -> DownloadPlan:
    values = {
        "source": source(),
        "items": [item("a"), item("b")],
        "output_format": OutputFormat.MP4,
        "quality": "1080",
        "dedupe_mode": DedupeMode.SAFE,
        "output_dir": tmp_path / "downloads" / "Example",
        "selection_label": "Views between 10L and 1Cr",
        "sort_mode": "most_viewed",
        "estimated_size_bytes": 10 * 1024**3,
    }
    values.update(overrides)
    return builder().build(**values)  # type: ignore[arg-type]


def test_plan_snapshots_items_and_normalizes_quality(tmp_path: Path) -> None:
    original = [item("a"), item("b")]
    plan = build_plan(tmp_path, items=original)
    original.append(item("c"))

    assert plan.item_count == 2
    assert [entry.media_key for entry in plan.items] == ["a", "b"]
    assert plan.quality == "1080p"
    assert plan.output_format == "mp4"
    assert plan.dedupe_mode == "safe"
    assert plan.created_at == "2026-09-05T01:02:03Z"
    assert plan.plan_id == "plan-fixed-id"


def test_equivalent_functional_plans_have_same_content_hash(tmp_path: Path) -> None:
    first = build_plan(tmp_path)
    second = DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=1, used=0, free=1),
        now_provider=lambda: datetime(2030, 1, 1, tzinfo=UTC),
        id_provider=lambda: "different-plan-id",
    ).build(
        source=source(),
        items=[item("a"), item("b")],
        output_format=OutputFormat.MP4,
        quality="1080p",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=tmp_path / "downloads" / "Example",
        selection_label="Views between 10L and 1Cr",
        sort_mode="most_viewed",
        estimated_size_bytes=None,
    )

    assert first.plan_id != second.plan_id
    assert first.created_at != second.created_at
    assert first.available_disk_bytes != second.available_disk_bytes
    assert first.content_hash == second.content_hash


def test_content_hash_changes_when_order_or_policy_changes(tmp_path: Path) -> None:
    baseline = build_plan(tmp_path)
    reordered = build_plan(tmp_path, items=[item("b"), item("a")])
    mp3 = build_plan(
        tmp_path,
        output_format=OutputFormat.MP3,
        quality="320",
        dedupe_mode=DedupeMode.AUDIO,
    )

    assert reordered.content_hash != baseline.content_hash
    assert mp3.content_hash != baseline.content_hash


def test_json_round_trip_verifies_hash(tmp_path: Path) -> None:
    plan = build_plan(tmp_path)

    restored = DownloadPlan.from_json(plan.to_json())

    assert restored == plan


def test_serialized_plan_tampering_is_detected(tmp_path: Path) -> None:
    plan = build_plan(tmp_path)
    payload = plan.to_dict()
    payload["quality"] = "720p"

    with pytest.raises(InputError, match="content hash"):
        DownloadPlan.from_dict(payload)


def test_disk_status_and_preview_warning(tmp_path: Path) -> None:
    plan = DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=20, used=15, free=5),
        now_provider=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        id_provider=lambda: "disk-test",
    ).build(
        source=source(),
        items=[item("a")],
        output_format=OutputFormat.MP4,
        quality="best",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=tmp_path / "not-created-yet",
        selection_label="All",
        sort_mode="source",
        estimated_size_bytes=10,
    )
    preview = build_preview(plan)

    assert plan.disk_sufficient is False
    assert preview.selected_count == 1
    assert preview.output_format == "MP4"
    assert preview.estimated_size == "10 B"
    assert preview.free_space == "5 B"
    assert any("exceeds" in warning for warning in preview.warnings)
    assert not (tmp_path / "not-created-yet").exists()


def test_unknown_estimate_is_explicit_in_preview(tmp_path: Path) -> None:
    plan = build_plan(tmp_path, estimated_size_bytes=None)
    preview = build_preview(plan)

    assert plan.disk_sufficient is None
    assert preview.estimated_size == "Unknown"
    assert any("unavailable" in warning for warning in preview.warnings)


def test_builder_rejects_empty_or_invalid_plan_inputs(tmp_path: Path) -> None:
    common = {
        "source": source(),
        "output_format": OutputFormat.MP4,
        "quality": "best",
        "dedupe_mode": DedupeMode.SAFE,
        "output_dir": tmp_path,
        "selection_label": "All",
        "sort_mode": "source",
    }
    with pytest.raises(InputError, match="no media"):
        builder().build(items=[], **common)  # type: ignore[arg-type]
    with pytest.raises(InputError, match="cannot be negative"):
        builder().build(items=[item("a")], estimated_size_bytes=-1, **common)  # type: ignore[arg-type]
    with pytest.raises(InputError, match="selection label"):
        builder().build(items=[item("a")], **{**common, "selection_label": "   "})  # type: ignore[arg-type]
    with pytest.raises(InputError, match="sort mode"):
        builder().build(items=[item("a")], **{**common, "sort_mode": "   "})  # type: ignore[arg-type]


def test_invalid_quality_is_rejected_before_plan_creation(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="Invalid video quality"):
        build_plan(tmp_path, quality="ultra")


def test_empty_plan_id_provider_is_rejected(tmp_path: Path) -> None:
    custom = DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=1, used=0, free=1),
        id_provider=lambda: "   ",
    )
    with pytest.raises(InputError, match="empty ID"):
        custom.build(
            source=source(),
            items=[item("a")],
            output_format=OutputFormat.MP4,
            quality="best",
            dedupe_mode=DedupeMode.SAFE,
            output_dir=tmp_path,
            selection_label="All",
            sort_mode="source",
        )
