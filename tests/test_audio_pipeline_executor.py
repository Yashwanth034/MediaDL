from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mediadl.core.errors import DownloadError, UserCancelledError
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.dedupe.basic import BasicDedupeService
from mediadl.downloads.executor import CollectionJobExecutor
from mediadl.downloads.jobs import JobRepository, JobStatus, RetryPolicy
from mediadl.downloads.plan import DiskSpace, DownloadPlanBuilder
from mediadl.downloads.single import (
    DownloadReceipt,
    SingleDownloadRequest,
    StagedDownloadReceipt,
)
from mediadl.sources.models import MediaItemStub, SourceDescriptor, SourceKind
from mediadl.storage.database import Database


def _source() -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=SourceKind.CHANNEL_VIDEOS,
        source_key="@Pipeline",
        url="https://www.youtube.com/@Pipeline/videos",
        title="Pipeline Channel",
    )


def _plan(
    tmp_path: Path,
    *,
    output_format: OutputFormat,
    count: int = 8,
    plan_id: str = "audio-pipeline-job",
):
    items = [
        MediaItemStub(
            media_key=f"item-{index}",
            title=f"Item {index}",
            url=f"https://www.youtube.com/watch?v=item-{index}",
            media_type="video",
        )
        for index in range(count)
    ]
    return DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=10_000_000, used=0, free=10_000_000),
        now_provider=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        id_provider=lambda: plan_id,
    ).build(
        source=_source(),
        items=items,
        output_format=output_format,
        quality="best",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=tmp_path / "downloads",
        selection_label="All",
        sort_mode="source",
    )


def _runtime(tmp_path: Path, *, output_format: OutputFormat, count: int = 8):
    database = Database(tmp_path / "pipeline.sqlite3")
    assert database.initialize() == 3
    jobs = JobRepository(database)
    dedupe = BasicDedupeService(database)
    jobs.create_job(_plan(tmp_path, output_format=output_format, count=count))
    return jobs, dedupe


class PipelineDownloader:
    def __init__(self, *, delay: float = 0.05) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.completed_sources = 0
        self.source_calls: list[str] = []
        self.direct_calls: list[str] = []
        self.errors: dict[str, BaseException] = {}

    def download_source(
        self,
        request: SingleDownloadRequest,
        *,
        progress_hook: object | None = None,
    ) -> StagedDownloadReceipt:
        media_id = request.url.rsplit("=", 1)[-1]
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            self.source_calls.append(media_id)
        try:
            if callable(progress_hook):
                progress_hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 1,
                        "total_bytes": 2,
                    }
                )
            error = self.errors.get(media_id)
            if error is not None:
                raise error
            time.sleep(self.delay)
            request.output_dir.mkdir(parents=True, exist_ok=True)
            source_path = request.output_dir / f"Item {media_id} [{media_id}].webm"
            source_path.write_bytes((f"source-{media_id}-" * 16).encode())
            with self.lock:
                self.completed_sources += 1
            return StagedDownloadReceipt(
                media_id=media_id,
                title=f"Item {media_id}",
                source_url=request.url,
                output_format=request.output_format,
                quality=request.quality,
                source_path=source_path,
            )
        finally:
            with self.lock:
                self.active -= 1

    def download(
        self,
        request: SingleDownloadRequest,
        *,
        progress_hook: object | None = None,
        postprocessor_hook: object | None = None,
    ) -> DownloadReceipt:
        media_id = request.url.rsplit("=", 1)[-1]
        with self.lock:
            self.direct_calls.append(media_id)
        if callable(progress_hook):
            progress_hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 1,
                    "total_bytes": 1,
                }
            )
        return DownloadReceipt(
            media_id=media_id,
            title=f"Item {media_id}",
            source_url=request.url,
            output_format=request.output_format,
            quality=request.quality,
        )


