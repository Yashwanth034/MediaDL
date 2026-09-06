import hashlib
from pathlib import Path

import pytest

from mediadl.core.errors import InputError
from mediadl.core.formats import OutputFormat
from mediadl.dedupe.basic import (
    BasicDedupeService,
    BasicDuplicateKind,
    DownloadArchive,
    archive_profile_path,
    sha256_file,
    source_profile_key,
)
from mediadl.storage.database import Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "dedupe.sqlite3")
    assert db.initialize() == 3
    with db.transaction() as connection:
        for key in ("a", "b", "c"):
            connection.execute(
                """
                INSERT INTO media_items(platform, media_key, url, title)
                VALUES ('youtube', ?, ?, ?)
                """,
                (key, f"https://www.youtube.com/watch?v={key}", f"Video {key}"),
            )
    return db


def test_source_profile_key_is_deterministic_and_profile_aware() -> None:
    mp4_best = source_profile_key(
        platform="youtube",
        media_key="abc123",
        output_format=OutputFormat.MP4,
        quality="best",
    )
    assert mp4_best == source_profile_key(
        platform="youtube",
        media_key="abc123",
        output_format="mp4",
        quality="best",
    )
    assert mp4_best != source_profile_key(
        platform="youtube",
        media_key="abc123",
        output_format=OutputFormat.MP3,
        quality="best",
    )
    assert mp4_best != source_profile_key(
        platform="youtube",
        media_key="abc123",
        output_format=OutputFormat.MP4,
        quality="1080p",
    )


def test_source_profile_key_rejects_empty_fields() -> None:
    for kwargs in (
        {"platform": "", "media_key": "a", "output_format": "mp4", "quality": "best"},
        {"platform": "youtube", "media_key": "", "output_format": "mp4", "quality": "best"},
        {"platform": "youtube", "media_key": "a", "output_format": "", "quality": "best"},
        {"platform": "youtube", "media_key": "a", "output_format": "mp4", "quality": ""},
    ):
        with pytest.raises(InputError):
            source_profile_key(**kwargs)


def test_archive_profile_path_is_stable_safe_and_distinct(tmp_path: Path) -> None:
    mp4 = archive_profile_path(tmp_path, OutputFormat.MP4, "1080p")
    mp4_again = archive_profile_path(tmp_path, "mp4", "1080p")
    mp3 = archive_profile_path(tmp_path, OutputFormat.MP3, "320k")

    assert mp4 == mp4_again
    assert mp4 != mp3
    assert mp4.parent == tmp_path
    assert mp4.suffix == ".txt"
    assert "/" not in mp4.name


def test_download_archive_is_ytdlp_compatible_atomic_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "archive.txt"
    archive = DownloadArchive(path)

    assert not archive.contains("youtube", "abc123")
    assert archive.add("youtube", "abc123")
    assert not archive.add("youtube", "abc123")
    assert archive.add("youtube", "def456")
    assert archive.contains("youtube", "abc123")
    assert archive.contains("youtube", "def456")
    assert path.read_text(encoding="utf-8") == "youtube abc123\nyoutube def456\n"
    assert not list(tmp_path.glob(".*.tmp-*"))


def test_download_archive_rejects_invalid_tokens(tmp_path: Path) -> None:
    archive = DownloadArchive(tmp_path / "archive.txt")
    invalid = (
        ("", "abc"),
        ("you tube", "abc"),
        ("youtube", ""),
        ("youtube", "abc\ndef"),
    )
    for extractor, media_key in invalid:
        with pytest.raises(InputError):
            archive.add(extractor, media_key)


def test_sha256_file_streams_and_matches_hashlib(tmp_path: Path) -> None:
    payload = (b"MediaDL exact duplicate test\x00" * 100_000) + b"end"
    path = tmp_path / "large.bin"
    path.write_bytes(payload)

    expected = hashlib.sha256(payload).hexdigest()

    assert sha256_file(path) == expected
    assert sha256_file(path, chunk_size=17) == expected


def test_sha256_rejects_missing_path_and_bad_chunk_size(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="missing or non-file"):
        sha256_file(tmp_path / "missing.bin")
    path = tmp_path / "file.bin"
    path.write_bytes(b"x")
    with pytest.raises(InputError, match="chunk size"):
        sha256_file(path, chunk_size=0)


def test_source_profile_duplicate_does_not_block_other_formats_or_quality(
    database: Database,
    tmp_path: Path,
) -> None:
    service = BasicDedupeService(database)
    output = tmp_path / "a.mp4"
    output.write_bytes(b"unique-a")

    registered = service.register_completed_file(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        path=output,
    )

    assert registered.kind is BasicDuplicateKind.NEW
    same = service.check_source_profile(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
    )
    mp3 = service.check_source_profile(
        media_key="a",
        output_format=OutputFormat.MP3,
        quality="best",
    )
    lower_quality = service.check_source_profile(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="720p",
    )

    assert same.kind is BasicDuplicateKind.SOURCE_PROFILE
    assert same.path == str(output)
    assert mp3.kind is BasicDuplicateKind.NEW
    assert lower_quality.kind is BasicDuplicateKind.NEW


