"""Persistent job queue, retry/backoff, interruption recovery, and resume state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from mediadl.core.errors import DatabaseError, InputError
from mediadl.downloads.plan import DownloadPlan, PlanItem
from mediadl.storage.database import Database


class JobStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    CANCELLED = "cancelled"


class JobItemStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_UNAVAILABLE = "skipped_unavailable"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


_TERMINAL_ITEM_STATUSES = {
    JobItemStatus.COMPLETED,
    JobItemStatus.SKIPPED_DUPLICATE,
    JobItemStatus.SKIPPED_UNAVAILABLE,
    JobItemStatus.FAILED_FINAL,
    JobItemStatus.CANCELLED,
}
_TRANSIENT_ITEM_STATUSES = {
    JobItemStatus.DOWNLOADING,
    JobItemStatus.POSTPROCESSING,
    JobItemStatus.VERIFYING,
}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise InputError("Retry max attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise InputError("Retry delays cannot be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise InputError("Retry max delay cannot be below base delay")

    def delay_seconds(self, attempt: int) -> float:
        if attempt < 1:
            raise InputError("Retry attempt must be at least 1")
        delay = self.base_delay_seconds * (2 ** (attempt - 1))
        return min(delay, self.max_delay_seconds)


@dataclass(frozen=True, slots=True)
class QueueItem:
    job_id: str
    job_item_id: int
    media_key: str
    title: str
    url: str
    position: int
    attempts: int


@dataclass(frozen=True, slots=True)
class JobProgress:
    job_id: str
    status: JobStatus
    total: int
    pending: int
    active: int
    completed: int
    skipped: int
    retryable_failed: int
    final_failed: int
    cancelled: int

    @property
    def finished(self) -> int:
        return self.completed + self.skipped + self.final_failed + self.cancelled


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    status: JobStatus
    source_title: str | None
    source_key: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    total: int
    completed: int
    skipped: int
    retryable_failed: int
    final_failed: int


@dataclass(frozen=True, slots=True)
class JobBreakdown:
    job_id: str
    status: JobStatus
    total: int
    pending: int
    downloading: int
    postprocessing: int
    verifying: int
    completed: int
    skipped_duplicate: int
    skipped_unavailable: int
    retryable_failed: int
    final_failed: int
    cancelled: int
    unavailable_reasons: tuple[tuple[str, int], ...] = ()


class JobRepository:
    """Transactional persistent queue around the existing jobs/job_items schema."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_job(self, plan: DownloadPlan) -> str:
        verified = DownloadPlan.from_dict(plan.to_dict())
        media_keys = [item.media_key for item in verified.items]
        if len(media_keys) != len(set(media_keys)):
            raise InputError("A download plan cannot contain the same media item more than once")

        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (verified.plan_id,)
            ).fetchone()
            if existing is not None:
                raise InputError(f"Download job already exists: {verified.plan_id}")
            source_id = self._ensure_source(connection, verified)
            selection_json = json.dumps(
                {
                    "selection_label": verified.selection_label,
                    "sort_mode": verified.sort_mode,
                    "content_hash": verified.content_hash,
                },
                sort_keys=True,
            )
            connection.execute(
                """
                INSERT INTO jobs(id, source_id, status, selection_json, plan_json, output_dir)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    verified.plan_id,
                    source_id,
                    JobStatus.PLANNED.value,
                    selection_json,
                    verified.to_json(),
                    verified.output_dir,
                ),
            )
            for position, item in enumerate(verified.items):
                media_id = self._ensure_media(connection, source_id, item)
                connection.execute(
                    """
                    INSERT INTO job_items(job_id, media_item_id, position, status)
                    VALUES (?, ?, ?, ?)
                    """,
                    (verified.plan_id, media_id, position, JobItemStatus.PENDING.value),
                )
        return verified.plan_id

    def load_plan(self, job_id: str) -> DownloadPlan:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT plan_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise InputError(f"Unknown download job: {job_id}")
        return DownloadPlan.from_json(str(row[0]))

    def list_jobs(self, *, limit: int = 20) -> tuple[JobRecord, ...]:
        if limit < 1 or limit > 200:
            raise InputError("Job history limit must be between 1 and 200")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    j.id,
                    j.status,
                    s.title,
                    s.source_key,
                    j.created_at,
                    j.updated_at,
                    j.completed_at,
                    COUNT(ji.id) AS total,
                    SUM(CASE WHEN ji.status = ? THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN ji.status IN (?, ?) THEN 1 ELSE 0 END) AS skipped,
                    SUM(CASE WHEN ji.status = ? THEN 1 ELSE 0 END) AS retryable_failed,
                    SUM(CASE WHEN ji.status = ? THEN 1 ELSE 0 END) AS final_failed
                FROM jobs j
                LEFT JOIN sources s ON s.id = j.source_id
                LEFT JOIN job_items ji ON ji.job_id = j.id
                GROUP BY j.id
                ORDER BY j.updated_at DESC, j.created_at DESC
                LIMIT ?
                """,
                (
                    JobItemStatus.COMPLETED.value,
                    JobItemStatus.SKIPPED_DUPLICATE.value,
                    JobItemStatus.SKIPPED_UNAVAILABLE.value,
                    JobItemStatus.FAILED_RETRYABLE.value,
                    JobItemStatus.FAILED_FINAL.value,
                    limit,
                ),
            ).fetchall()
        records: list[JobRecord] = []
        for row in rows:
            try:
                status = JobStatus(str(row[1]))
            except ValueError as exc:
                raise DatabaseError(f"Job {row[0]} has invalid persisted status: {row[1]}") from exc
            records.append(
                JobRecord(
                    job_id=str(row[0]),
                    status=status,
                    source_title=str(row[2]) if row[2] is not None else None,
                    source_key=str(row[3]) if row[3] is not None else None,
                    created_at=str(row[4]),
                    updated_at=str(row[5]),
                    completed_at=str(row[6]) if row[6] is not None else None,
                    total=int(row[7] or 0),
                    completed=int(row[8] or 0),
                    skipped=int(row[9] or 0),
                    retryable_failed=int(row[10] or 0),
                    final_failed=int(row[11] or 0),
                )
            )
        return tuple(records)

    def latest_resumable_job(self) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status IN (?, ?)
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (JobStatus.PAUSED.value, JobStatus.PLANNED.value),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def latest_retryable_job(self) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT DISTINCT j.id
                FROM jobs j
                JOIN job_items ji ON ji.job_id = j.id
                WHERE ji.status = ?
                ORDER BY j.updated_at DESC, j.created_at DESC
                LIMIT 1
                """,
                (JobItemStatus.FAILED_RETRYABLE.value,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def start_job(self, job_id: str) -> None:
        with self.database.transaction() as connection:
            status = self._job_status(connection, job_id)
            if status not in {JobStatus.PLANNED, JobStatus.PAUSED}:
                raise InputError(f"Job {job_id} cannot start from status {status.value}")
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (JobStatus.RUNNING.value, job_id),
            )

    def pause_job(self, job_id: str) -> None:
        with self.database.transaction() as connection:
            status = self._job_status(connection, job_id)
            if status is not JobStatus.RUNNING:
                raise InputError(f"Job {job_id} is not running")
            active = self._active_item_count(connection, job_id)
            if active:
                raise InputError("Cannot pause while an item is in an active processing phase")
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (JobStatus.PAUSED.value, job_id),
            )

    def interrupt_job(self, job_id: str) -> None:
        """Pause a running job after Ctrl+C without consuming a retry attempt."""

        with self.database.transaction() as connection:
            status = self._job_status(connection, job_id)
            if status is not JobStatus.RUNNING:
                return
            transient_values = tuple(status.value for status in _TRANSIENT_ITEM_STATUSES)
            placeholders = ",".join("?" for _ in transient_values)
            connection.execute(
                f"""
                UPDATE job_items
                SET status = ?,
                    attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    retry_after_at = NULL,
                    last_error = 'Interrupted by user',
                    completed_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND status IN ({placeholders})
                """,
                (JobItemStatus.PENDING.value, job_id, *transient_values),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (JobStatus.PAUSED.value, job_id),
            )

    def cancel_job(self, job_id: str) -> None:
        with self.database.transaction() as connection:
            status = self._job_status(connection, job_id)
            if status in {
                JobStatus.COMPLETED,
                JobStatus.COMPLETED_WITH_FAILURES,
                JobStatus.CANCELLED,
            }:
                raise InputError(f"Job {job_id} is already terminal")
            connection.execute(
                """
                UPDATE job_items
                SET status = ?, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND status NOT IN (?, ?, ?, ?, ?)
                """,
                (
                    JobItemStatus.CANCELLED.value,
                    job_id,
                    JobItemStatus.COMPLETED.value,
                    JobItemStatus.SKIPPED_DUPLICATE.value,
                    JobItemStatus.SKIPPED_UNAVAILABLE.value,
                    JobItemStatus.FAILED_FINAL.value,
                    JobItemStatus.CANCELLED.value,
                ),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (JobStatus.CANCELLED.value, job_id),
            )

    def claim_next(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        include_retryable: bool = True,
    ) -> QueueItem | None:
        current = _utc(now)
        with self.database.transaction() as connection:
            if self._job_status(connection, job_id) is not JobStatus.RUNNING:
                raise InputError(f"Job {job_id} must be running before work can be claimed")
            if include_retryable:
                row = connection.execute(
                    """
                    SELECT ji.id, ji.position, ji.attempts, ji.status,
                           m.media_key, m.title, m.url
                    FROM job_items ji
                    JOIN media_items m ON m.id = ji.media_item_id
                    WHERE ji.job_id = ? AND (
                        ji.status = ? OR (
                            ji.status = ? AND (
                                ji.retry_after_at IS NULL OR ji.retry_after_at <= ?
                            )
                        )
                    )
                    ORDER BY ji.position ASC
                    LIMIT 1
                    """,
                    (
                        job_id,
                        JobItemStatus.PENDING.value,
                        JobItemStatus.FAILED_RETRYABLE.value,
                        _format_time(current),
                    ),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT ji.id, ji.position, ji.attempts, ji.status,
                           m.media_key, m.title, m.url
                    FROM job_items ji
                    JOIN media_items m ON m.id = ji.media_item_id
                    WHERE ji.job_id = ? AND ji.status = ?
                    ORDER BY ji.position ASC
                    LIMIT 1
                    """,
                    (job_id, JobItemStatus.PENDING.value),
                ).fetchone()
            if row is None:
                return None
            job_item_id = int(row[0])
            attempts = int(row[2]) + 1
            connection.execute(
                """
                UPDATE job_items
                SET status = ?, attempts = ?, retry_after_at = NULL,
                    last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (JobItemStatus.DOWNLOADING.value, attempts, job_item_id),
            )
            connection.execute(
                """
                UPDATE failures SET resolved_at = CURRENT_TIMESTAMP
                WHERE job_item_id = ? AND resolved_at IS NULL
                """,
                (job_item_id,),
            )
            return QueueItem(
                job_id=job_id,
                job_item_id=job_item_id,
                media_key=str(row[4]),
                title=str(row[5]),
                url=str(row[6]),
                position=int(row[1]),
                attempts=attempts,
            )

    def requeue_due_retryable(self, job_id: str, *, now: datetime | None = None) -> int:
        """Requeue retryable failures whose backoff expired, at most once per executor run."""

        current = _utc(now)
        with self.database.transaction() as connection:
            if self._job_status(connection, job_id) is not JobStatus.RUNNING:
                raise InputError(f"Job {job_id} must be running before retries can be requeued")
            rows = connection.execute(
                """
                SELECT id FROM job_items
                WHERE job_id = ? AND status = ?
                  AND (retry_after_at IS NULL OR retry_after_at <= ?)
                ORDER BY position ASC
                """,
                (
                    job_id,
                    JobItemStatus.FAILED_RETRYABLE.value,
                    _format_time(current),
                ),
            ).fetchall()
            item_ids = [int(row[0]) for row in rows]
            if not item_ids:
                return 0
            placeholders = ",".join("?" for _ in item_ids)
            connection.execute(
                f"UPDATE job_items SET status = ?, retry_after_at = NULL, "
                f"updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                (JobItemStatus.PENDING.value, *item_ids),
            )
            return len(item_ids)

    def set_phase(self, job_item_id: int, phase: JobItemStatus) -> None:
        if phase not in {JobItemStatus.POSTPROCESSING, JobItemStatus.VERIFYING}:
            raise InputError("Only postprocessing or verifying are valid explicit phases")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM job_items WHERE id = ?",
                (job_item_id,),
            ).fetchone()
            if row is None:
                raise InputError(f"Unknown job item: {job_item_id}")
            current = JobItemStatus(str(row[0]))
            allowed_previous = {
                JobItemStatus.POSTPROCESSING: JobItemStatus.DOWNLOADING,
                JobItemStatus.VERIFYING: JobItemStatus.POSTPROCESSING,
            }[phase]
            if current is not allowed_previous:
                raise InputError(f"Cannot move job item from {current.value} to {phase.value}")
            connection.execute(
                "UPDATE job_items SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (phase.value, job_item_id),
            )

    def complete_item(
        self,
        job_item_id: int,
        *,
        status: JobItemStatus = JobItemStatus.COMPLETED,
        output_path: str | None = None,
        detail: str | None = None,
    ) -> JobStatus:
        if status not in {
            JobItemStatus.COMPLETED,
            JobItemStatus.SKIPPED_DUPLICATE,
            JobItemStatus.SKIPPED_UNAVAILABLE,
        }:
            raise InputError("Invalid successful terminal item status")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT job_id, status FROM job_items WHERE id = ?",
                (job_item_id,),
            ).fetchone()
            if row is None:
                raise InputError(f"Unknown job item: {job_item_id}")
            current = JobItemStatus(str(row[1]))
            if current not in _TRANSIENT_ITEM_STATUSES:
                raise InputError(f"Cannot complete item from status {current.value}")
            job_id = str(row[0])
            connection.execute(
                """
                UPDATE job_items
                SET status = ?, output_path = ?, completed_at = CURRENT_TIMESTAMP,
                    last_error = ?, retry_after_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status.value, output_path, detail, job_item_id),
            )
            return self._refresh_job_status(connection, job_id)

    def fail_item(
        self,
        job_item_id: int,
        *,
        category: str,
        message: str,
        retryable: bool,
        policy: RetryPolicy,
        now: datetime | None = None,
    ) -> JobStatus:
        if not category.strip() or not message.strip():
            raise InputError("Failure category and message are required")
        current_time = _utc(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT job_id, status, attempts FROM job_items WHERE id = ?",
                (job_item_id,),
            ).fetchone()
            if row is None:
                raise InputError(f"Unknown job item: {job_item_id}")
            current = JobItemStatus(str(row[1]))
            if current not in _TRANSIENT_ITEM_STATUSES:
                raise InputError(f"Cannot fail item from status {current.value}")
            job_id = str(row[0])
            attempts = int(row[2])
            can_retry = retryable and attempts < policy.max_attempts
            next_status = (
                JobItemStatus.FAILED_RETRYABLE if can_retry else JobItemStatus.FAILED_FINAL
            )
            retry_after = None
            if can_retry:
                retry_after = _format_time(
                    current_time + timedelta(seconds=policy.delay_seconds(attempts))
                )
            connection.execute(
                """
                UPDATE job_items
                SET status = ?, last_error = ?, retry_after_at = ?,
                    completed_at = CASE WHEN ? THEN NULL ELSE CURRENT_TIMESTAMP END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_status.value, message, retry_after, int(can_retry), job_item_id),
            )
            connection.execute(
                """
                INSERT INTO failures(job_item_id, category, retryable, message)
                VALUES (?, ?, ?, ?)
                """,
                (job_item_id, category.strip(), int(can_retry), message.strip()),
            )
            return self._refresh_job_status(connection, job_id)

    def retry_failures(self, job_id: str) -> int:
        with self.database.transaction() as connection:
            self._job_status(connection, job_id)
            rows = connection.execute(
                "SELECT id FROM job_items WHERE job_id = ? AND status = ?",
                (job_id, JobItemStatus.FAILED_RETRYABLE.value),
            ).fetchall()
            item_ids = [int(row[0]) for row in rows]
            if item_ids:
                placeholders = ",".join("?" for _ in item_ids)
                connection.execute(
                    f"UPDATE job_items SET status = ?, retry_after_at = NULL, "
                    f"updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                    (JobItemStatus.PENDING.value, *item_ids),
                )
                connection.execute(
                    f"UPDATE failures SET resolved_at = CURRENT_TIMESTAMP "
                    f"WHERE resolved_at IS NULL AND job_item_id IN ({placeholders})",
                    tuple(item_ids),
                )
            return len(item_ids)

    def recover_unavailable(self, job_id: str) -> int:
        """Requeue only items previously classified as unavailable for one job."""

        with self.database.transaction() as connection:
            status = self._job_status(connection, job_id)
            if status is JobStatus.RUNNING:
                raise InputError(f"Job {job_id} is currently running")
            rows = connection.execute(
                "SELECT id FROM job_items WHERE job_id = ? AND status = ? ORDER BY position",
                (job_id, JobItemStatus.SKIPPED_UNAVAILABLE.value),
            ).fetchall()
            item_ids = [int(row[0]) for row in rows]
            if not item_ids:
                return 0
            placeholders = ",".join("?" for _ in item_ids)
            connection.execute(
                f"""
                UPDATE job_items
                SET status = ?, attempts = 0, retry_after_at = NULL,
                    last_error = NULL, output_path = NULL, completed_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                (JobItemStatus.PENDING.value, *item_ids),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (JobStatus.PAUSED.value, job_id),
            )
            return len(item_ids)

    def recover_interrupted_jobs(self) -> tuple[str, ...]:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at",
                (JobStatus.RUNNING.value,),
            ).fetchall()
            recovered: list[str] = []
            for row in rows:
                job_id = str(row[0])
                connection.execute(
                    """
                    UPDATE job_items
                    SET status = ?, retry_after_at = NULL,
                        last_error = 'Recovered after interrupted process',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = ? AND status IN (?, ?, ?)
                    """,
                    (
                        JobItemStatus.PENDING.value,
                        job_id,
                        JobItemStatus.DOWNLOADING.value,
                        JobItemStatus.POSTPROCESSING.value,
                        JobItemStatus.VERIFYING.value,
                    ),
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (JobStatus.PAUSED.value, job_id),
                )
                recovered.append(job_id)
            return tuple(recovered)

    def item_statuses(self, job_id: str) -> dict[str, JobItemStatus]:
        """Return current per-media job state for safe parallel dependency handling."""

        with self.database.connection() as connection:
            self._job_status(connection, job_id)
            rows = connection.execute(
                """
                SELECT m.media_key, ji.status
                FROM job_items ji
                JOIN media_items m ON m.id = ji.media_item_id
                WHERE ji.job_id = ?
                ORDER BY ji.position ASC
                """,
                (job_id,),
            ).fetchall()
        try:
            return {str(row[0]): JobItemStatus(str(row[1])) for row in rows}
        except ValueError as exc:
            raise DatabaseError(f"Job {job_id} contains an invalid item status") from exc

    def job_breakdown(self, job_id: str) -> JobBreakdown:
        """Return exact persisted per-status counts for one collection job."""

        with self.database.connection() as connection:
            status = self._job_status(connection, job_id)
            rows = connection.execute(
                """
                SELECT status, COUNT(*) FROM job_items
                WHERE job_id = ? GROUP BY status
                """,
                (job_id,),
            ).fetchall()
            unavailable_rows = connection.execute(
                """
                SELECT COALESCE(NULLIF(last_error, ''), 'reason not recorded'), COUNT(*)
                FROM job_items
                WHERE job_id = ? AND status = ?
                GROUP BY COALESCE(NULLIF(last_error, ''), 'reason not recorded')
                ORDER BY COUNT(*) DESC, COALESCE(NULLIF(last_error, ''), 'reason not recorded')
                """,
                (job_id, JobItemStatus.SKIPPED_UNAVAILABLE.value),
            ).fetchall()
        counts = {str(row[0]): int(row[1]) for row in rows}
        return JobBreakdown(
            job_id=job_id,
            status=status,
            total=sum(counts.values()),
            pending=counts.get(JobItemStatus.PENDING.value, 0),
            downloading=counts.get(JobItemStatus.DOWNLOADING.value, 0),
            postprocessing=counts.get(JobItemStatus.POSTPROCESSING.value, 0),
            verifying=counts.get(JobItemStatus.VERIFYING.value, 0),
            completed=counts.get(JobItemStatus.COMPLETED.value, 0),
            skipped_duplicate=counts.get(JobItemStatus.SKIPPED_DUPLICATE.value, 0),
            skipped_unavailable=counts.get(JobItemStatus.SKIPPED_UNAVAILABLE.value, 0),
            retryable_failed=counts.get(JobItemStatus.FAILED_RETRYABLE.value, 0),
            final_failed=counts.get(JobItemStatus.FAILED_FINAL.value, 0),
            cancelled=counts.get(JobItemStatus.CANCELLED.value, 0),
            unavailable_reasons=tuple((str(row[0]), int(row[1])) for row in unavailable_rows),
        )

    def progress(self, job_id: str) -> JobProgress:
        breakdown = self.job_breakdown(job_id)
        return JobProgress(
            job_id=job_id,
            status=breakdown.status,
            total=breakdown.total,
            pending=breakdown.pending,
            active=breakdown.downloading + breakdown.postprocessing + breakdown.verifying,
            completed=breakdown.completed,
            skipped=breakdown.skipped_duplicate + breakdown.skipped_unavailable,
            retryable_failed=breakdown.retryable_failed,
            final_failed=breakdown.final_failed,
            cancelled=breakdown.cancelled,
        )

    def finalize_job(self, job_id: str) -> JobStatus:
        with self.database.transaction() as connection:
            return self._refresh_job_status(connection, job_id)

    @staticmethod
    def _ensure_source(connection: object, plan: DownloadPlan) -> int:
        row = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT id FROM sources
            WHERE platform = ? AND source_type = ? AND source_key = ?
            """,
            (plan.source.platform, plan.source.kind, plan.source.source_key),
        ).fetchone()
        if row is not None:
            return int(row[0])
        cursor = connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO sources(platform, source_type, source_key, url, title)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                plan.source.platform,
                plan.source.kind,
                plan.source.source_key,
                plan.source.url,
                plan.source.title,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _ensure_media(connection: object, source_id: int, item: PlanItem) -> int:
        row = connection.execute(  # type: ignore[attr-defined]
            "SELECT id FROM media_items WHERE platform = 'youtube' AND media_key = ?",
            (item.media_key,),
        ).fetchone()
        if row is None:
            cursor = connection.execute(  # type: ignore[attr-defined]
                """
                INSERT INTO media_items(
                    platform, media_key, source_id, url, title, upload_date,
                    duration_seconds, media_type
                ) VALUES ('youtube', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.media_key,
                    source_id,
                    item.url,
                    item.title,
                    item.upload_date,
                    item.duration_seconds,
                    item.media_type,
                ),
            )
            media_id = int(cursor.lastrowid)
        else:
            media_id = int(row[0])
        if item.view_count is not None or item.like_count is not None:
            connection.execute(  # type: ignore[attr-defined]
                """
                INSERT INTO media_stats(media_item_id, view_count, like_count)
                VALUES (?, ?, ?)
                ON CONFLICT(media_item_id) DO UPDATE SET
                    view_count = excluded.view_count,
                    like_count = excluded.like_count,
                    fetched_at = CURRENT_TIMESTAMP
                """,
                (media_id, item.view_count, item.like_count),
            )
        return media_id

    @staticmethod
    def _job_status(connection: object, job_id: str) -> JobStatus:
        row = connection.execute(  # type: ignore[attr-defined]
            "SELECT status FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise InputError(f"Unknown download job: {job_id}")
        try:
            return JobStatus(str(row[0]))
        except ValueError as exc:
            raise DatabaseError(f"Job {job_id} has invalid persisted status: {row[0]}") from exc

    @staticmethod
    def _active_item_count(connection: object, job_id: str) -> int:
        placeholders = ",".join("?" for _ in _TRANSIENT_ITEM_STATUSES)
        row = connection.execute(  # type: ignore[attr-defined]
            f"SELECT COUNT(*) FROM job_items WHERE job_id = ? AND status IN ({placeholders})",
            (job_id, *(status.value for status in _TRANSIENT_ITEM_STATUSES)),
        ).fetchone()
        return int(row[0])

    @classmethod
    def _refresh_job_status(cls, connection: object, job_id: str) -> JobStatus:
        current = cls._job_status(connection, job_id)
        if current is JobStatus.CANCELLED:
            return current
        rows = connection.execute(  # type: ignore[attr-defined]
            "SELECT status, COUNT(*) FROM job_items WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
        counts = {JobItemStatus(str(row[0])): int(row[1]) for row in rows}
        unfinished = sum(
            count for status, count in counts.items() if status not in _TERMINAL_ITEM_STATUSES
        )
        if unfinished:
            return current
        final_status = (
            JobStatus.COMPLETED_WITH_FAILURES
            if counts.get(JobItemStatus.FAILED_FINAL, 0)
            else JobStatus.COMPLETED
        )
        connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE jobs SET status = ?, completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """,
            (final_status.value, job_id),
        )
        return final_status


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
