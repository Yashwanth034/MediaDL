from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.downloads.jobs import (
    JobItemStatus,
    JobRepository,
    JobStatus,
    RetryPolicy,
)
from mediadl.downloads.plan import DiskSpace, DownloadPlan, DownloadPlanBuilder
from mediadl.sources.models import MediaItemStub, SourceDescriptor, SourceKind
from mediadl.storage.database import Database


def source() -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=SourceKind.CHANNEL_VIDEOS,
        source_key="@Example",
        url="https://www.youtube.com/@Example/videos",
        title="Example",
    )


def media(key: str) -> MediaItemStub:
    return MediaItemStub(
        media_key=key,
        title=f"Video {key}",
        url=f"https://www.youtube.com/watch?v={key}",
        view_count=100,
        like_count=10,
    )


def plan(
    tmp_path: Path, *, plan_id: str = "job-1", keys: tuple[str, ...] = ("a", "b")
) -> DownloadPlan:
    return DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=1_000_000, used=0, free=1_000_000),
        now_provider=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        id_provider=lambda: plan_id,
    ).build(
        source=source(),
        items=[media(key) for key in keys],
        output_format=OutputFormat.MP4,
        quality="best",
        dedupe_mode=DedupeMode.SAFE,
        output_dir=tmp_path / "downloads",
        selection_label="All",
        sort_mode="source",
    )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "jobs.sqlite3")
    assert db.initialize() == 3
    return db


@pytest.fixture
def repository(database: Database) -> JobRepository:
    return JobRepository(database)


def test_create_load_and_claim_preserves_plan_order(
    repository: JobRepository,
    tmp_path: Path,
) -> None:
    created = plan(tmp_path, keys=("c", "a", "b"))
    assert repository.create_job(created) == "job-1"
    assert repository.load_plan("job-1") == created

    initial = repository.progress("job-1")
    assert initial.status is JobStatus.PLANNED
    assert initial.total == 3
    assert initial.pending == 3

    repository.start_job("job-1")
    first = repository.claim_next("job-1")
    assert first is not None
    assert first.media_key == "c"
    assert first.position == 0
    assert first.attempts == 1


def test_phase_progression_and_successful_job_completion(
    repository: JobRepository,
    tmp_path: Path,
) -> None:
    repository.create_job(plan(tmp_path, keys=("a", "b")))
    repository.start_job("job-1")

    first = repository.claim_next("job-1")
    assert first is not None
    repository.set_phase(first.job_item_id, JobItemStatus.POSTPROCESSING)
    repository.set_phase(first.job_item_id, JobItemStatus.VERIFYING)
    status = repository.complete_item(first.job_item_id, output_path="a.mp4")
    assert status is JobStatus.RUNNING

    second = repository.claim_next("job-1")
    assert second is not None
    final = repository.complete_item(second.job_item_id, status=JobItemStatus.SKIPPED_DUPLICATE)

    assert final is JobStatus.COMPLETED
    progress = repository.progress("job-1")
    assert progress.status is JobStatus.COMPLETED
    assert progress.completed == 1
    assert progress.skipped == 1
    assert progress.finished == 2


def test_retry_backoff_and_max_attempts(repository: JobRepository, tmp_path: Path) -> None:
    repository.create_job(plan(tmp_path, keys=("a",)))
    repository.start_job("job-1")
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=60)
    now = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)

    first = repository.claim_next("job-1", now=now)
    assert first is not None and first.attempts == 1
    status = repository.fail_item(
        first.job_item_id,
        category="network",
        message="temporary timeout",
        retryable=True,
        policy=policy,
        now=now,
    )
    assert status is JobStatus.RUNNING
    assert repository.claim_next("job-1", now=now + timedelta(seconds=9)) is None

    second = repository.claim_next("job-1", now=now + timedelta(seconds=10))
    assert second is not None and second.attempts == 2
    repository.fail_item(
        second.job_item_id,
        category="network",
        message="temporary timeout again",
        retryable=True,
        policy=policy,
        now=now + timedelta(seconds=10),
    )
    assert repository.claim_next("job-1", now=now + timedelta(seconds=29)) is None

    third = repository.claim_next("job-1", now=now + timedelta(seconds=30))
    assert third is not None and third.attempts == 3
    final = repository.fail_item(
        third.job_item_id,
        category="network",
        message="still failing",
        retryable=True,
        policy=policy,
        now=now + timedelta(seconds=30),
    )

    assert final is JobStatus.COMPLETED_WITH_FAILURES
    progress = repository.progress("job-1")
    assert progress.final_failed == 1
    assert progress.retryable_failed == 0


