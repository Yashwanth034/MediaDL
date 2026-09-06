"""Runtime free-space protection for direct and collection downloads."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediadl.core.errors import DownloadError, InputError

DiskUsageProbe = Callable[[Path], shutil._ntuple_diskusage]


@dataclass(frozen=True, slots=True)
class DiskGuardPolicy:
    reserve_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.reserve_bytes < 0:
            raise InputError("Disk reserve cannot be negative")


class DiskSpaceGuard:
    """Abort before the filesystem reaches a configured safety reserve."""

    def __init__(
        self,
        output_dir: Path,
        *,
        policy: DiskGuardPolicy | None = None,
        disk_usage: DiskUsageProbe = shutil.disk_usage,
    ) -> None:
        self.output_dir = output_dir.expanduser().resolve(strict=False)
        self.policy = policy or DiskGuardPolicy()
        self.disk_usage = disk_usage

    def ensure_space(self) -> None:
        probe = _nearest_existing_path(self.output_dir)
        free = int(self.disk_usage(probe).free)
        if free < self.policy.reserve_bytes:
            raise DownloadError(
                "Download paused because free disk space fell below the safety reserve "
                f"({_human_bytes(free)} free; {_human_bytes(self.policy.reserve_bytes)} reserved).",
                retryable=True,
                category="disk_space",
            )

    def progress_hook(self, status: Mapping[str, Any]) -> None:
        state = str(status.get("status") or "").casefold()
        if state in {"downloading", "finished"}:
            self.ensure_space()


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _human_bytes(value: int) -> str:
    amount = float(max(value, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"
