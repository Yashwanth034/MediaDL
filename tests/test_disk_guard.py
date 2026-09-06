from pathlib import Path
from types import SimpleNamespace

import pytest

from mediadl.core.errors import DownloadError, InputError
from mediadl.downloads.disk_guard import DiskGuardPolicy, DiskSpaceGuard


def usage(free: int) -> SimpleNamespace:
    return SimpleNamespace(total=1000, used=1000 - free, free=free)


def test_disk_guard_allows_space_at_or_above_reserve(tmp_path: Path) -> None:
    guard = DiskSpaceGuard(
        tmp_path / "future" / "downloads",
        policy=DiskGuardPolicy(reserve_bytes=100),
        disk_usage=lambda _: usage(100),
    )

    guard.ensure_space()
    guard.progress_hook({"status": "downloading"})
    guard.progress_hook({"status": "finished"})


def test_disk_guard_raises_retryable_disk_space_error_below_reserve(tmp_path: Path) -> None:
    guard = DiskSpaceGuard(
        tmp_path,
        policy=DiskGuardPolicy(reserve_bytes=100),
        disk_usage=lambda _: usage(99),
    )

    with pytest.raises(DownloadError) as caught:
        guard.ensure_space()

    assert caught.value.retryable is True
    assert caught.value.category == "disk_space"
    assert "safety reserve" in str(caught.value)


def test_disk_guard_ignores_non_transfer_progress_events(tmp_path: Path) -> None:
    guard = DiskSpaceGuard(
        tmp_path,
        policy=DiskGuardPolicy(reserve_bytes=100),
        disk_usage=lambda _: usage(0),
    )

    guard.progress_hook({"status": "preparing"})
    guard.progress_hook({})


def test_disk_guard_rejects_negative_reserve() -> None:
    with pytest.raises(InputError, match="cannot be negative"):
        DiskGuardPolicy(reserve_bytes=-1)