def test_manual_retry_makes_retryable_failure_immediately_pending(
    repository: JobRepository,
    tmp_path: Path,
) -> None:
    repository.create_job(plan(tmp_path, keys=("a",)))
    repository.start_job("job-1")
    now = datetime(2026, 9, 5, tzinfo=UTC)
    claimed = repository.claim_next("job-1", now=now)
    assert claimed is not None
    repository.fail_item(
        claimed.job_item_id,
        category="network",
        message="retry later",
        retryable=True,
        policy=RetryPolicy(max_attempts=5, base_delay_seconds=3600, max_delay_seconds=3600),
        now=now,
    )

    assert repository.retry_failures("job-1") == 1
    retried = repository.claim_next("job-1", now=now + timedelta(seconds=1))
    assert retried is not None
    assert retried.attempts == 2


def test_recover_unavailable_requeues_only_unavailable_items(
    repository: JobRepository,
    tmp_path: Path,
) -> None:
    repository.create_job(plan(tmp_path, keys=("a", "b", "c")))
    repository.start_job("job-1")

    completed = repository.claim_next("job-1")
    assert completed is not None
    repository.complete_item(completed.job_item_id, output_path="a.mp4")

    duplicate = repository.claim_next("job-1")
    assert duplicate is not None
    repository.complete_item(
        duplicate.job_item_id,
        status=JobItemStatus.SKIPPED_DUPLICATE,
        output_path="existing-b.mp4",
    )

    unavailable = repository.claim_next("job-1")
    assert unavailable is not None
    repository.complete_item(
        unavailable.job_item_id,
        status=JobItemStatus.SKIPPED_UNAVAILABLE,
        detail="extractor: old YouTube challenge failure",
    )
    before = repository.job_breakdown("job-1")
    assert before.status is JobStatus.COMPLETED
    assert before.completed == 1
    assert before.skipped_duplicate == 1
    assert before.skipped_unavailable == 1

    assert repository.recover_unavailable("job-1") == 1
    recovered = repository.job_breakdown("job-1")
    assert recovered.status is JobStatus.PAUSED
    assert recovered.completed == 1
    assert recovered.skipped_duplicate == 1
    assert recovered.skipped_unavailable == 0
    assert recovered.pending == 1

    repository.start_job("job-1")
    retry = repository.claim_next("job-1")
    assert retry is not None
    assert retry.media_key == "c"
    assert retry.attempts == 1
    assert repository.complete_item(retry.job_item_id, output_path="c.mp4") is JobStatus.COMPLETED


def test_interrupted_transient_item_recovers_to_paused_job_and_resumes(
    database: Database,
    tmp_path: Path,
) -> None:
    first_repo = JobRepository(database)
    first_repo.create_job(plan(tmp_path, keys=("a", "b")))
    first_repo.start_job("job-1")
    claimed = first_repo.claim_next("job-1")
    assert claimed is not None
    first_repo.set_phase(claimed.job_item_id, JobItemStatus.POSTPROCESSING)

    after_restart = JobRepository(database)
    assert after_restart.recover_interrupted_jobs() == ("job-1",)
    paused = after_restart.progress("job-1")
    assert paused.status is JobStatus.PAUSED
    assert paused.pending == 2
    assert paused.active == 0

    after_restart.start_job("job-1")
    reclaimed = after_restart.claim_next("job-1")
    assert reclaimed is not None
    assert reclaimed.media_key == "a"
    assert reclaimed.attempts == 2


