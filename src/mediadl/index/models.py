"""Stable result models for persistent source indexing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IndexSummary:
    source_id: int
    scan_id: int
    added_count: int
    metadata_updated_count: int
    stats_updated_count: int
    unchanged_count: int
    missing_count: int
    complete_scan: bool

    @property
    def changed_count(self) -> int:
        return self.added_count + self.metadata_updated_count + self.stats_updated_count
