from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.dedupe.basic import BasicDedupeService
from mediadl.dedupe.smart import (
    DuplicateClassification,
    SimilarityEvidence,
    SmartDuplicateResult,
)
from mediadl.downloads.executor import CollectionJobExecutor
from mediadl.downloads.jobs import JobRepository, JobStatus
from mediadl.downloads.plan import DiskSpace, DownloadPlanBuilder
from mediadl.downloads.single import DownloadReceipt, SingleDownloadRequest
from mediadl.sources.models import MediaItemStub, SourceDescriptor, SourceKind
from mediadl.storage.database import Database


class FileDownloader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.paths: list[Path] = []

    def download(
        self,
        request: SingleDownloadRequest,
        *,
        progress_hook: object | None = None,
        postprocessor_hook: object | None = None,
    ) -> DownloadReceipt:
        media_key = request.url.rsplit("=", 1)[-1]
        request.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = request.output_dir / f"Track [{media_key}].{request.output_format.value}"
        output_path.write_bytes(self.payloads[media_key])
        self.paths.append(output_path)
        return DownloadReceipt(
            media_id=media_key,
            title=f"Track {media_key}",
            source_url=request.url,
            output_format=request.output_format,
            quality=request.quality,
            output_path=output_path,
        )


class FakeSmartDedupe:
    def __init__(self, result: SmartDuplicateResult) -> None:
        self.result = result
        self.fingerprinted: list[str] = []
        self.comparisons: list[tuple[str, str]] = []

    def ensure_fingerprint(self, media_key: str, path: Path) -> object:
        assert path.is_file()
        self.fingerprinted.append(media_key)
        return object()

    def compare_cached(self, left_media_key: str, right_media_key: str) -> SmartDuplicateResult:
        self.comparisons.append((left_media_key, right_media_key))
        return self.result


def source() -> SourceDescriptor:
    return SourceDescriptor(
        platform="youtube",
        kind=SourceKind.CHANNEL_VIDEOS,
        source_key="@Dedupe",
        url="https://www.youtube.com/@Dedupe/videos",
        title="Dedupe Channel",
    )


def media(key: str, title: str, duration: float) -> MediaItemStub:
    return MediaItemStub(
        media_key=key,
        title=title,
        url=f"https://www.youtube.com/watch?v={key}",
        duration_seconds=duration,
        media_type="video",
    )


def make_plan(
    tmp_path: Path,
    *,
    dedupe_mode: DedupeMode,
    output_format: OutputFormat = OutputFormat.MP4,
):
    return DownloadPlanBuilder(
        disk_probe=lambda _: DiskSpace(total=10_000_000, used=0, free=10_000_000),
        now_provider=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        id_provider=lambda: "dedupe-execution-job",
    ).build(
        source=source(),
        items=[
            media("a", "Artist Great Song Official Video", 180),
            media("b", "Artist Great Song Lyrics", 181),
        ],
        output_format=output_format,
        quality="best",
        dedupe_mode=dedupe_mode,
        output_dir=tmp_path / "downloads",
        selection_label="All",
        sort_mode="source",
    )


def runtime(
    tmp_path: Path, *, dedupe_mode: DedupeMode, output_format: OutputFormat = OutputFormat.MP4
):
    database = Database(tmp_path / "dedupe-execution.sqlite3")
    assert database.initialize() == 3
    jobs = JobRepository(database)
    jobs.create_job(make_plan(tmp_path, dedupe_mode=dedupe_mode, output_format=output_format))
    return database, jobs, BasicDedupeService(database)


def same_media_result() -> SmartDuplicateResult:
    return SmartDuplicateResult(
        DuplicateClassification.SAME_MEDIA,
        0.99,
        SimilarityEvidence(0.99, 0.99, 0.99),
        "same media",
    )


def audio_variant_result() -> SmartDuplicateResult:
    return SmartDuplicateResult(
        DuplicateClassification.AUDIO_VARIANT,
        0.95,
        SimilarityEvidence(1.0, 0.3, 1.0),
        "same audio, different visuals",
    )


