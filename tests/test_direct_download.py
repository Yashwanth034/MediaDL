from pathlib import Path

from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.dedupe.basic import BasicDedupeService
from mediadl.downloads.direct import DirectDownloadCoordinator, DirectDownloadStatus
from mediadl.downloads.single import DownloadReceipt, SingleDownloadRequest
from mediadl.index.repository import IndexRepository
from mediadl.sources.resolver import SourceResolver
from mediadl.storage.database import Database


class FileDownloader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def download(self, request: SingleDownloadRequest, *, progress_hook=None) -> DownloadReceipt:
        media_key = request.url.rsplit("=", 1)[-1]
        self.calls.append(media_key)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = request.output_dir / f"Direct [{media_key}].{request.output_format.value}"
        output_path.write_bytes(self.payloads[media_key])
        if progress_hook is not None:
            progress_hook(
                {
                    "status": "finished",
                    "downloaded_bytes": len(self.payloads[media_key]),
                    "total_bytes": len(self.payloads[media_key]),
                }
            )
        return DownloadReceipt(
            media_id=media_key,
            title=f"Direct {media_key}",
            source_url=request.url,
            output_format=request.output_format,
            quality="best",
            output_path=output_path,
        )


def runtime(tmp_path: Path, payloads: dict[str, bytes]):
    database = Database(tmp_path / "direct.sqlite3")
    assert database.initialize() == 3
    downloader = FileDownloader(payloads)
    coordinator = DirectDownloadCoordinator(
        downloader,  # type: ignore[arg-type]
        IndexRepository(database),
        BasicDedupeService(database),
    )
    return database, downloader, coordinator


def request(tmp_path: Path, media_key: str) -> SingleDownloadRequest:
    return SingleDownloadRequest(
        url=f"https://www.youtube.com/watch?v={media_key}",
        output_dir=tmp_path / "downloads",
        output_format=OutputFormat.MP4,
        quality="best",
    )


def test_direct_same_source_profile_skips_before_redownload(tmp_path: Path) -> None:
    _, downloader, coordinator = runtime(tmp_path, {"a": b"content-a"})
    source = SourceResolver().resolve("https://www.youtube.com/watch?v=a")

    first = coordinator.download(source, request(tmp_path, "a"), dedupe_mode=DedupeMode.SAFE)
    second = coordinator.download(source, request(tmp_path, "a"), dedupe_mode=DedupeMode.SAFE)

    assert first.status is DirectDownloadStatus.DOWNLOADED
    assert second.status is DirectDownloadStatus.SKIPPED_DUPLICATE
    assert downloader.calls == ["a"]
    assert second.output_path == first.output_path


def test_direct_missing_recorded_file_redownloads_instead_of_false_skipping(tmp_path: Path) -> None:
    database, downloader, coordinator = runtime(tmp_path, {"a": b"content-a"})
    source = SourceResolver().resolve("https://www.youtube.com/watch?v=a")

    first = coordinator.download(source, request(tmp_path, "a"), dedupe_mode=DedupeMode.SAFE)
    assert first.output_path is not None
    Path(first.output_path).unlink()

    second = coordinator.download(source, request(tmp_path, "a"), dedupe_mode=DedupeMode.SAFE)

    assert first.status is DirectDownloadStatus.DOWNLOADED
    assert second.status is DirectDownloadStatus.DOWNLOADED
    assert downloader.calls == ["a", "a"]
    assert second.output_path is not None
    assert Path(second.output_path).is_file()
    with database.connection() as connection:
        statuses = [
            str(row[0])
            for row in connection.execute(
                "SELECT status FROM downloads ORDER BY id"
            ).fetchall()
        ]
    assert statuses == ["stale", "completed"]


def test_direct_different_ids_with_identical_bytes_remove_only_new_copy(tmp_path: Path) -> None:
    database, downloader, coordinator = runtime(tmp_path, {"a": b"same", "b": b"same"})
    source_a = SourceResolver().resolve("https://www.youtube.com/watch?v=a")
    source_b = SourceResolver().resolve("https://www.youtube.com/watch?v=b")

    first = coordinator.download(source_a, request(tmp_path, "a"), dedupe_mode=DedupeMode.SAFE)
    second = coordinator.download(source_b, request(tmp_path, "b"), dedupe_mode=DedupeMode.SAFE)

    assert first.status is DirectDownloadStatus.DOWNLOADED
    assert second.status is DirectDownloadStatus.SKIPPED_DUPLICATE
    assert downloader.calls == ["a", "b"]
    assert first.output_path is not None
    assert second.output_path == first.output_path
    assert Path(first.output_path).is_file()
    assert not (tmp_path / "downloads" / "Direct [b].mp4").exists()
    with database.connection() as connection:
        completed = connection.execute(
            "SELECT COUNT(*) FROM downloads WHERE status = 'completed'"
        ).fetchone()[0]
    assert completed == 2


def test_direct_dedupe_off_keeps_distinct_files(tmp_path: Path) -> None:
    _, downloader, coordinator = runtime(tmp_path, {"a": b"same", "b": b"same"})
    source_a = SourceResolver().resolve("https://www.youtube.com/watch?v=a")
    source_b = SourceResolver().resolve("https://www.youtube.com/watch?v=b")

    first = coordinator.download(source_a, request(tmp_path, "a"), dedupe_mode=DedupeMode.OFF)
    second = coordinator.download(source_b, request(tmp_path, "b"), dedupe_mode=DedupeMode.OFF)

    assert first.status is DirectDownloadStatus.DOWNLOADED
    assert second.status is DirectDownloadStatus.DOWNLOADED
    assert downloader.calls == ["a", "b"]
    assert (tmp_path / "downloads" / "Direct [a].mp4").is_file()
    assert (tmp_path / "downloads" / "Direct [b].mp4").is_file()
