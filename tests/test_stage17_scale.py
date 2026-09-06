from __future__ import annotations

import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

from mediadl.cli.collection import CollectionQueryInput, compile_collection_query
from mediadl.cli.workflow import CollectionWorkflow
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.dedupe.candidates import iter_candidate_pairs
from mediadl.downloads.plan import DiskSpace, DownloadPlanBuilder
from mediadl.index.repository import IndexRepository
from mediadl.sources.models import MediaItemStub, ScanResult, SourceDescriptor, SourceKind
from mediadl.storage.database import Database


def media(
    index: int,
    *,
    title: str | None = None,
    duration: float = 60.0,
) -> MediaItemStub:
    return MediaItemStub(
        media_key=str(index),
        title=title or f"Video {index}",
        url=f"https://www.youtube.com/watch?v={index}",
        duration_seconds=duration,
        view_count=index * 1_000,
        like_count=index * 10,
        upload_date="20250101",
    )


def source() -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=SourceKind.CHANNEL_VIDEOS,
        source_key="@Scale",
        url="https://www.youtube.com/@Scale/videos",
        title="Scale",
    )


def test_scan_limit_is_used_only_when_unseen_items_cannot_change_result() -> None:
    assert compile_collection_query(CollectionQueryInput(first=25)).scan_limit == 25
    assert compile_collection_query(CollectionQueryInput(range_value="10:40")).scan_limit == 40
    assert compile_collection_query(CollectionQueryInput(range_value="10:")).scan_limit is None
    assert compile_collection_query(CollectionQueryInput(last=25)).scan_limit is None
    assert compile_collection_query(CollectionQueryInput(latest=25)).scan_limit is None
    assert compile_collection_query(CollectionQueryInput(most_viewed=25)).scan_limit is None
    assert (
        compile_collection_query(CollectionQueryInput(first=25, views_above="10L")).scan_limit
        is None
    )


def test_workflow_marks_bounded_scan_partial(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    class Scanner:
        def scan(self, descriptor: SourceDescriptor, *, max_items: int | None = None) -> ScanResult:
            calls.append(("scan", max_items))
            count = max_items or 100
            return ScanResult(
                source=descriptor,
                title="Scale",
                items=tuple(media(i) for i in range(count)),
                reported_count=100,
            )

    class Index:
        def index_scan(self, scan: ScanResult, *, complete_scan: bool) -> None:
            calls.append(("index", complete_scan))

    class Enricher:
        def enrich(self, items: object, required: object) -> object:
            return items

    builder = DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=10_000_000, used=0, free=10_000_000),
        id_provider=lambda: "scale-plan",
    )
    workflow = CollectionWorkflow(
        Scanner(),  # type: ignore[arg-type]
        Index(),  # type: ignore[arg-type]
        Enricher(),  # type: ignore[arg-type]
        plan_builder=builder,
    )

    plan = workflow.prepare(
        source(),
        compile_collection_query(CollectionQueryInput(first=10)),
        output_format=OutputFormat.MP4,
        quality="best",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=tmp_path,
    )

    assert calls[:2] == [("scan", 10), ("index", False)]
    assert plan.item_count == 10


def test_filtered_first_n_stops_deep_enrichment_after_enough_matches(tmp_path: Path) -> None:
    enriched_keys: list[str] = []
    index_calls: list[bool] = []
    scan_limits: list[int | None] = []

    class Scanner:
        def scan(self, descriptor: SourceDescriptor, *, max_items: int | None = None) -> ScanResult:
            scan_limits.append(max_items)
            count = min(max_items or 100, 100)
            return ScanResult(
                source=descriptor,
                title="Scale",
                items=tuple(replace(media(i), like_count=None) for i in range(count)),
                reported_count=100,
            )

    class Index:
        def index_scan(self, scan: ScanResult, *, complete_scan: bool) -> None:
            index_calls.append(complete_scan)

    class Enricher:
        def enrich(
            self,
            items: object,
            required: object,
            *,
            parallelism: int = 1,
        ) -> tuple[MediaItemStub, ...]:
            source_items = tuple(items)  # type: ignore[arg-type]
            assert parallelism == 4
            enriched: list[MediaItemStub] = []
            for item in source_items:
                enriched_keys.append(item.media_key)
                likes = {"0": 1_000, "1": 5_000, "2": 20_000, "3": 30_000}.get(
                    item.media_key,
                    50_000,
                )
                enriched.append(replace(item, like_count=likes))
            return tuple(enriched)

    builder = DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=10_000_000, used=0, free=10_000_000),
        id_provider=lambda: "filtered-first-plan",
    )
    workflow = CollectionWorkflow(
        Scanner(),  # type: ignore[arg-type]
        Index(),  # type: ignore[arg-type]
        Enricher(),  # type: ignore[arg-type]
        plan_builder=builder,
    )
    query = compile_collection_query(CollectionQueryInput(first=2, likes_above="10K"))

    plan = workflow.prepare(
        source(),
        query,
        output_format=OutputFormat.MP4,
        quality="best",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=tmp_path,
    )

    assert query.first_match_limit == 2
    assert scan_limits == [20]
    assert enriched_keys == ["0", "1", "2", "3"]
    assert index_calls == [False, False]
    assert plan.item_count == 2


