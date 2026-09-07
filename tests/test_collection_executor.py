import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mediadl.core.errors import DownloadError, MediaDLError, UserCancelledError
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.dedupe.basic import BasicDedupeService
from mediadl.downloads.executor import CollectionJobExecutor
from mediadl.downloads.jobs import JobRepository, JobStatus, RetryPolicy
from mediadl.downloads.plan import DiskSpace, DownloadPlanBuilder
from mediadl.downloads.single import DownloadReceipt, SingleDownloadRequest
from mediadl.sources.models import MediaItemStub, SourceDescriptor, SourceKind
from mediadl.storage.database import Database


class FakeDownloader:
    def __init__(self) -> None:
        self.requests: list[SingleDownloadRequest] = []
        self.errors: dict[str, Exception] = {}

    def download(
        self,
        request: SingleDownloadRequest,
        *,
        progress_hook: object | None = None,
        postprocessor_hook: object | None = None,
    ) -> DownloadReceipt:
        self.requests.append(request)
        if callable(progress_hook):
            progress_hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 50,
                    "total_bytes": 100,
                }
            )
        error = self.errors.get(request.url)
        if error is not None:
            raise error
        media_id = request.url.rsplit("=", 1)[-1]
        return DownloadReceipt(
            media_id=media_id,
            title=f"Video {media_id}",
            source_url=request.url,
            output_format=request.output_format,
            quality=request.quality,
        )


def source() -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=SourceKind.CHANNEL_VIDEOS,
        source_key="@Example",
        url="https://www.youtube.com/@Example/videos",
        title="Example Channel",
    )


def item(key: str, media_type: str = "video") -> MediaItemStub:
    return MediaItemStub(
        media_key=key,
        title=f"Video {key}",
        url=f"https://www.youtube.com/watch?v={key}",
        media_type=media_type,
    )


def build_plan(
    tmp_path: Path, keys: tuple[tuple[str, str], ...] = (("a", "video"), ("b", "short"))
):
    return DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=1000, used=0, free=1000),
        now_provider=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        id_provider=lambda: "executor-job",
    ).build(
        source=source(),
        items=[item(key, media_type) for key, media_type in keys],
        output_format=OutputFormat.MP4,
        quality="best",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=tmp_path / "downloads",
        selection_label="All",
        sort_mode="source",
    )


@pytest.fixture
def setup(tmp_path: Path) -> tuple[JobRepository, BasicDedupeService, FakeDownloader]:
    database = Database(tmp_path / "executor.sqlite3")
    assert database.initialize() == 3
    jobs = JobRepository(database)
    dedupe = BasicDedupeService(database)
    downloader = FakeDownloader()
    jobs.create_job(build_plan(tmp_path))
    return jobs, dedupe, downloader