def test_exact_file_duplicate_is_found_across_different_source_ids(
    database: Database,
    tmp_path: Path,
) -> None:
    service = BasicDedupeService(database)
    original = tmp_path / "original.mp4"
    duplicate = tmp_path / "different-name.mp4"
    original.write_bytes(b"same media bytes")
    duplicate.write_bytes(b"same media bytes")

    first = service.register_completed_file(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        path=original,
    )
    match = service.check_exact_file(duplicate)
    second = service.register_completed_file(
        media_key="b",
        output_format=OutputFormat.MP4,
        quality="best",
        path=duplicate,
    )

    assert first.kind is BasicDuplicateKind.NEW
    assert match.kind is BasicDuplicateKind.EXACT_FILE
    assert match.media_key == "a"
    assert match.path == str(original)
    assert second.kind is BasicDuplicateKind.EXACT_FILE

    alias = service.check_source_profile(
        media_key="b",
        output_format=OutputFormat.MP4,
        quality="best",
    )
    assert alias.kind is BasicDuplicateKind.SOURCE_PROFILE
    assert alias.path == str(original)

    with database.connection() as connection:
        file_count = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        completed = connection.execute(
            "SELECT COUNT(*) FROM downloads WHERE status = 'completed'"
        ).fetchone()[0]
    assert file_count == 1
    assert completed == 2


def test_different_file_bytes_register_independently(database: Database, tmp_path: Path) -> None:
    service = BasicDedupeService(database)
    first_path = tmp_path / "a.mp4"
    second_path = tmp_path / "b.mp4"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")

    service.register_completed_file(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        path=first_path,
    )
    before = service.check_exact_file(second_path)
    second = service.register_completed_file(
        media_key="b",
        output_format=OutputFormat.MP4,
        quality="best",
        path=second_path,
    )

    assert before.kind is BasicDuplicateKind.NEW
    assert second.kind is BasicDuplicateKind.NEW
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_registering_same_source_profile_twice_is_idempotent(
    database: Database,
    tmp_path: Path,
) -> None:
    service = BasicDedupeService(database)
    path = tmp_path / "a.mp4"
    path.write_bytes(b"same")

    first = service.register_completed_file(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        path=path,
    )
    second = service.register_completed_file(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        path=path,
    )

    assert first.kind is BasicDuplicateKind.NEW
    assert second.kind is BasicDuplicateKind.SOURCE_PROFILE
    assert first.download_id == second.download_id
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 1


def test_register_rejects_unknown_media_and_missing_file(
    database: Database, tmp_path: Path
) -> None:
    service = BasicDedupeService(database)
    missing = tmp_path / "missing.mp4"
    with pytest.raises(InputError, match="does not exist"):
        service.register_completed_file(
            media_key="a",
            output_format=OutputFormat.MP4,
            quality="best",
            path=missing,
        )

    path = tmp_path / "unknown.mp4"
    path.write_bytes(b"unknown")
    with pytest.raises(InputError, match="unknown media"):
        service.register_completed_file(
            media_key="missing",
            output_format=OutputFormat.MP4,
            quality="best",
            path=path,
        )


def test_missing_source_profile_output_is_retired_and_can_be_registered_again(
    database: Database, tmp_path: Path
) -> None:
    service = BasicDedupeService(database)
    output = tmp_path / "a.mp4"
    output.write_bytes(b"first")
    first = service.register_completed_file(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        path=output,
    )
    assert first.download_id is not None

    output.unlink()
    match = service.check_source_profile(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
    )
    assert match.kind is BasicDuplicateKind.NEW

    with database.connection() as connection:
        assert connection.execute(
            "SELECT status FROM downloads WHERE id = ?", (first.download_id,)
        ).fetchone()[0] == "stale"
        assert connection.execute(
            "SELECT COUNT(*) FROM files WHERE download_id = ?", (first.download_id,)
        ).fetchone()[0] == 0

    output.write_bytes(b"replacement")
    replacement = service.register_completed_file(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        path=output,
    )
    assert replacement.kind is BasicDuplicateKind.NEW
    assert replacement.download_id != first.download_id


def test_missing_exact_file_record_is_retired_instead_of_false_matching(
    database: Database, tmp_path: Path
) -> None:
    service = BasicDedupeService(database)
    original = tmp_path / "original.mp4"
    candidate = tmp_path / "candidate.mp4"
    original.write_bytes(b"same bytes")
    candidate.write_bytes(b"same bytes")
    registered = service.register_completed_file(
        media_key="a",
        output_format=OutputFormat.MP4,
        quality="best",
        path=original,
    )
    assert registered.download_id is not None

    original.unlink()
    match = service.check_exact_file(candidate)
    assert match.kind is BasicDuplicateKind.NEW

    with database.connection() as connection:
        assert connection.execute(
            "SELECT status FROM downloads WHERE id = ?", (registered.download_id,)
        ).fetchone()[0] == "stale"
        assert connection.execute(
            "SELECT COUNT(*) FROM files WHERE download_id = ?", (registered.download_id,)
        ).fetchone()[0] == 0


def test_mark_source_completed_can_record_known_existing_output(
    database: Database, tmp_path: Path
) -> None:
    service = BasicDedupeService(database)
    existing = tmp_path / "already-existing.mp3"
    existing.write_bytes(b"existing")
    download_id = service.mark_source_completed(
        media_key="c",
        output_format=OutputFormat.MP3,
        quality="320k",
        output_path=str(existing),
    )
    again = service.mark_source_completed(
        media_key="c",
        output_format=OutputFormat.MP3,
        quality="320k",
        output_path=str(tmp_path / "ignored-new-value.mp3"),
    )

    assert again == download_id
    match = service.check_source_profile(
        media_key="c",
        output_format=OutputFormat.MP3,
        quality="320k",
    )
    assert match.path == str(existing)