def test_channel_oldest_uses_preloaded_newest_first_scan_without_date_enrichment(
    tmp_path: Path,
) -> None:
    scan_calls = 0
    required_sets: list[set[object]] = []
    indexed: list[bool] = []

    class Scanner:
        def scan(self, descriptor: SourceDescriptor, *, max_items: int | None = None) -> ScanResult:
            nonlocal scan_calls
            scan_calls += 1
            raise AssertionError("preloaded guided scan should avoid a second collection scan")

    class Index:
        def index_scan(self, scan: ScanResult, *, complete_scan: bool) -> None:
            indexed.append(complete_scan)

    class Enricher:
        def enrich(self, items: object, required: object) -> object:
            required_sets.append(set(required))  # type: ignore[arg-type]
            return items

    builder = DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=10_000_000, used=0, free=10_000_000),
        id_provider=lambda: "oldest-preloaded-plan",
    )
    workflow = CollectionWorkflow(
        Scanner(),  # type: ignore[arg-type]
        Index(),  # type: ignore[arg-type]
        Enricher(),  # type: ignore[arg-type]
        plan_builder=builder,
    )
    items = tuple(
        replace(media(i), upload_date=None)
        for i in range(874)
    )
    preloaded = ScanResult(
        source=source(),
        title="Scale",
        items=items,
        reported_count=874,
    )

    plan = workflow.prepare(
        source(),
        compile_collection_query(CollectionQueryInput(oldest=500)),
        output_format=OutputFormat.M4A,
        quality="best",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=tmp_path,
        preloaded_scan=preloaded,
    )

    assert scan_calls == 0
    assert indexed == [True]
    assert required_sets == [set()]
    assert plan.item_count == 500
    assert plan.items[0].media_key == "873"
    assert plan.items[-1].media_key == "374"


def test_candidate_screening_finds_title_variant_and_respects_duration() -> None:
    items = [
        media(1, title="Artist - Great Song Official Video", duration=180),
        media(2, title="Artist - Great Song Lyrics", duration=181),
        media(3, title="Artist - Great Song Live", duration=300),
    ]

    pairs = list(iter_candidate_pairs(items))
    keys = {(pair.left_media_key, pair.right_media_key) for pair in pairs}

    assert ("1", "2") in keys
    assert ("1", "3") not in keys
    assert ("2", "3") not in keys


def test_exact_normalized_title_survives_bounded_history() -> None:
    items = [media(0, title="Artist Great Song Official Video", duration=180)]
    items.extend(media(i, title=f"Other Track {i}", duration=180) for i in range(1, 200))
    items.append(media(999, title="Artist Great Song Lyrics", duration=180))

    pairs = list(
        iter_candidate_pairs(
            items,
            max_candidates_per_item=4,
            token_history=8,
            duration_history=2,
        )
    )

    assert any(pair.left_media_key == "0" and pair.right_media_key == "999" for pair in pairs)


def test_candidate_count_is_strictly_bounded_for_same_duration_short_like_data() -> None:
    count = 10_000
    items = [media(i, title=f"Short clip {i}", duration=60) for i in range(count)]

    pairs = sum(
        1
        for _ in iter_candidate_pairs(
            items,
            max_candidates_per_item=6,
            token_history=16,
            duration_history=4,
        )
    )

    assert pairs <= count * 6


def test_50000_item_filter_sort_selection_has_bounded_incremental_memory() -> None:
    count = 50_000
    items = [media(i, duration=float(30 + (i % 300))) for i in range(count)]
    query = compile_collection_query(CollectionQueryInput(views_above="10L", most_viewed=100))

    tracemalloc.start()
    started = time.perf_counter()
    selected = query.apply(items)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(selected) == 100
    assert selected[0].view_count == (count - 1) * 1_000
    assert elapsed < 5.0
    assert peak < 64 * 1024 * 1024


def test_10000_item_sqlite_index_and_repeat_scan_remain_correct(tmp_path: Path) -> None:
    database = Database(tmp_path / "scale.sqlite3")
    assert database.initialize() == 3
    repository = IndexRepository(database)
    items = tuple(media(i) for i in range(10_000))
    scan = ScanResult(source=source(), title="Scale", items=items, reported_count=len(items))

    started = time.perf_counter()
    first = repository.index_scan(scan, complete_scan=True)
    second = repository.index_scan(scan, complete_scan=True)
    elapsed = time.perf_counter() - started

    assert first.added_count == 10_000
    assert second.added_count == 0
    assert second.unchanged_count == 10_000
    assert elapsed < 15.0