def test_executor_downloads_frozen_plan_and_routes_clean_folders(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    summary = CollectionJobExecutor(jobs, downloader, dedupe).run("executor-job")  # type: ignore[arg-type]

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 2
    assert summary.skipped == 0
    assert [request.output_dir.name for request in downloader.requests] == ["Videos", "Shorts"]
    assert all(
        request.output_dir.parent.name == "Example Channel" for request in downloader.requests
    )


def test_executor_skips_source_profile_duplicate_before_download(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
    tmp_path: Path,
) -> None:
    jobs, dedupe, downloader = setup
    existing = tmp_path / "existing-a.mp4"
    existing.write_bytes(b"already downloaded")
    dedupe.mark_source_completed(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        output_path=str(existing),
    )

    summary = CollectionJobExecutor(jobs, downloader, dedupe).run("executor-job")  # type: ignore[arg-type]

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 1
    assert summary.skipped == 1
    assert [request.url for request in downloader.requests] == ["https://www.youtube.com/watch?v=b"]


def test_executor_skips_permanent_unavailable_failure_and_continues(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    downloader.errors["https://www.youtube.com/watch?v=a"] = DownloadError(
        "Private video",
        retryable=False,
        category="private",
    )

    summary = CollectionJobExecutor(jobs, downloader, dedupe).run("executor-job")  # type: ignore[arg-type]

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 1
    assert summary.skipped == 1
    assert len(downloader.requests) == 2


def test_restricted_skip_is_not_reported_as_media_unavailable(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    downloader.errors["https://www.youtube.com/watch?v=a"] = DownloadError(
        "This media requires authorized signed-in access.",
        retryable=False,
        category="auth_required",
    )

    summary = CollectionJobExecutor(jobs, downloader, dedupe).run("executor-job")  # type: ignore[arg-type]

    assert summary.status is JobStatus.COMPLETED
    assert summary.skipped == 1
    assert summary.skipped_unavailable == 0
    assert summary.skipped_restricted == 1
    breakdown = jobs.job_breakdown("executor-job")
    assert breakdown.skipped_media_unavailable == 0
    assert breakdown.skipped_restricted == 1


def test_executor_persists_redacted_engine_detail_for_final_failures(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    downloader.errors["https://www.youtube.com/watch?v=a"] = DownloadError(
        "The media request failed with an unclassified error.",
        retryable=False,
        category="unknown",
        detail="ERROR: [youtube] a: synthetic raw engine detail",
    )

    summary = CollectionJobExecutor(jobs, downloader, dedupe).run("executor-job")  # type: ignore[arg-type]

    assert summary.status is JobStatus.COMPLETED_WITH_FAILURES
    breakdown = jobs.job_breakdown("executor-job")
    assert breakdown.final_failed == 1
    assert breakdown.final_failure_reasons == (
        ("unknown: ERROR: [youtube] a: synthetic raw engine detail", 1),
    )


def test_executor_pauses_immediately_on_youtube_bot_check(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    downloader.errors["https://www.youtube.com/watch?v=a"] = DownloadError(
        "YouTube requested signed-in browser cookies to continue.",
        retryable=False,
        category="bot_check",
    )

    with pytest.raises(MediaDLError, match="signed-in browser cookies"):
        CollectionJobExecutor(jobs, downloader, dedupe).run("executor-job")  # type: ignore[arg-type]

    breakdown = jobs.job_breakdown("executor-job")
    assert breakdown.status is JobStatus.PAUSED
    assert breakdown.skipped_unavailable == 0
    assert breakdown.pending == 2
    assert len(downloader.requests) == 1
    with jobs.database.connection() as connection:
        paused_errors = connection.execute(
            "SELECT last_error FROM job_items WHERE job_id = ? AND last_error IS NOT NULL",
            ("executor-job",),
        ).fetchall()
    assert [str(row[0]) for row in paused_errors] == [
        "Paused for access requirement: YouTube requested signed-in browser cookies to continue."
    ]


@pytest.mark.parametrize(
    ("category", "message"),
    [
        ("cookie_access", "Browser cookies could not be read or decrypted."),
        ("po_token_required", "YouTube requires a compatible Proof-of-Origin token."),
    ],
)
def test_executor_pauses_on_access_prerequisites(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
    category: str,
    message: str,
) -> None:
    jobs, dedupe, downloader = setup
    downloader.errors["https://www.youtube.com/watch?v=a"] = DownloadError(
        message,
        retryable=False,
        category=category,
    )

    with pytest.raises(MediaDLError, match="paused this job"):
        CollectionJobExecutor(jobs, downloader, dedupe).run("executor-job")  # type: ignore[arg-type]

    breakdown = jobs.job_breakdown("executor-job")
    assert breakdown.status is JobStatus.PAUSED
    assert breakdown.skipped_unavailable == 0
    assert breakdown.pending == 2


def test_parallel_executor_pauses_on_youtube_bot_check_without_false_unavailable(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    downloader.errors["https://www.youtube.com/watch?v=a"] = DownloadError(
        "YouTube requested signed-in browser cookies to continue.",
        retryable=False,
        category="bot_check",
    )

    with pytest.raises(MediaDLError, match="signed-in browser cookies"):
        CollectionJobExecutor(jobs, downloader, dedupe, max_workers=2).run("executor-job")  # type: ignore[arg-type]

    breakdown = jobs.job_breakdown("executor-job")
    assert breakdown.status is JobStatus.PAUSED
    assert breakdown.skipped_unavailable == 0
    assert breakdown.pending >= 1


def test_executor_pauses_when_retryable_failure_is_waiting(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    downloader.errors["https://www.youtube.com/watch?v=a"] = DownloadError(
        "Temporary network error",
        retryable=True,
        category="network",
    )
    executor = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=3600, max_delay_seconds=3600),
    )

    summary = executor.run("executor-job")

    assert summary.status is JobStatus.PAUSED
    assert summary.completed == 1
    assert summary.failed == 1
    progress = jobs.progress("executor-job")
    assert progress.retryable_failed == 1


class LowDiskGuard:
    def ensure_space(self) -> None:
        raise DownloadError(
            "Download paused because free disk space fell below the safety reserve.",
            retryable=True,
            category="disk_space",
        )

    def progress_hook(self, status: object) -> None:
        pass


def test_executor_pauses_cleanly_when_disk_reserve_is_crossed(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    executor = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=3600, max_delay_seconds=3600),
        disk_guard_factory=lambda _: LowDiskGuard(),  # type: ignore[arg-type]
    )

    summary = executor.run("executor-job")

    assert summary.status is JobStatus.PAUSED
    assert summary.completed == 0
    assert summary.failed == 2
    assert downloader.requests == []
    assert jobs.progress("executor-job").retryable_failed == 2


def test_executor_forwards_concise_progress_context(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    events: list[tuple[str, int, int, str, int | None]] = []

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        progress_observer=lambda title, position, total, status: events.append(
            (
                title,
                position,
                total,
                str(status["status"]),
                int(status["downloaded_bytes"]) if "downloaded_bytes" in status else None,
            )
        ),
    ).run("executor-job")

    assert summary.status is JobStatus.COMPLETED
    assert events == [
        ("Video a", 1, 2, "processing", None),
        ("Video a", 1, 2, "downloading", 50),
        ("Video b", 2, 2, "processing", None),
        ("Video b", 2, 2, "downloading", 50),
    ]


def test_executor_ctrl_c_pauses_entire_job_and_can_resume(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, _ = setup

    class InterruptingDownloader:
        def download(
            self,
            request: SingleDownloadRequest,
            *,
            progress_hook: object | None = None,
            postprocessor_hook: object | None = None,
        ) -> DownloadReceipt:
            if callable(progress_hook):
                progress_hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 50,
                        "total_bytes": 100,
                    }
                )
            raise KeyboardInterrupt

    with pytest.raises(UserCancelledError) as caught:
        CollectionJobExecutor(
            jobs,
            InterruptingDownloader(),  # type: ignore[arg-type]
            dedupe,
        ).run("executor-job")

    assert caught.value.exit_code == 130
    assert "mdl resume executor-job" in str(caught.value)
    interrupted = jobs.progress("executor-job")
    assert interrupted.status is JobStatus.PAUSED
    assert interrupted.pending == 2
    assert interrupted.active == 0
    assert interrupted.completed == 0

    resumed_downloader = FakeDownloader()
    resumed = CollectionJobExecutor(jobs, resumed_downloader, dedupe).run("executor-job")  # type: ignore[arg-type]
    assert resumed.status is JobStatus.COMPLETED
    assert resumed.completed == 2
    assert len(resumed_downloader.requests) == 2


def test_parallel_executor_overlaps_collection_items_and_emits_batch_progress(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, _ = setup

    class ConcurrentDownloader:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.peak_active = 0

        def download(
            self,
            request: SingleDownloadRequest,
            *,
            progress_hook: object | None = None,
            postprocessor_hook: object | None = None,
        ) -> DownloadReceipt:
            with self.lock:
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
            try:
                if callable(progress_hook):
                    progress_hook(
                        {
                            "status": "downloading",
                            "downloaded_bytes": 50,
                            "total_bytes": 100,
                        }
                    )
                time.sleep(0.08)
                media_id = request.url.rsplit("=", 1)[-1]
                return DownloadReceipt(
                    media_id=media_id,
                    title=f"Video {media_id}",
                    source_url=request.url,
                    output_format=request.output_format,
                    quality=request.quality,
                )
            finally:
                with self.lock:
                    self.active -= 1

    downloader = ConcurrentDownloader()
    events: list[dict[str, object]] = []
    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        max_workers=2,
        progress_observer=lambda _title, _position, _total, status: events.append(dict(status)),
    ).run("executor-job")

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 2
    assert downloader.peak_active == 2
    assert any(event.get("status") == "batch" for event in events)
    assert any(int(event.get("active_items", 0)) == 2 for event in events)


def test_parallel_executor_tracks_postprocessing_without_leaving_active_items(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, _ = setup

    class PostprocessingDownloader:
        def download(
            self,
            request: SingleDownloadRequest,
            *,
            progress_hook: object | None = None,
            postprocessor_hook: object | None = None,
        ) -> DownloadReceipt:
            if callable(progress_hook):
                progress_hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
            if callable(postprocessor_hook):
                postprocessor_hook({"status": "started"})
            media_id = request.url.rsplit("=", 1)[-1]
            return DownloadReceipt(
                media_id=media_id,
                title=f"Video {media_id}",
                source_url=request.url,
                output_format=request.output_format,
                quality=request.quality,
            )

    events: list[dict[str, object]] = []
    summary = CollectionJobExecutor(
        jobs,
        PostprocessingDownloader(),  # type: ignore[arg-type]
        dedupe,
        max_workers=2,
        progress_observer=lambda _title, _position, _total, status: events.append(dict(status)),
    ).run("executor-job")

    assert summary.status is JobStatus.COMPLETED
    assert jobs.progress("executor-job").active == 0
    assert any(int(event.get("processing_items", 0)) > 0 for event in events)


def test_parallel_executor_worker_interrupt_pauses_entire_job_for_resume(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, _ = setup

    class InterruptingDownloader:
        def download(
            self,
            request: SingleDownloadRequest,
            *,
            progress_hook: object | None = None,
            postprocessor_hook: object | None = None,
        ) -> DownloadReceipt:
            if callable(progress_hook):
                progress_hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
            raise KeyboardInterrupt

    with pytest.raises(UserCancelledError) as caught:
        CollectionJobExecutor(
            jobs,
            InterruptingDownloader(),  # type: ignore[arg-type]
            dedupe,
            max_workers=2,
        ).run("executor-job")

    assert caught.value.exit_code == 130
    progress = jobs.progress("executor-job")
    assert progress.status is JobStatus.PAUSED
    assert progress.active == 0
    assert progress.pending == 2


def test_parallel_executor_keeps_source_profile_duplicate_precheck(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
    tmp_path: Path,
) -> None:
    jobs, dedupe, downloader = setup
    existing = tmp_path / "existing-a.mp4"
    existing.write_bytes(b"already downloaded")
    dedupe.mark_source_completed(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        output_path=str(existing),
    )

    summary = CollectionJobExecutor(
        jobs,
        downloader,
        dedupe,
        max_workers=2,
    ).run("executor-job")  # type: ignore[arg-type]

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 1
    assert summary.skipped == 1
    assert [request.url for request in downloader.requests] == ["https://www.youtube.com/watch?v=b"]


def test_executor_returns_terminal_job_without_redownloading(
    setup: tuple[JobRepository, BasicDedupeService, FakeDownloader],
) -> None:
    jobs, dedupe, downloader = setup
    executor = CollectionJobExecutor(jobs, downloader, dedupe)  # type: ignore[arg-type]
    assert executor.run("executor-job").status is JobStatus.COMPLETED
    request_count = len(downloader.requests)

    summary = executor.run("executor-job")

    assert summary.status is JobStatus.COMPLETED
    assert len(downloader.requests) == request_count
