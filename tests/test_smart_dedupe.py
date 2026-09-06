from pathlib import Path

import pytest

from mediadl.core.errors import DatabaseError, InputError
from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.dedupe.repository import SmartDedupeRepository
from mediadl.dedupe.service import SmartDedupeService
from mediadl.dedupe.smart import (
    DuplicateClassification,
    SimilarityEvidence,
    SmartDedupePolicy,
    SmartDuplicateClassifier,
    SmartDuplicateResult,
    audio_similarity,
    duration_similarity,
    video_similarity,
)
from mediadl.engines.media_fingerprint import MediaFingerprint
from mediadl.storage.database import Database


def frame(value: int) -> int:
    return int.from_bytes(bytes([value]) * 256, "big")


def gradient(reverse: bool = False) -> int:
    values = list(range(16)) * 16
    if reverse:
        values = list(reversed(values))
    pixels = bytes(value * 16 for value in values)
    return int.from_bytes(pixels, "big")


def fingerprint(
    *,
    audio: tuple[int, ...] | None = (1, 2, 3, 4, 5, 6),
    video: tuple[int, ...] | None = None,
    duration: float = 100.0,
    has_audio: bool = True,
    has_video: bool = True,
) -> MediaFingerprint:
    if video is None and has_video:
        video = (gradient(), gradient(), gradient())
    return MediaFingerprint(
        duration_seconds=duration,
        has_audio=has_audio,
        has_video=has_video,
        audio_words=audio if has_audio else None,
        video_hashes=video if has_video else None,
    )


def test_audio_similarity_handles_equal_shifted_and_different_sequences() -> None:
    base = (1, 2, 3, 4, 5, 6)
    shifted = (0xAAAAAAAA, 1, 2, 3, 4, 5, 6, 0x55555555)

    assert audio_similarity(base, base) == 1.0
    assert audio_similarity(base, shifted) == 1.0
    assert audio_similarity((0, 0, 0), (0xFFFFFFFF,) * 3) == 0.0
    assert audio_similarity(None, base) is None
    with pytest.raises(ValueError):
        audio_similarity(base, base, max_shift=-1)


def test_video_similarity_uses_luminance_and_structure() -> None:
    same = (gradient(), gradient())
    reversed_frames = (gradient(True), gradient(True))
    black = (frame(0), frame(0))
    white = (frame(255), frame(255))

    assert video_similarity(same, same) == 1.0
    assert video_similarity(black, white) == pytest.approx(0.3)
    assert video_similarity(same, reversed_frames) < 0.9
    assert video_similarity(None, same) is None


def test_duration_similarity_is_normalized() -> None:
    assert duration_similarity(100, 100) == 1.0
    assert duration_similarity(99, 100) == 0.99
    assert duration_similarity(0, 0) == 1.0
    assert duration_similarity(None, 100) is None


def test_same_reencoded_media_classifies_same_media() -> None:
    left = fingerprint()
    right = fingerprint(
        audio=(1, 2, 3, 4, 5, 6),
        video=(gradient(), gradient(), gradient()),
        duration=99.5,
    )

    result = SmartDuplicateClassifier().compare(left, right)

    assert result.classification is DuplicateClassification.SAME_MEDIA
    assert result.confidence > 0.95


def test_same_audio_with_different_visuals_is_audio_variant_not_video_duplicate() -> None:
    left = fingerprint(video=(frame(0),) * 3)
    right = fingerprint(video=(frame(255),) * 3)

    result = SmartDuplicateClassifier().compare(left, right)

    assert result.classification is DuplicateClassification.AUDIO_VARIANT
    assert result.evidence.audio_similarity == 1.0
    assert result.evidence.video_similarity == pytest.approx(0.3)
    assert not SmartDedupePolicy.should_skip(
        result,
        output_format=OutputFormat.MP4,
        mode=DedupeMode.SAFE,
    )
    assert SmartDedupePolicy.should_skip(
        result,
        output_format=OutputFormat.MP3,
        mode=DedupeMode.SAFE,
    )


def test_audio_mode_can_dedupe_audio_variant_but_off_never_skips() -> None:
    result = SmartDuplicateResult(
        classification=DuplicateClassification.AUDIO_VARIANT,
        confidence=0.75,
        evidence=SimilarityEvidence(
            audio_similarity=0.92,
            video_similarity=0.2,
            duration_similarity=0.99,
        ),
        reason="same audio",
    )

    assert SmartDedupePolicy.should_skip(
        result,
        output_format=OutputFormat.MP3,
        mode=DedupeMode.AUDIO,
    )
    assert not SmartDedupePolicy.should_skip(
        result,
        output_format=OutputFormat.MP3,
        mode=DedupeMode.SAFE,
    )
    assert not SmartDedupePolicy.should_skip(
        result,
        output_format=OutputFormat.MP3,
        mode=DedupeMode.OFF,
    )