class PipelineConverter:
    def __init__(self, downloader: PipelineDownloader, *, total: int, delay: float = 0.08) -> None:
        self.downloader = downloader
        self.total = total
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.calls: list[str] = []
        self.started_before_all_downloads_finished = False

    def convert(self, staged, *, disk_guard, stop_event):  # type: ignore[no-untyped-def]
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            self.calls.append(str(staged.media_id))
            if self.downloader.completed_sources < self.total:
                self.started_before_all_downloads_finished = True
        try:
            deadline = time.monotonic() + self.delay
            while time.monotonic() < deadline:
                if stop_event.is_set():
                    from mediadl.downloads.audio_pipeline import AudioConversionCancelled

                    raise AudioConversionCancelled
                time.sleep(0.005)
            target = staged.source_path.with_suffix(f".{staged.output_format.value}")
            target.write_bytes((f"converted-{staged.media_id}-" * 16).encode())
            staged.source_path.unlink(missing_ok=True)
            return DownloadReceipt(
                media_id=staged.media_id,
                title=staged.title,
                source_url=staged.source_url,
                output_format=staged.output_format,
                quality=staged.quality,
                output_path=target,
            )
        finally:
            with self.lock:
                self.active -= 1


def test_heavy_audio_pipeline_bounds_downloads_and_conversions_and_overlaps_stages(
    tmp_path: Path,
) -> None:
    jobs, dedupe = _runtime(tmp_path, output_format=OutputFormat.MP3, count=8)
    downloader = PipelineDownloader(delay=0.05)
    converter = PipelineConverter(downloader, total=8, delay=0.08)
    events: list[dict[str, object]] = []

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        max_workers=3,
        conversion_workers=2,
        audio_converter=converter,  # type: ignore[arg-type]
        progress_observer=lambda _title, _position, _total, status: events.append(dict(status)),
    ).run("audio-pipeline-job")

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 8
    assert summary.skipped == 0
    assert downloader.peak_active == 3
    assert converter.peak_active == 2
    assert converter.started_before_all_downloads_finished
    assert len(downloader.source_calls) == 8
    assert len(converter.calls) == 8
    assert downloader.direct_calls == []
    assert any(int(event.get("converting_items", 0)) > 0 for event in events)
    assert jobs.progress("audio-pipeline-job").active == 0


def test_m4a_keeps_native_direct_path_and_never_uses_conversion_pipeline(tmp_path: Path) -> None:
    jobs, dedupe = _runtime(tmp_path, output_format=OutputFormat.M4A, count=4)
    downloader = PipelineDownloader(delay=0.01)
    converter = PipelineConverter(downloader, total=4)

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        max_workers=4,
        conversion_workers=2,
        audio_converter=converter,  # type: ignore[arg-type]
    ).run("audio-pipeline-job")

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 4
    assert sorted(downloader.direct_calls) == [f"item-{index}" for index in range(4)]
    assert downloader.source_calls == []
    assert converter.calls == []


def test_heavy_pipeline_duplicate_is_skipped_before_source_bandwidth(tmp_path: Path) -> None:
    jobs, dedupe = _runtime(tmp_path, output_format=OutputFormat.MP3, count=3)
    existing = tmp_path / "existing.mp3"
    existing.write_bytes(b"existing unique audio")
    dedupe.mark_source_completed(
        media_key="item-0",
        output_format=OutputFormat.MP3,
        quality="best",
        output_path=str(existing),
    )
    downloader = PipelineDownloader(delay=0.01)
    converter = PipelineConverter(downloader, total=2, delay=0.01)

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        max_workers=3,
        conversion_workers=2,
        audio_converter=converter,  # type: ignore[arg-type]
    ).run("audio-pipeline-job")

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 2
    assert summary.skipped_duplicate == 1
    assert "item-0" not in downloader.source_calls
    assert len(downloader.source_calls) == 2
    assert len(converter.calls) == 2