def test_pause_requires_no_active_item(repository: JobRepository, tmp_path: Path) -> None:
    repository.create_job(plan(tmp_path, keys=("a",)))
    repository.start_job("job-1")
    claimed = repository.claim_next("job-1")
    assert claimed is not None

    with pytest.raises(InputError, match="active processing"):
        repository.pause_job("job-1")

    repository.fail_item(
        claimed.job_item_id,
        category="network",
        message="pause after retryable failure",
        retryable=True,
        policy=RetryPolicy(),
    )
    repository.pause_job("job-1")
    assert repository.progress("job-1").status is JobStatus.PAUSED


def test_cancel_marks_unfinished_items_terminal(repository: JobRepository, tmp_path: Path) -> None:
    repository.create_job(plan(tmp_path, keys=("a", "b", "c")))
    repository.start_job("job-1")
    repository.cancel_job("job-1")

    progress = repository.progress("job-1")
    assert progress.status is JobStatus.CANCELLED
    assert progress.cancelled == 3
    assert progress.finished == 3


def test_nonretryable_failure_finishes_with_failures(
    repository: JobRepository, tmp_path: Path
) -> None:
    repository.create_job(plan(tmp_path, keys=("a",)))
    repository.start_job("job-1")
    claimed = repository.claim_next("job-1")
    assert claimed is not None

    status = repository.fail_item(
        claimed.job_item_id,
        category="private",
        message="video is unavailable",
        retryable=False,
        policy=RetryPolicy(),
    )

    assert status is JobStatus.COMPLETED_WITH_FAILURES


def test_retry_policy_exponential_cap_and_validation() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=2, max_delay_seconds=5)
    assert policy.delay_seconds(1) == 2
    assert policy.delay_seconds(2) == 4
    assert policy.delay_seconds(3) == 5
    assert policy.delay_seconds(4) == 5

    with pytest.raises(InputError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(InputError):
        RetryPolicy(base_delay_seconds=-1)
    with pytest.raises(InputError):
        RetryPolicy(base_delay_seconds=10, max_delay_seconds=5)
    with pytest.raises(InputError):
        policy.delay_seconds(0)


def test_duplicate_job_and_invalid_state_transitions_are_rejected(
    repository: JobRepository,
    tmp_path: Path,
) -> None:
    repository.create_job(plan(tmp_path, keys=("a",)))
    with pytest.raises(InputError, match="already exists"):
        repository.create_job(plan(tmp_path, keys=("a",)))
    with pytest.raises(InputError, match="must be running"):
        repository.claim_next("job-1")

    repository.start_job("job-1")
    with pytest.raises(InputError, match="cannot start"):
        repository.start_job("job-1")
    claimed = repository.claim_next("job-1")
    assert claimed is not None
    with pytest.raises(InputError, match="Cannot move"):
        repository.set_phase(claimed.job_item_id, JobItemStatus.VERIFYING)


def test_persisted_plan_tampering_is_detected(
    repository: JobRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    repository.create_job(plan(tmp_path, keys=("a",)))
    with database.transaction() as connection:
        raw = connection.execute("SELECT plan_json FROM jobs WHERE id = 'job-1'").fetchone()[0]
        altered = str(raw).replace('"quality": "best"', '"quality": "720p"')
        connection.execute("UPDATE jobs SET plan_json = ? WHERE id = 'job-1'", (altered,))

    with pytest.raises(InputError, match="content hash"):
        repository.load_plan("job-1")


def test_plan_builder_rejects_duplicate_media_keys(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="same media item"):
        DownloadPlanBuilder(
            disk_probe=lambda _: DiskSpace(total=1, used=0, free=1),
            id_provider=lambda: "duplicate-plan",
        ).build(
            source=source(),
            items=[media("a"), media("a")],
            output_format=OutputFormat.MP4,
            quality="best",
            dedupe_mode=DedupeMode.SAFE,
            output_dir=tmp_path,
            selection_label="All",
            sort_mode="source",
        )
