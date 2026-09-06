"""High-level collection planning workflow used by the CLI."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from mediadl.cli.collection import CollectionQuery
from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.downloads.plan import DownloadPlan, DownloadPlanBuilder
from mediadl.index.enrichment import MetadataEnricher, MetadataField
from mediadl.index.repository import IndexRepository
from mediadl.sources.availability import AvailabilityAction, AvailabilityPolicy
from mediadl.sources.models import MediaItemStub, ScanResult, SourceDescriptor
from mediadl.sources.scanner import CollectionScanner


class NoMatchingMediaError(InputError):
    """Expected outcome when a valid collection has no items after user filters."""

    def __init__(
        self,
        message: str,
        *,
        source_title: str | None = None,
        available_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.source_title = source_title
        self.available_count = available_count


class CollectionWorkflow:
    """Plan one playlist/channel-tab collection without downloading media."""

    def __init__(
        self,
        scanner: CollectionScanner,
        index: IndexRepository,
        enricher: MetadataEnricher,
        *,
        plan_builder: DownloadPlanBuilder | None = None,
    ) -> None:
        self.scanner = scanner
        self.index = index
        self.enricher = enricher
        self.plan_builder = plan_builder or DownloadPlanBuilder()

    def prepare(
        self,
        source: SourceDescriptor,
        query: CollectionQuery,
        *,
        output_format: OutputFormat,
        quality: str,
        dedupe_mode: DedupeMode,
        output_dir: Path,
        preloaded_scan: ScanResult | None = None,
    ) -> DownloadPlan:
        first_match_limit = query.first_match_limit
        if first_match_limit is not None:
            if preloaded_scan is not None:
                scan = _validated_preloaded_scan(source, preloaded_scan)
                candidates = tuple(
                    item
                    for item in scan.items
                    if AvailabilityPolicy.decide(item.availability).action
                    in {AvailabilityAction.DOWNLOAD, AvailabilityAction.AUTH_REQUIRED}
                )
                selected, changed = self._select_first_matches(
                    candidates,
                    query,
                    limit=first_match_limit,
                    required=set(query.required_metadata),
                )
                if changed:
                    self.index.index_scan(
                        ScanResult(
                            source=scan.source,
                            title=scan.title,
                            items=changed,
                            reported_count=scan.reported_count,
                            skipped_entries=scan.skipped_entries,
                        ),
                        complete_scan=False,
                    )
            else:
                scan, selected = self._scan_first_matches(
                    source,
                    query,
                    limit=first_match_limit,
                )
            if not selected:
                candidates = tuple(
                    item
                    for item in scan.items
                    if AvailabilityPolicy.decide(item.availability).action
                    in {AvailabilityAction.DOWNLOAD, AvailabilityAction.AUTH_REQUIRED}
                )
                raise NoMatchingMediaError(
                    _no_match_message(query, candidates),
                    source_title=scan.title,
                    available_count=len(candidates),
                )
            titled_source = replace(source, title=scan.title)
            return self.plan_builder.build(
                source=titled_source,
                items=selected,
                output_format=output_format,
                quality=quality,
                dedupe_mode=dedupe_mode,
                output_dir=output_dir,
                selection_label=query.selection_label,
                sort_mode=query.sort_mode.value,
                estimated_size_bytes=None,
            )

        scan_limit = query.scan_limit
        if preloaded_scan is not None:
            full_scan = _validated_preloaded_scan(source, preloaded_scan)
            scan = _slice_scan(full_scan, scan_limit)
        else:
            scan = self.scanner.scan(source, max_items=scan_limit)
        self.index.index_scan(scan, complete_scan=scan_limit is None)

        candidates = tuple(
            item
            for item in scan.items
            if AvailabilityPolicy.decide(item.availability).action
            in {AvailabilityAction.DOWNLOAD, AvailabilityAction.AUTH_REQUIRED}
        )
        use_source_date_order = source.kind.is_channel_tab and query.source_date_order_eligible
        required = set(query.required_metadata)
        if use_source_date_order:
            required.discard(MetadataField.UPLOAD_DATE)
        enriched = self.enricher.enrich(candidates, required)
        changed = tuple(
            enriched_item
            for original, enriched_item in zip(candidates, enriched, strict=True)
            if enriched_item != original
        )
        selected = (
            query.apply_source_date_order(enriched)
            if use_source_date_order
            else query.apply(enriched)
        )
        if not selected:
            raise NoMatchingMediaError(
                _no_match_message(query, enriched),
                source_title=scan.title,
                available_count=len(enriched),
            )

        if changed:
            enriched_scan = ScanResult(
                source=scan.source,
                title=scan.title,
                items=changed,
                reported_count=scan.reported_count,
                skipped_entries=scan.skipped_entries,
            )
            self.index.index_scan(enriched_scan, complete_scan=False)

        titled_source = replace(source, title=scan.title)
        return self.plan_builder.build(
            source=titled_source,
            items=selected,
            output_format=output_format,
            quality=quality,
            dedupe_mode=dedupe_mode,
            output_dir=output_dir,
            selection_label=query.selection_label,
            sort_mode=query.sort_mode.value,
            estimated_size_bytes=None,
        )

    def _scan_first_matches(
        self,
        source: SourceDescriptor,
        query: CollectionQuery,
        *,
        limit: int,
    ) -> tuple[ScanResult, tuple[MediaItemStub, ...]]:
        """Scan bounded prefixes and stop once enough source-ordered matches are known."""

        required = set(query.required_metadata)
        selected: list[MediaItemStub] = []
        processed_valid_items = 0
        scan_limit = max(20, min(200, limit * 4))

        while True:
            scan = self.scanner.scan(source, max_items=scan_limit)
            scanned_positions = len(scan.items) + scan.skipped_entries
            reached_end = scanned_positions < scan_limit
            self.index.index_scan(scan, complete_scan=reached_end)

            new_items = scan.items[processed_valid_items:]
            candidates = tuple(
                item
                for item in new_items
                if AvailabilityPolicy.decide(item.availability).action
                in {AvailabilityAction.DOWNLOAD, AvailabilityAction.AUTH_REQUIRED}
            )
            matches, changed = self._select_first_matches(
                candidates,
                query,
                limit=limit - len(selected),
                required=required,
            )
            selected.extend(matches)

            if changed:
                enriched_scan = ScanResult(
                    source=scan.source,
                    title=scan.title,
                    items=changed,
                    reported_count=scan.reported_count,
                    skipped_entries=scan.skipped_entries,
                )
                self.index.index_scan(enriched_scan, complete_scan=False)

            if len(selected) >= limit or reached_end:
                return scan, tuple(selected)

            processed_valid_items = len(scan.items)
            scan_limit *= 2

    def _select_first_matches(
        self,
        candidates: tuple[MediaItemStub, ...],
        query: CollectionQuery,
        *,
        limit: int,
        required: set[MetadataField],
    ) -> tuple[tuple[MediaItemStub, ...], tuple[MediaItemStub, ...]]:
        """Enrich in source order and stop once enough filtered matches are known."""

        selected: list[MediaItemStub] = []
        changed: list[MediaItemStub] = []
        batch_size = 4
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            enriched_batch = self.enricher.enrich(batch, required, parallelism=batch_size)
            for candidate, enriched in zip(batch, enriched_batch, strict=True):
                if enriched != candidate:
                    changed.append(enriched)
                if query.matches_item(enriched):
                    selected.append(enriched)
                    if len(selected) >= limit:
                        return tuple(selected), tuple(changed)
        return tuple(selected), tuple(changed)


def _validated_preloaded_scan(
    source: SourceDescriptor,
    scan: ScanResult,
) -> ScanResult:
    if scan.source.kind is not source.kind or scan.source.url != source.url:
        raise InputError("Preloaded collection scan does not match the requested source")
    return scan


def _slice_scan(scan: ScanResult, limit: int | None) -> ScanResult:
    if limit is None or limit >= len(scan.items):
        return scan
    return ScanResult(
        source=scan.source,
        title=scan.title,
        items=scan.items[:limit],
        reported_count=scan.reported_count,
        skipped_entries=scan.skipped_entries,
    )


def _no_match_message(
    query: CollectionQuery,
    candidates: tuple[MediaItemStub, ...] | list[MediaItemStub],
) -> str:
    items = tuple(candidates)
    count = len(items)
    if count == 0:
        return "This collection contains no currently usable media."

    spec = query.filter_spec
    maximum = spec.duration_max_seconds
    if maximum is not None:
        known = [item.duration_seconds for item in items if item.duration_seconds is not None]
        if len(known) == count and known and all(value > maximum for value in known):
            shortest = min(known)
            return (
                f"0 of {count} available item(s) matched: all are longer than the "
                f"{_format_duration(maximum)} maximum "
                f"(shortest is {_format_duration(shortest)})."
            )

    minimum = spec.duration_min_seconds
    if minimum is not None:
        known = [item.duration_seconds for item in items if item.duration_seconds is not None]
        if len(known) == count and known and all(value < minimum for value in known):
            longest = max(known)
            return (
                f"0 of {count} available item(s) matched: all are shorter than the "
                f"{_format_duration(minimum)} minimum "
                f"(longest is {_format_duration(longest)})."
            )

    return (
        f"0 of {count} available item(s) matched the current selection/filter rules. "
        "Adjust the filters or choose another source."
    )


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours and minutes == 0 and secs == 0:
        return f"{hours}h"
    if not hours and minutes and secs == 0:
        return f"{minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