def test_heavy_pipeline_unavailable_item_does_not_abort_other_workers_and_reason_is_kept(
    tmp_path: Path,
) -> None:
    jobs, dedupe = _runtime(tmp_path, output_format=OutputFormat.MP3, count=4)
    downloader = PipelineDownloader(delay=0.01)
    downloader.errors["item-1"] = DownloadError(
        "This video is private.", retryable=False, category="private"
    )
    converter = PipelineConverter(downloader, total=3, delay=0.01)

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        max_workers=3,
        conversion_workers=2,
        audio_converter=converter,  # type: ignore[arg-type]
    ).run("audio-pipeline-job")

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 3
    assert summary.skipped_unavailable == 1
    assert len(converter.calls) == 3
    breakdown = jobs.job_breakdown("audio-pipeline-job")
    assert breakdown.unavailable_reasons == (("private: This video is private.", 1),)


def test_heavy_pipeline_retry_preserves_completed_items_and_retries_only_failed_item(
    tmp_path: Path,
) -> None:
    jobs, dedupe = _runtime(tmp_path, output_format=OutputFormat.MP3, count=4)
    downloader = PipelineDownloader(delay=0.01)
    downloader.errors["item-1"] = DownloadError(
        "Temporary network error", retryable=True, category="network"
    )
    converter = PipelineConverter(downloader, total=3, delay=0.01)
    executor = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        max_workers=3,
        conversion_workers=2,
        audio_converter=converter,  # type: ignore[arg-type]
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=3600, max_delay_seconds=3600),
    )

    first = executor.run("audio-pipeline-job")
    assert first.status is JobStatus.PAUSED
    assert first.completed == 3
    assert first.failed == 1
    first_calls = list(downloader.source_calls)
    assert first_calls.count("item-1") == 1

    assert jobs.retry_failures("audio-pipeline-job") == 1
    downloader.errors.clear()
    second = executor.run("audio-pipeline-job")

    assert second.status is JobStatus.COMPLETED
    assert second.completed == 4
    assert downloader.source_calls.count("item-1") == 2
    for media_id in ("item-0", "item-2", "item-3"):
        assert downloader.source_calls.count(media_id) == 1


def test_due_retryable_is_requeued_once_on_later_pipeline_resume(tmp_path: Path) -> None:
    database = Database(tmp_path / "retry-due.sqlite3")
    assert database.initialize() == 3
    jobs = JobRepository(database)
    jobs.create_job(_plan(tmp_path, output_format=OutputFormat.MP3, count=1))
    jobs.start_job("audio-pipeline-job")
    claimed = jobs.claim_next("audio-pipeline-job")
    assert claimed is not None
    jobs.fail_item(
        claimed.job_item_id,
        category="network",
        message="temporary",
        retryable=True,
        policy=RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0),
    )
    jobs.pause_job("audio-pipeline-job")
    jobs.start_job("audio-pipeline-job")

    assert jobs.requeue_due_retryable("audio-pipeline-job") == 1
    retry = jobs.claim_next("audio-pipeline-job", include_retryable=False)
    assert retry is not None
    assert retry.job_item_id == claimed.job_item_id
    assert retry.attempts == 2
    assert jobs.claim_next("audio-pipeline-job", include_retryable=False) is None


def test_heavy_pipeline_worker_interrupt_stops_all_threads_and_leaves_job_resumable(
    tmp_path: Path,
) -> None:
    jobs, dedupe = _runtime(tmp_path, output_format=OutputFormat.MP3, count=5)
    downloader = PipelineDownloader(delay=0.03)
    downloader.errors["item-0"] = KeyboardInterrupt()
    converter = PipelineConverter(downloader, total=4, delay=0.08)

    with pytest.raises(UserCancelledError) as caught:
        CollectionJobExecutor(
            jobs,
            downloader,  # type: ignore[arg-type]
            dedupe,
            max_workers=3,
            conversion_workers=2,
            audio_converter=converter,  # type: ignore[arg-type]
        ).run("audio-pipeline-job")

    assert caught.value.exit_code == 130
    progress = jobs.progress("audio-pipeline-job")
    assert progress.status is JobStatus.PAUSED
    assert progress.active == 0
    assert not any(
        thread.is_alive() and thread.name.startswith("mediadl-audio-")
        for thread in threading.enumerate()
    )
