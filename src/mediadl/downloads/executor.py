"""Persistent collection-job execution bridge."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediadl.core.errors import DownloadError, InputError, MediaDLError, UserCancelledError
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.dedupe.basic import BasicDedupeService, BasicDuplicateKind
from mediadl.dedupe.candidates import CandidatePair, CandidateScreener
from mediadl.dedupe.service import SmartDedupeService
from mediadl.dedupe.smart import SmartDedupePolicy
from mediadl.downloads.audio_pipeline import AudioConversionCancelled, AudioConversionService
from mediadl.downloads.disk_guard import DiskSpaceGuard
from mediadl.downloads.jobs import (
    JobItemStatus,
    JobRepository,
    JobStatus,
    QueueItem,
    RetryPolicy,
)
from mediadl.downloads.plan import DownloadPlan
from mediadl.downloads.single import (
    DownloadReceipt,
    SingleDownloadRequest,
    SingleDownloadService,
    StagedDownloadReceipt,
)
from mediadl.storage.filenames import MediaFolder, collection_directory


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    job_id: str
    status: JobStatus
    completed: int
    skipped: int
    failed: int
    skipped_duplicate: int = 0
    skipped_unavailable: int = 0


DiskGuardFactory = Callable[[Path], DiskSpaceGuard]
ProgressObserver = Callable[[str, int, int, Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class _StagedAudioWork:
    claimed: QueueItem
    staged: StagedDownloadReceipt
    candidates: tuple[CandidatePair, ...]
    disk_guard: DiskSpaceGuard


@dataclass(frozen=True, slots=True)
class _ConvertedAudioWork:
    claimed: QueueItem
    receipt: DownloadReceipt
    candidates: tuple[CandidatePair, ...]


class _ParallelCancelled(Exception):
    """Internal cooperative stop used by parallel collection workers."""


class _BatchProgressState:
    """Collapse many worker updates into one stable overall collection status."""

    def __init__(
        self,
        observer: ProgressObserver | None,
        *,
        total_items: int,
        finished_items: int,
    ) -> None:
        self.observer = observer
        self.total_items = max(1, total_items)
        self.finished_items = max(0, finished_items)
        self.active: dict[int, str] = {}
        self.lock = threading.Lock()

    def preparing(self, position: int) -> None:
        self._set(position, "preparing")

    def transfer(self, position: int, status: Mapping[str, Any]) -> None:
        state = str(status.get("status") or "").casefold()
        if state == "finished":
            self._set(position, "processing")
        elif state == "downloading":
            self._set(position, "downloading")

    def processing(self, position: int) -> None:
        self._set(position, "processing")

    def converting(self, position: int) -> None:
        self._set(position, "converting")

    def finished(self, position: int) -> None:
        with self.lock:
            self.active.pop(position, None)
            self.finished_items = min(self.total_items, self.finished_items + 1)
            self._emit_locked()

    def released(self, position: int) -> None:
        with self.lock:
            self.active.pop(position, None)
            self._emit_locked()

    def _set(self, position: int, phase: str) -> None:
        with self.lock:
            self.active[position] = phase
            self._emit_locked()

    def _emit_locked(self) -> None:
        if self.observer is None:
            return
        phases = tuple(self.active.values())
        self.observer(
            "Collection",
            self.finished_items,
            self.total_items,
            {
                "status": "batch",
                "completed_items": self.finished_items,
                "active_items": len(phases),
                "preparing_items": phases.count("preparing"),
                "downloading_items": phases.count("downloading"),
                "processing_items": phases.count("processing"),
                "converting_items": phases.count("converting"),
            },
        )


class CollectionJobExecutor:
    """Execute one frozen persistent plan safely, sequentially or with bounded workers."""

    def __init__(
        self,
        jobs: JobRepository,
        downloader: SingleDownloadService,
        dedupe: BasicDedupeService,
        *,
        retry_policy: RetryPolicy | None = None,
        smart_dedupe: SmartDedupeService | None = None,
        disk_guard_factory: DiskGuardFactory = DiskSpaceGuard,
        progress_observer: ProgressObserver | None = None,
        max_workers: int = 1,
        conversion_workers: int = 2,
        audio_converter: AudioConversionService | None = None,
    ) -> None:
        if max_workers < 1 or max_workers > 8:
            raise InputError("Collection worker count must be between 1 and 8")
        if conversion_workers < 1 or conversion_workers > 4:
            raise InputError("Collection conversion worker count must be between 1 and 4")
        self.jobs = jobs
        self.downloader = downloader
        self.dedupe = dedupe
        self.retry_policy = retry_policy or RetryPolicy()
        self.smart_dedupe = smart_dedupe
        self.disk_guard_factory = disk_guard_factory
        self.progress_observer = progress_observer
        self.max_workers = max_workers
        self.conversion_workers = conversion_workers
        self.audio_converter = audio_converter or AudioConversionService()

    def run(self, job_id: str) -> ExecutionSummary:
        plan = self.jobs.load_plan(job_id)
        progress = self.jobs.progress(job_id)
        if progress.status in {JobStatus.PLANNED, JobStatus.PAUSED}:
            self.jobs.start_job(job_id)
        elif progress.status not in {JobStatus.RUNNING}:
            return self._summary(job_id)

        output_format = OutputFormat(plan.output_format)
        dedupe_mode = DedupeMode(plan.dedupe_mode)
        if self.max_workers > 1 and plan.item_count > 1:
            if output_format in {OutputFormat.MP3, OutputFormat.FLAC, OutputFormat.WAV}:
                return self._run_audio_pipeline(job_id, plan, output_format, dedupe_mode)
            return self._run_parallel(job_id, plan, output_format, dedupe_mode)

        item_by_key = {item.media_key: item for item in plan.items}
        screener = (
            CandidateScreener()
            if self.smart_dedupe is not None and dedupe_mode is not DedupeMode.OFF
            else None
        )
        screened_through = -1

        while True:
            claimed = self.jobs.claim_next(job_id)
            if claimed is None:
                break
            item = item_by_key[claimed.media_key]
            candidate_pairs: tuple[CandidatePair, ...] = ()
            if screener is not None and claimed.position > screened_through:
                for position in range(screened_through + 1, claimed.position + 1):
                    pairs = screener.consider(plan.items[position])
                    if position == claimed.position:
                        candidate_pairs = pairs
                screened_through = claimed.position

            existing = (
                self.dedupe.check_source_profile(
                    media_key=claimed.media_key,
                    output_format=output_format,
                    quality=plan.quality,
                )
                if dedupe_mode is not DedupeMode.OFF
                else None
            )
            if existing is not None and existing.is_duplicate:
                status = self.jobs.complete_item(
                    claimed.job_item_id,
                    status=JobItemStatus.SKIPPED_DUPLICATE,
                    output_path=existing.path,
                )
                if _is_terminal(status):
                    break
                continue

            output_dir = _item_output_directory(
                Path(plan.output_dir),
                plan.source.title or plan.source.source_key,
                item.media_type,
                output_format,
            )
            request = SingleDownloadRequest(
                url=claimed.url,
                output_dir=output_dir,
                output_format=output_format,
                quality=plan.quality,
            )
            disk_guard = self.disk_guard_factory(output_dir)

            try:
                disk_guard.ensure_space()
                if self.progress_observer is not None:
                    self.progress_observer(
                        item.title,
                        claimed.position + 1,
                        plan.item_count,
                        {"status": "processing", "phase": "Preparing…"},
                    )
                transfer_hook = self._progress_hook(
                    disk_guard,
                    item.title,
                    claimed.position + 1,
                    plan.item_count,
                )
                post_hook = self._postprocessor_hook(
                    item.title,
                    claimed.position + 1,
                    plan.item_count,
                    output_format,
                )
                if post_hook is None:
                    receipt = self.downloader.download(
                        request,
                        progress_hook=transfer_hook,
                    )
                else:
                    receipt = self.downloader.download(
                        request,
                        progress_hook=transfer_hook,
                        postprocessor_hook=post_hook,
                    )
                item_status, recorded_path = self._register_download(
                    media_key=claimed.media_key,
                    job_item_id=claimed.job_item_id,
                    output_format=output_format,
                    quality=receipt.quality,
                    output_path=receipt.output_path,
                    candidates=candidate_pairs,
                    dedupe_mode=dedupe_mode,
                )
                status = self.jobs.complete_item(
                    claimed.job_item_id,
                    status=item_status,
                    output_path=recorded_path,
                )
                if _is_terminal(status):
                    break
            except KeyboardInterrupt:
                self.jobs.interrupt_job(job_id)
                raise UserCancelledError(
                    f"Cancelled by user. Resume later with: mdl resume {job_id}"
                ) from None
            except DownloadError as exc:
                if exc.category in {
                    "private",
                    "deleted",
                    "geo_blocked",
                    "auth_required",
                    "age_restricted",
                    "drm",
                    "unavailable",
                }:
                    status = self.jobs.complete_item(
                        claimed.job_item_id,
                        status=JobItemStatus.SKIPPED_UNAVAILABLE,
                        detail=f"{exc.category}: {exc}",
                    )
                    if _is_terminal(status):
                        break
                    continue
                status = self.jobs.fail_item(
                    claimed.job_item_id,
                    category=exc.category,
                    message=str(exc),
                    retryable=exc.retryable,
                    policy=self.retry_policy,
                )
                if _is_terminal(status):
                    break
            except MediaDLError as exc:
                status = self.jobs.fail_item(
                    claimed.job_item_id,
                    category="local",
                    message=str(exc),
                    retryable=False,
                    policy=self.retry_policy,
                )
                if _is_terminal(status):
                    break

        progress = self.jobs.progress(job_id)
        if progress.retryable_failed and progress.status is JobStatus.RUNNING:
            self.jobs.pause_job(job_id)
        else:
            self.jobs.finalize_job(job_id)
        return self._summary(job_id)

    def _run_audio_pipeline(
        self,
        job_id: str,
        plan: DownloadPlan,
        output_format: OutputFormat,
        dedupe_mode: DedupeMode,
    ) -> ExecutionSummary:
        """Pipeline heavy audio as bounded downloads -> conversions -> registration."""

        item_by_key = {item.media_key: item for item in plan.items}
        candidate_pairs = self._parallel_candidate_pairs(plan, dedupe_mode)
        self.jobs.requeue_due_retryable(job_id)
        statuses = self.jobs.item_statuses(job_id)
        resolved_positions = {
            position
            for position, item in enumerate(plan.items)
            if _is_terminal_item_status(statuses[item.media_key])
            or statuses[item.media_key] is JobItemStatus.FAILED_RETRYABLE
        }
        batch_progress = _BatchProgressState(
            self.progress_observer,
            total_items=plan.item_count,
            finished_items=self.jobs.progress(job_id).finished,
        )

        download_workers = min(self.max_workers, plan.item_count)
        conversion_workers = min(self.conversion_workers, plan.item_count)
        queue_bound = max(2, download_workers, conversion_workers * 2)
        staged_queue: queue.Queue[_StagedAudioWork] = queue.Queue(maxsize=queue_bound)
        converted_queue: queue.Queue[_ConvertedAudioWork] = queue.Queue(maxsize=queue_bound)

        stop_event = threading.Event()
        downloads_done = threading.Event()
        conversions_done = threading.Event()
        state_changed = threading.Event()
        resolved_lock = threading.Lock()
        error_lock = threading.Lock()
        counter_lock = threading.Lock()
        worker_errors: list[BaseException] = []
        remaining_download_workers = download_workers
        remaining_conversion_workers = conversion_workers

        def mark_resolved(position: int) -> None:
            with resolved_lock:
                resolved_positions.add(position)
            state_changed.set()

        def is_resolved(position: int) -> bool:
            with resolved_lock:
                return position in resolved_positions

        def record_worker_error(exc: BaseException) -> None:
            stop_event.set()
            with error_lock:
                worker_errors.append(exc)
            state_changed.set()

        def queue_put(target: queue.Queue[Any], value: Any) -> None:
            while True:
                if stop_event.is_set():
                    raise _ParallelCancelled
                try:
                    target.put(value, timeout=0.10)
                    return
                except queue.Full:
                    continue

        def resolve_download_failure(claimed: QueueItem, exc: DownloadError) -> None:
            if exc.category in {
                "private",
                "deleted",
                "geo_blocked",
                "auth_required",
                "age_restricted",
                "drm",
                "unavailable",
            }:
                self.jobs.complete_item(
                    claimed.job_item_id,
                    status=JobItemStatus.SKIPPED_UNAVAILABLE,
                    detail=f"{exc.category}: {exc}",
                )
                batch_progress.finished(claimed.position)
                mark_resolved(claimed.position)
                return
            retryable = exc.retryable and claimed.attempts < self.retry_policy.max_attempts
            self.jobs.fail_item(
                claimed.job_item_id,
                category=exc.category,
                message=str(exc),
                retryable=exc.retryable,
                policy=self.retry_policy,
            )
            if retryable:
                batch_progress.released(claimed.position)
            else:
                batch_progress.finished(claimed.position)
            mark_resolved(claimed.position)

        def make_transfer_hook(
            disk_guard: DiskSpaceGuard,
            position: int,
        ) -> Callable[[dict[str, Any]], None]:
            def hook(status: dict[str, Any]) -> None:
                self._raise_if_stopped(stop_event)
                disk_guard.progress_hook(status)
                batch_progress.transfer(position, status)

            return hook

        def download_worker() -> None:
            nonlocal remaining_download_workers
            try:
                while not stop_event.is_set():
                    try:
                        claimed = self.jobs.claim_next(job_id, include_retryable=False)
                    except InputError:
                        if _is_terminal(self.jobs.progress(job_id).status):
                            return
                        raise
                    if claimed is None:
                        return
                    item = item_by_key[claimed.media_key]
                    position = claimed.position
                    try:
                        existing = (
                            self.dedupe.check_source_profile(
                                media_key=claimed.media_key,
                                output_format=output_format,
                                quality=plan.quality,
                            )
                            if dedupe_mode is not DedupeMode.OFF
                            else None
                        )
                        if existing is not None and existing.is_duplicate:
                            self.jobs.complete_item(
                                claimed.job_item_id,
                                status=JobItemStatus.SKIPPED_DUPLICATE,
                                output_path=existing.path,
                            )
                            batch_progress.finished(position)
                            mark_resolved(position)
                            continue

                        output_dir = _item_output_directory(
                            Path(plan.output_dir),
                            plan.source.title or plan.source.source_key,
                            item.media_type,
                            output_format,
                        )
                        request = SingleDownloadRequest(
                            url=claimed.url,
                            output_dir=output_dir,
                            output_format=output_format,
                            quality=plan.quality,
                        )
                        disk_guard = self.disk_guard_factory(output_dir)
                        batch_progress.preparing(position)
                        disk_guard.ensure_space()

                        transfer_hook = make_transfer_hook(disk_guard, position)
                        staged = self.downloader.download_source(
                            request,
                            progress_hook=transfer_hook,
                        )
                        self._raise_if_stopped(stop_event)
                        self.jobs.set_phase(
                            claimed.job_item_id,
                            JobItemStatus.POSTPROCESSING,
                        )
                        batch_progress.processing(position)
                        queue_put(
                            staged_queue,
                            _StagedAudioWork(
                                claimed=claimed,
                                staged=staged,
                                candidates=candidate_pairs.get(position, ()),
                                disk_guard=disk_guard,
                            ),
                        )
                    except _ParallelCancelled:
                        batch_progress.released(position)
                        return
                    except DownloadError as exc:
                        if stop_event.is_set():
                            batch_progress.released(position)
                            return
                        resolve_download_failure(claimed, exc)
                    except MediaDLError as exc:
                        if stop_event.is_set():
                            batch_progress.released(position)
                            return
                        self.jobs.fail_item(
                            claimed.job_item_id,
                            category="local",
                            message=str(exc),
                            retryable=False,
                            policy=self.retry_policy,
                        )
                        batch_progress.finished(position)
                        mark_resolved(position)
            except BaseException as exc:
                record_worker_error(exc)
            finally:
                with counter_lock:
                    remaining_download_workers -= 1
                    if remaining_download_workers == 0:
                        downloads_done.set()
                        state_changed.set()

        def conversion_worker() -> None:
            nonlocal remaining_conversion_workers
            try:
                while True:
                    if stop_event.is_set() and staged_queue.empty():
                        return
                    try:
                        work = staged_queue.get(timeout=0.10)
                    except queue.Empty:
                        if downloads_done.is_set():
                            return
                        continue
                    try:
                        if stop_event.is_set():
                            batch_progress.released(work.claimed.position)
                            continue
                        batch_progress.converting(work.claimed.position)
                        receipt = self.audio_converter.convert(
                            work.staged,
                            disk_guard=work.disk_guard,
                            stop_event=stop_event,
                        )
                        queue_put(
                            converted_queue,
                            _ConvertedAudioWork(
                                claimed=work.claimed,
                                receipt=receipt,
                                candidates=work.candidates,
                            ),
                        )
                    except (AudioConversionCancelled, _ParallelCancelled):
                        batch_progress.released(work.claimed.position)
                        return
                    except DownloadError as exc:
                        if stop_event.is_set():
                            batch_progress.released(work.claimed.position)
                            return
                        resolve_download_failure(work.claimed, exc)
                    except MediaDLError as exc:
                        if stop_event.is_set():
                            batch_progress.released(work.claimed.position)
                            return
                        self.jobs.fail_item(
                            work.claimed.job_item_id,
                            category="local",
                            message=str(exc),
                            retryable=False,
                            policy=self.retry_policy,
                        )
                        batch_progress.finished(work.claimed.position)
                        mark_resolved(work.claimed.position)
                    finally:
                        staged_queue.task_done()
            except BaseException as exc:
                record_worker_error(exc)
            finally:
                with counter_lock:
                    remaining_conversion_workers -= 1
                    if remaining_conversion_workers == 0:
                        conversions_done.set()
                        state_changed.set()

        def finalize_converted(work: _ConvertedAudioWork) -> None:
            position = work.claimed.position
            try:
                item_status, recorded_path = self._register_download(
                    media_key=work.claimed.media_key,
                    job_item_id=work.claimed.job_item_id,
                    output_format=output_format,
                    quality=work.receipt.quality,
                    output_path=work.receipt.output_path,
                    candidates=work.candidates,
                    dedupe_mode=dedupe_mode,
                )
                self.jobs.complete_item(
                    work.claimed.job_item_id,
                    status=item_status,
                    output_path=recorded_path,
                )
                batch_progress.finished(position)
            except MediaDLError as exc:
                self.jobs.fail_item(
                    work.claimed.job_item_id,
                    category="local",
                    message=str(exc),
                    retryable=False,
                    policy=self.retry_policy,
                )
                batch_progress.finished(position)
            finally:
                mark_resolved(position)

        def finalizer_worker() -> None:
            ready: dict[int, _ConvertedAudioWork] = {}
            next_position = 0
            try:
                while next_position < plan.item_count:
                    progressed = False
                    while len(ready) < queue_bound:
                        try:
                            work = converted_queue.get_nowait()
                        except queue.Empty:
                            break
                        ready[work.claimed.position] = work
                        converted_queue.task_done()
                        progressed = True

                    while next_position < plan.item_count:
                        if is_resolved(next_position):
                            next_position += 1
                            progressed = True
                            continue
                        work = ready.pop(next_position, None)
                        if work is None:
                            break
                        finalize_converted(work)
                        next_position += 1
                        progressed = True

                    if next_position >= plan.item_count:
                        return
                    if stop_event.is_set() and not progressed:
                        return
                    if (
                        conversions_done.is_set()
                        and converted_queue.empty()
                        and not ready
                        and not is_resolved(next_position)
                    ):
                        if stop_event.is_set():
                            return
                        raise RuntimeError(
                            f"Audio pipeline finished without resolving item {next_position + 1}"
                        )
                    if progressed:
                        continue
                    try:
                        work = converted_queue.get(timeout=0.10)
                    except queue.Empty:
                        state_changed.wait(timeout=0.10)
                        state_changed.clear()
                        continue
                    ready[work.claimed.position] = work
                    converted_queue.task_done()
            except BaseException as exc:
                record_worker_error(exc)

        download_threads = [
            threading.Thread(
                target=download_worker,
                name=f"mediadl-audio-download-{index + 1}",
            )
            for index in range(download_workers)
        ]
        conversion_threads = [
            threading.Thread(
                target=conversion_worker,
                name=f"mediadl-audio-convert-{index + 1}",
            )
            for index in range(conversion_workers)
        ]
        finalizer = threading.Thread(
            target=finalizer_worker,
            name="mediadl-audio-finalize",
        )
        threads = [finalizer, *conversion_threads, *download_threads]

        try:
            for thread in threads:
                thread.start()
            self._wait_for_pipeline_threads(threads, stop_event, worker_errors)
        except KeyboardInterrupt:
            stop_event.set()
            state_changed.set()
            self._join_all_workers(threads)
            self.jobs.interrupt_job(job_id)
            raise UserCancelledError(
                f"Cancelled by user. Resume later with: mdl resume {job_id}"
            ) from None

        if worker_errors:
            stop_event.set()
            state_changed.set()
            self._join_all_workers(threads)
            self.jobs.interrupt_job(job_id)
            error = worker_errors[0]
            if isinstance(error, KeyboardInterrupt):
                raise UserCancelledError(
                    f"Cancelled by user. Resume later with: mdl resume {job_id}"
                ) from None
            raise error

        progress = self.jobs.progress(job_id)
        if progress.retryable_failed and progress.status is JobStatus.RUNNING:
            self.jobs.pause_job(job_id)
        else:
            self.jobs.finalize_job(job_id)
        return self._summary(job_id)

    def _run_parallel(
        self,
        job_id: str,
        plan: DownloadPlan,
        output_format: OutputFormat,
        dedupe_mode: DedupeMode,
    ) -> ExecutionSummary:
        """Run independent collection items concurrently while keeping job state serialized."""

        item_by_key = {item.media_key: item for item in plan.items}
        candidate_pairs = self._parallel_candidate_pairs(plan, dedupe_mode)
        completion_events = {
            item.media_key: threading.Event()
            for item in plan.items
        }
        for media_key, status in self.jobs.item_statuses(job_id).items():
            if _is_terminal_item_status(status):
                completion_events[media_key].set()

        batch_progress = _BatchProgressState(
            self.progress_observer,
            total_items=plan.item_count,
            finished_items=self.jobs.progress(job_id).finished,
        )
        stop_event = threading.Event()
        registration_lock = threading.Lock()
        error_lock = threading.Lock()
        worker_errors: list[BaseException] = []

        def worker() -> None:
            try:
                while not stop_event.is_set():
                    try:
                        claimed = self.jobs.claim_next(job_id)
                    except InputError:
                        if _is_terminal(self.jobs.progress(job_id).status):
                            return
                        raise
                    if claimed is None:
                        return
                    self._process_parallel_claim(
                        plan=plan,
                        claimed=claimed,
                        item_by_key=item_by_key,
                        output_format=output_format,
                        dedupe_mode=dedupe_mode,
                        candidates=candidate_pairs.get(claimed.position, ()),
                        completion_events=completion_events,
                        batch_progress=batch_progress,
                        stop_event=stop_event,
                        registration_lock=registration_lock,
                    )
            except _ParallelCancelled:
                return
            except BaseException as exc:
                stop_event.set()
                with error_lock:
                    worker_errors.append(exc)

        worker_count = min(self.max_workers, plan.item_count)
        threads = [
            threading.Thread(
                target=worker,
                name=f"mediadl-download-{index + 1}",
            )
            for index in range(worker_count)
        ]

        try:
            for thread in threads:
                thread.start()
            self._wait_for_pipeline_threads(threads, stop_event, worker_errors)
        except KeyboardInterrupt:
            stop_event.set()
            self._join_all_workers(threads)
            self.jobs.interrupt_job(job_id)
            raise UserCancelledError(
                f"Cancelled by user. Resume later with: mdl resume {job_id}"
            ) from None

        if worker_errors:
            stop_event.set()
            self._join_all_workers(threads)
            self.jobs.interrupt_job(job_id)
            error = worker_errors[0]
            if isinstance(error, KeyboardInterrupt):
                raise UserCancelledError(
                    f"Cancelled by user. Resume later with: mdl resume {job_id}"
                ) from None
            raise error

        progress = self.jobs.progress(job_id)
        if progress.retryable_failed and progress.status is JobStatus.RUNNING:
            self.jobs.pause_job(job_id)
        else:
            self.jobs.finalize_job(job_id)
        return self._summary(job_id)

    def _parallel_candidate_pairs(
        self,
        plan: DownloadPlan,
        dedupe_mode: DedupeMode,
    ) -> dict[int, tuple[CandidatePair, ...]]:
        if self.smart_dedupe is None or dedupe_mode is DedupeMode.OFF:
            return {}
        screener = CandidateScreener()
        return {
            position: screener.consider(item)
            for position, item in enumerate(plan.items)
        }

    def _process_parallel_claim(
        self,
        *,
        plan: DownloadPlan,
        claimed: QueueItem,
        item_by_key: dict[str, Any],
        output_format: OutputFormat,
        dedupe_mode: DedupeMode,
        candidates: tuple[CandidatePair, ...],
        completion_events: dict[str, threading.Event],
        batch_progress: _BatchProgressState,
        stop_event: threading.Event,
        registration_lock: threading.Lock,
    ) -> None:
        item = item_by_key[claimed.media_key]
        position = claimed.position
        resolved_event = completion_events[claimed.media_key]
        try:
            self._raise_if_stopped(stop_event)
            existing = (
                self.dedupe.check_source_profile(
                    media_key=claimed.media_key,
                    output_format=output_format,
                    quality=plan.quality,
                )
                if dedupe_mode is not DedupeMode.OFF
                else None
            )
            if existing is not None and existing.is_duplicate:
                self._raise_if_stopped(stop_event)
                self.jobs.complete_item(
                    claimed.job_item_id,
                    status=JobItemStatus.SKIPPED_DUPLICATE,
                    output_path=existing.path,
                )
                batch_progress.finished(position)
                return

            output_dir = _item_output_directory(
                Path(plan.output_dir),
                plan.source.title or plan.source.source_key,
                item.media_type,
                output_format,
            )
            request = SingleDownloadRequest(
                url=claimed.url,
                output_dir=output_dir,
                output_format=output_format,
                quality=plan.quality,
            )
            disk_guard = self.disk_guard_factory(output_dir)
            batch_progress.preparing(position)
            disk_guard.ensure_space()

            def transfer_hook(status: dict[str, Any]) -> None:
                self._raise_if_stopped(stop_event)
                disk_guard.progress_hook(status)
                batch_progress.transfer(position, status)

            postprocessing_started = False

            def postprocessor_hook(status: dict[str, Any]) -> None:
                nonlocal postprocessing_started
                self._raise_if_stopped(stop_event)
                state = str(status.get("status") or "").casefold()
                if state not in {"started", "processing"}:
                    return
                if not postprocessing_started:
                    self.jobs.set_phase(claimed.job_item_id, JobItemStatus.POSTPROCESSING)
                    postprocessing_started = True
                batch_progress.processing(position)

            receipt = self.downloader.download(
                request,
                progress_hook=transfer_hook,
                postprocessor_hook=postprocessor_hook,
            )
            self._raise_if_stopped(stop_event)
            self._wait_for_parallel_candidates(
                candidates,
                completion_events,
                stop_event,
            )

            with registration_lock:
                self._raise_if_stopped(stop_event)
                item_status, recorded_path = self._register_download(
                    media_key=claimed.media_key,
                    job_item_id=claimed.job_item_id,
                    output_format=output_format,
                    quality=receipt.quality,
                    output_path=receipt.output_path,
                    candidates=candidates,
                    dedupe_mode=dedupe_mode,
                )
                self.jobs.complete_item(
                    claimed.job_item_id,
                    status=item_status,
                    output_path=recorded_path,
                )
            batch_progress.finished(position)
        except _ParallelCancelled:
            batch_progress.released(position)
            raise
        except DownloadError as exc:
            if stop_event.is_set():
                batch_progress.released(position)
                raise _ParallelCancelled from None
            if exc.category in {
                "private",
                "deleted",
                "geo_blocked",
                "auth_required",
                "age_restricted",
                "drm",
                "unavailable",
            }:
                self.jobs.complete_item(
                    claimed.job_item_id,
                    status=JobItemStatus.SKIPPED_UNAVAILABLE,
                    detail=f"{exc.category}: {exc}",
                )
                batch_progress.finished(position)
                return
            retryable = exc.retryable and claimed.attempts < self.retry_policy.max_attempts
            self.jobs.fail_item(
                claimed.job_item_id,
                category=exc.category,
                message=str(exc),
                retryable=exc.retryable,
                policy=self.retry_policy,
            )
            if retryable:
                batch_progress.released(position)
            else:
                batch_progress.finished(position)
        except MediaDLError as exc:
            if stop_event.is_set():
                batch_progress.released(position)
                raise _ParallelCancelled from None
            self.jobs.fail_item(
                claimed.job_item_id,
                category="local",
                message=str(exc),
                retryable=False,
                policy=self.retry_policy,
            )
            batch_progress.finished(position)
        finally:
            resolved_event.set()

    @staticmethod
    def _wait_for_pipeline_threads(
        threads: list[threading.Thread],
        stop_event: threading.Event,
        worker_errors: list[BaseException],
    ) -> None:
        """Wait for cooperative workers while keeping Ctrl+C responsive."""

        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.10)
            if worker_errors:
                stop_event.set()

    @staticmethod
    def _join_all_workers(threads: list[threading.Thread]) -> None:
        """Never return while a collection worker can still mutate/download in background."""

        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.10)

    @staticmethod
    def _raise_if_stopped(stop_event: threading.Event) -> None:
        if stop_event.is_set():
            raise _ParallelCancelled

    def _wait_for_parallel_candidates(
        self,
        candidates: tuple[CandidatePair, ...],
        completion_events: dict[str, threading.Event],
        stop_event: threading.Event,
    ) -> None:
        for candidate in candidates:
            event = completion_events.get(candidate.left_media_key)
            if event is None:
                continue
            while not event.wait(timeout=0.10):
                self._raise_if_stopped(stop_event)
        self._raise_if_stopped(stop_event)

    def _progress_hook(
        self,
        disk_guard: DiskSpaceGuard,
        title: str,
        position: int,
        total_items: int,
    ) -> Callable[[dict[str, Any]], None]:
        def hook(status: dict[str, Any]) -> None:
            disk_guard.progress_hook(status)
            if self.progress_observer is not None:
                self.progress_observer(title, position, total_items, status)

        return hook

    def _postprocessor_hook(
        self,
        title: str,
        position: int,
        total_items: int,
        output_format: OutputFormat,
    ) -> Callable[[dict[str, Any]], None] | None:
        if self.progress_observer is None:
            return None

        phase = (
            f"Converting {output_format.value.upper()}…"
            if output_format.is_audio
            else f"Finalizing {output_format.value.upper()}…"
        )

        def hook(status: dict[str, Any]) -> None:
            state = str(status.get("status") or "").casefold()
            if state in {"started", "processing"}:
                self.progress_observer(
                    title,
                    position,
                    total_items,
                    {"status": "processing", "phase": phase},
                )

        return hook

    def _register_download(
        self,
        *,
        media_key: str,
        job_item_id: int,
        output_format: OutputFormat,
        quality: str,
        output_path: Path | None,
        candidates: tuple[CandidatePair, ...],
        dedupe_mode: DedupeMode,
    ) -> tuple[JobItemStatus, str | None]:
        if output_path is None or not output_path.is_file():
            self.dedupe.mark_source_completed(
                media_key=media_key,
                output_format=output_format,
                quality=quality,
                output_path=None,
                job_item_id=job_item_id,
            )
            return JobItemStatus.COMPLETED, None

        if dedupe_mode is DedupeMode.OFF:
            self.dedupe.register_completed_file(
                media_key=media_key,
                output_format=output_format,
                quality=quality,
                path=output_path,
                job_item_id=job_item_id,
            )
            return JobItemStatus.COMPLETED, str(output_path)

        exact = self.dedupe.check_exact_file(output_path)
        if (
            exact.kind is BasicDuplicateKind.EXACT_FILE
            and exact.path is not None
            and _remove_new_duplicate(output_path, Path(exact.path))
        ):
            self.dedupe.mark_source_completed(
                media_key=media_key,
                output_format=output_format,
                quality=quality,
                output_path=exact.path,
                job_item_id=job_item_id,
            )
            return JobItemStatus.SKIPPED_DUPLICATE, exact.path

        smart_alias = self._smart_duplicate_path(
            media_key=media_key,
            output_path=output_path,
            output_format=output_format,
            quality=quality,
            candidates=candidates,
            dedupe_mode=dedupe_mode,
        )
        if smart_alias is not None and _remove_new_duplicate(output_path, smart_alias):
            self.dedupe.mark_source_completed(
                media_key=media_key,
                output_format=output_format,
                quality=quality,
                output_path=str(smart_alias),
                job_item_id=job_item_id,
            )
            return JobItemStatus.SKIPPED_DUPLICATE, str(smart_alias)

        self.dedupe.register_completed_file(
            media_key=media_key,
            output_format=output_format,
            quality=quality,
            path=output_path,
            job_item_id=job_item_id,
            precomputed_sha256=exact.sha256,
        )
        return JobItemStatus.COMPLETED, str(output_path)

    def _smart_duplicate_path(
        self,
        *,
        media_key: str,
        output_path: Path,
        output_format: OutputFormat,
        quality: str,
        candidates: tuple[CandidatePair, ...],
        dedupe_mode: DedupeMode,
    ) -> Path | None:
        if self.smart_dedupe is None or not candidates:
            return None
        try:
            self.smart_dedupe.ensure_fingerprint(media_key, output_path)
        except MediaDLError:
            return None

        for candidate in candidates:
            previous = self.dedupe.check_source_profile(
                media_key=candidate.left_media_key,
                output_format=output_format,
                quality=quality,
            )
            if previous.path is None:
                continue
            previous_path = Path(previous.path)
            if not previous_path.is_file():
                continue
            try:
                self.smart_dedupe.ensure_fingerprint(
                    candidate.left_media_key,
                    previous_path,
                )
                result = self.smart_dedupe.compare_cached(
                    candidate.left_media_key,
                    media_key,
                )
            except MediaDLError:
                continue
            if SmartDedupePolicy.should_skip(
                result,
                output_format=output_format,
                mode=dedupe_mode,
            ):
                return previous_path
        return None

    def _summary(self, job_id: str) -> ExecutionSummary:
        breakdown = self.jobs.job_breakdown(job_id)
        return ExecutionSummary(
            job_id=job_id,
            status=breakdown.status,
            completed=breakdown.completed,
            skipped=breakdown.skipped_duplicate + breakdown.skipped_unavailable,
            failed=breakdown.final_failed + breakdown.retryable_failed,
            skipped_duplicate=breakdown.skipped_duplicate,
            skipped_unavailable=breakdown.skipped_unavailable,
        )


def _remove_new_duplicate(downloaded: Path, kept: Path) -> bool:
    try:
        downloaded_resolved = downloaded.resolve(strict=True)
        kept_resolved = kept.resolve(strict=True)
    except OSError:
        return False
    if downloaded_resolved == kept_resolved or not kept_resolved.is_file():
        return False
    try:
        downloaded_resolved.unlink()
    except OSError:
        return False
    return True


def _is_terminal(status: JobStatus) -> bool:
    return status in {
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_WITH_FAILURES,
        JobStatus.CANCELLED,
    }


def _is_terminal_item_status(status: JobItemStatus) -> bool:
    return status in {
        JobItemStatus.COMPLETED,
        JobItemStatus.SKIPPED_DUPLICATE,
        JobItemStatus.SKIPPED_UNAVAILABLE,
        JobItemStatus.FAILED_FINAL,
        JobItemStatus.CANCELLED,
    }


def _item_output_directory(
    base: Path,
    collection_name: str,
    media_type: str,
    output_format: OutputFormat,
) -> Path:
    folder = {
        "short": MediaFolder.SHORTS,
        "stream": MediaFolder.STREAMS,
    }.get(media_type, MediaFolder.VIDEOS)
    return collection_directory(
        base,
        collection_name=collection_name,
        output_format=output_format,
        media_folder=folder,
    )