def test_parallel_safe_mode_serializes_exact_duplicate_registration(
    tmp_path: Path,
) -> None:
    database, jobs, dedupe = runtime(tmp_path, dedupe_mode=DedupeMode.SAFE)
    downloader = FileDownloader({"a": b"identical bytes", "b": b"identical bytes"})

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        max_workers=2,
    ).run("dedupe-execution-job")

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 1
    assert summary.skipped == 1
    existing_files = [path for path in downloader.paths if path.exists()]
    assert len(existing_files) == 1
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_parallel_smart_dedupe_waits_for_earlier_candidate_registration(
    tmp_path: Path,
) -> None:
    _, jobs, dedupe = runtime(tmp_path, dedupe_mode=DedupeMode.SAFE)
    downloader = FileDownloader({"a": b"encoding A", "b": b"encoding B"})
    smart = FakeSmartDedupe(same_media_result())

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        smart_dedupe=smart,  # type: ignore[arg-type]
        max_workers=2,
    ).run("dedupe-execution-job")

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 1
    assert summary.skipped == 1
    assert len([path for path in downloader.paths if path.exists()]) == 1
    assert smart.comparisons == [("a", "b")]


def test_safe_mode_removes_new_exact_duplicate_and_keeps_registered_original(
    tmp_path: Path,
) -> None:
    database, jobs, dedupe = runtime(tmp_path, dedupe_mode=DedupeMode.SAFE)
    downloader = FileDownloader({"a": b"identical bytes", "b": b"identical bytes"})

    summary = CollectionJobExecutor(jobs, downloader, dedupe).run("dedupe-execution-job")  # type: ignore[arg-type]

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 1
    assert summary.skipped == 1
    existing_files = [path for path in downloader.paths if path.exists()]
    assert len(existing_files) == 1
    b_profile = dedupe.check_source_profile(
        media_key="b", output_format=OutputFormat.MP4, quality="best"
    )
    assert b_profile.path == str(existing_files[0])
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_dedupe_off_keeps_both_physical_exact_copies(tmp_path: Path) -> None:
    _, jobs, dedupe = runtime(tmp_path, dedupe_mode=DedupeMode.OFF)
    downloader = FileDownloader({"a": b"identical bytes", "b": b"identical bytes"})

    summary = CollectionJobExecutor(jobs, downloader, dedupe).run("dedupe-execution-job")  # type: ignore[arg-type]

    assert summary.status is JobStatus.COMPLETED
    assert summary.completed == 2
    assert summary.skipped == 0
    assert len([path for path in downloader.paths if path.exists()]) == 2


def test_smart_same_media_removes_reencoded_candidate(tmp_path: Path) -> None:
    _, jobs, dedupe = runtime(tmp_path, dedupe_mode=DedupeMode.SAFE)
    downloader = FileDownloader({"a": b"encoding A", "b": b"encoding B"})
    smart = FakeSmartDedupe(same_media_result())

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        smart_dedupe=smart,  # type: ignore[arg-type]
    ).run("dedupe-execution-job")

    assert summary.completed == 1
    assert summary.skipped == 1
    assert len([path for path in downloader.paths if path.exists()]) == 1
    assert smart.comparisons == [("a", "b")]


def test_audio_variant_is_never_removed_for_mp4(tmp_path: Path) -> None:
    _, jobs, dedupe = runtime(tmp_path, dedupe_mode=DedupeMode.SAFE)
    downloader = FileDownloader({"a": b"official visuals", "b": b"lyrics visuals"})
    smart = FakeSmartDedupe(audio_variant_result())

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        smart_dedupe=smart,  # type: ignore[arg-type]
    ).run("dedupe-execution-job")

    assert summary.completed == 2
    assert summary.skipped == 0
    assert len([path for path in downloader.paths if path.exists()]) == 2
    assert smart.comparisons == [("a", "b")]


def test_audio_variant_can_be_removed_for_mp3_safe_mode(tmp_path: Path) -> None:
    _, jobs, dedupe = runtime(
        tmp_path,
        dedupe_mode=DedupeMode.SAFE,
        output_format=OutputFormat.MP3,
    )
    downloader = FileDownloader({"a": b"audio encoding A", "b": b"audio encoding B"})
    smart = FakeSmartDedupe(audio_variant_result())

    summary = CollectionJobExecutor(
        jobs,
        downloader,  # type: ignore[arg-type]
        dedupe,
        smart_dedupe=smart,  # type: ignore[arg-type]
    ).run("dedupe-execution-job")

    assert summary.completed == 1
    assert summary.skipped == 1
    assert len([path for path in downloader.paths if path.exists()]) == 1