def test_materially_different_audio_and_video_classify_different() -> None:
    left = fingerprint(audio=(0, 0, 0, 0), video=(frame(0),) * 3)
    right = fingerprint(audio=(0xFFFFFFFF,) * 4, video=(frame(255),) * 3)

    result = SmartDuplicateClassifier().compare(left, right)

    assert result.classification is DuplicateClassification.DIFFERENT
    assert not SmartDedupePolicy.should_skip(
        result,
        output_format=OutputFormat.MP4,
        mode=DedupeMode.SAFE,
    )


def test_audio_only_identical_content_can_classify_same_media() -> None:
    left = fingerprint(has_video=False)
    right = fingerprint(has_video=False, duration=99.8)

    result = SmartDuplicateClassifier().compare(left, right)

    assert result.classification is DuplicateClassification.SAME_MEDIA
    assert SmartDedupePolicy.should_skip(
        result,
        output_format=OutputFormat.MP3,
        mode=DedupeMode.SAFE,
    )


def test_video_vs_audio_only_is_not_same_media_even_when_audio_matches() -> None:
    video_item = fingerprint(has_video=True)
    audio_item = fingerprint(has_video=False)

    result = SmartDuplicateClassifier().compare(video_item, audio_item)

    assert result.classification is DuplicateClassification.LIKELY_VARIANT
    assert not SmartDedupePolicy.should_skip(
        result,
        output_format=OutputFormat.MP4,
        mode=DedupeMode.SAFE,
    )


@pytest.fixture
def repository(tmp_path: Path) -> SmartDedupeRepository:
    database = Database(tmp_path / "smart.sqlite3")
    assert database.initialize() == 3
    with database.transaction() as connection:
        for key in ("a", "b"):
            connection.execute(
                """
                INSERT INTO media_items(platform, media_key, url, title)
                VALUES ('youtube', ?, ?, ?)
                """,
                (key, f"https://www.youtube.com/watch?v={key}", f"Video {key}"),
            )
    return SmartDedupeRepository(database)


def test_smart_fingerprint_repository_round_trip_and_upsert(
    repository: SmartDedupeRepository,
) -> None:
    first = fingerprint()
    second = fingerprint(duration=123.0, video=(frame(10),) * 3)

    assert repository.load_fingerprint("a") is None
    repository.save_fingerprint("a", first)
    assert repository.load_fingerprint("a") == first
    repository.save_fingerprint("a", second)
    assert repository.load_fingerprint("a") == second

    with repository.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] == 1


def test_repository_records_comparison_group_and_members(
    repository: SmartDedupeRepository,
) -> None:
    result = SmartDuplicateClassifier().compare(fingerprint(), fingerprint())

    group_id = repository.record_comparison("a", "b", result)

    with repository.database.connection() as connection:
        group = connection.execute(
            "SELECT classification, confidence, evidence_json FROM duplicate_groups WHERE id = ?",
            (group_id,),
        ).fetchone()
        members = connection.execute(
            "SELECT role FROM duplicate_members WHERE group_id = ? ORDER BY role",
            (group_id,),
        ).fetchall()
    assert group[0] == "same_media"
    assert float(group[1]) > 0.9
    assert "audio_similarity" in str(group[2])
    assert [row[0] for row in members] == ["candidate", "reference"]


def test_repository_rejects_unknown_media_and_same_id_comparison(
    repository: SmartDedupeRepository,
) -> None:
    with pytest.raises(InputError, match="Unknown media"):
        repository.save_fingerprint("missing", fingerprint())
    result = SmartDuplicateClassifier().compare(fingerprint(), fingerprint())
    with pytest.raises(InputError, match="two different"):
        repository.record_comparison("a", "a", result)


def test_corrupt_stored_fingerprint_fails_closed(repository: SmartDedupeRepository) -> None:
    repository.save_fingerprint("a", fingerprint())
    with repository.database.transaction() as connection:
        connection.execute("UPDATE fingerprints SET value = '{bad json' WHERE 1 = 1")

    with pytest.raises(DatabaseError, match="invalid"):
        repository.load_fingerprint("a")


class FakeFingerprintEngine:
    def __init__(self, value: MediaFingerprint) -> None:
        self.value = value
        self.calls: list[Path] = []

    def fingerprint(self, path: Path) -> MediaFingerprint:
        self.calls.append(path)
        return self.value


def test_service_reuses_cached_fingerprint_and_records_comparison(
    repository: SmartDedupeRepository,
    tmp_path: Path,
) -> None:
    generated = fingerprint()
    engine = FakeFingerprintEngine(generated)
    service = SmartDedupeService(engine, repository)
    path = tmp_path / "does-not-need-to-exist-for-fake.mp4"

    assert service.ensure_fingerprint("a", path) == generated
    assert service.ensure_fingerprint("a", path) == generated
    assert engine.calls == [path]
    service.ensure_fingerprint("b", path)

    result = service.compare_cached("a", "b")

    assert result.classification is DuplicateClassification.SAME_MEDIA
    with repository.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM duplicate_groups").fetchone()[0] == 1


def test_service_requires_both_cached_fingerprints(
    repository: SmartDedupeRepository,
) -> None:
    service = SmartDedupeService(FakeFingerprintEngine(fingerprint()), repository)

    with pytest.raises(InputError, match="a, b"):
        service.compare_cached("a", "b")
