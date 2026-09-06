"""Conservative smart duplicate comparison and policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mediadl.core.formats import OutputFormat
from mediadl.core.policies import DedupeMode
from mediadl.engines.media_fingerprint import MediaFingerprint


class DuplicateClassification(StrEnum):
    SAME_MEDIA = "same_media"
    AUDIO_VARIANT = "audio_variant"
    LIKELY_VARIANT = "likely_variant"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SimilarityEvidence:
    audio_similarity: float | None
    video_similarity: float | None
    duration_similarity: float | None


@dataclass(frozen=True, slots=True)
class SmartDuplicateResult:
    classification: DuplicateClassification
    confidence: float
    evidence: SimilarityEvidence
    reason: str


@dataclass(frozen=True, slots=True)
class SmartThresholds:
    audio_same: float = 0.90
    video_same: float = 0.90
    video_different: float = 0.72
    duration_close: float = 0.97
    clearly_different: float = 0.55


class SmartDuplicateClassifier:
    """Classify fingerprint pairs; uncertain evidence is deliberately kept."""

    def __init__(self, thresholds: SmartThresholds | None = None) -> None:
        self.thresholds = thresholds or SmartThresholds()

    def compare(
        self,
        left: MediaFingerprint,
        right: MediaFingerprint,
    ) -> SmartDuplicateResult:
        audio = audio_similarity(left.audio_words, right.audio_words)
        video = video_similarity(left.video_hashes, right.video_hashes)
        duration = duration_similarity(left.duration_seconds, right.duration_seconds)
        evidence = SimilarityEvidence(audio, video, duration)
        t = self.thresholds

        if left.has_video != right.has_video:
            if audio is not None and audio >= t.audio_same and _close(duration, t.duration_close):
                return SmartDuplicateResult(
                    DuplicateClassification.LIKELY_VARIANT,
                    _confidence(audio, duration),
                    evidence,
                    "Audio matches closely but one item has video and the other does not.",
                )
            return SmartDuplicateResult(
                DuplicateClassification.UNKNOWN,
                0.5,
                evidence,
                "Media stream types differ and evidence is insufficient for deduplication.",
            )

        if left.has_video and right.has_video:
            if (
                video is not None
                and video >= t.video_same
                and _close(duration, t.duration_close)
                and _audio_compatible(left, right, audio, t.audio_same)
            ):
                return SmartDuplicateResult(
                    DuplicateClassification.SAME_MEDIA,
                    _confidence(video, audio, duration),
                    evidence,
                    "Video samples and available audio evidence match closely.",
                )
            if (
                audio is not None
                and audio >= t.audio_same
                and video is not None
                and video < t.video_different
                and _close(duration, t.duration_close)
            ):
                return SmartDuplicateResult(
                    DuplicateClassification.AUDIO_VARIANT,
                    _confidence(audio, 1.0 - video, duration),
                    evidence,
                    "Audio matches closely but sampled visuals are materially different.",
                )
            if (audio is not None and audio >= t.audio_same) or (
                video is not None and video >= t.video_different
            ):
                return SmartDuplicateResult(
                    DuplicateClassification.LIKELY_VARIANT,
                    _confidence(audio, video, duration),
                    evidence,
                    (
                        "Some media evidence is similar, but not enough for safe "
                        "same-media classification."
                    ),
                )
            if _clearly_different(audio, video, t.clearly_different):
                return SmartDuplicateResult(
                    DuplicateClassification.DIFFERENT,
                    _confidence(_inverse(audio), _inverse(video)),
                    evidence,
                    "Available audio and video evidence are materially different.",
                )
            return SmartDuplicateResult(
                DuplicateClassification.UNKNOWN,
                0.5,
                evidence,
                "Video comparison evidence is incomplete or ambiguous.",
            )

        if left.has_audio and right.has_audio:
            if audio is not None and audio >= t.audio_same and _close(duration, t.duration_close):
                return SmartDuplicateResult(
                    DuplicateClassification.SAME_MEDIA,
                    _confidence(audio, duration),
                    evidence,
                    "Audio-only fingerprints and duration match closely.",
                )
            if audio is not None and audio < t.clearly_different:
                return SmartDuplicateResult(
                    DuplicateClassification.DIFFERENT,
                    _confidence(1.0 - audio),
                    evidence,
                    "Audio-only fingerprints are materially different.",
                )
            return SmartDuplicateResult(
                DuplicateClassification.LIKELY_VARIANT,
                _confidence(audio, duration),
                evidence,
                "Audio-only evidence is similar or ambiguous but not safe enough to merge.",
            )

        return SmartDuplicateResult(
            DuplicateClassification.UNKNOWN,
            0.25,
            evidence,
            "No comparable fingerprint evidence is available.",
        )


class SmartDedupePolicy:
    """Translate classification into a conservative skip decision."""

    @staticmethod
    def should_skip(
        result: SmartDuplicateResult,
        *,
        output_format: OutputFormat,
        mode: DedupeMode,
    ) -> bool:
        if mode is DedupeMode.OFF:
            return False
        if result.classification is DuplicateClassification.SAME_MEDIA:
            return result.confidence >= 0.90
        if result.classification is DuplicateClassification.AUDIO_VARIANT:
            if output_format.is_video:
                return False
            audio = result.evidence.audio_similarity
            duration = result.evidence.duration_similarity
            minimum = 0.90 if mode is DedupeMode.AUDIO else 0.95
            return audio is not None and audio >= minimum and (duration is None or duration >= 0.97)
        return False


def audio_similarity(
    left: tuple[int, ...] | None,
    right: tuple[int, ...] | None,
    *,
    max_shift: int = 4,
) -> float | None:
    """Compare Chromaprint words using bit similarity with small sequence alignment shifts."""

    if not left or not right:
        return None
    if max_shift < 0:
        raise ValueError("max_shift cannot be negative")
    best = 0.0
    minimum_overlap = min(3, len(left), len(right))
    for shift in range(-max_shift, max_shift + 1):
        left_start = max(0, shift)
        right_start = max(0, -shift)
        overlap = min(len(left) - left_start, len(right) - right_start)
        if overlap < minimum_overlap:
            continue
        equal_bits = 0
        total_bits = 32 * overlap
        for index in range(overlap):
            xor = left[left_start + index] ^ right[right_start + index]
            equal_bits += 32 - xor.bit_count()
        best = max(best, equal_bits / total_bits)
    return best


def video_similarity(
    left: tuple[int, ...] | None,
    right: tuple[int, ...] | None,
) -> float | None:
    """Compare normalized 16×16 samples using luminance and edge structure."""

    if not left or not right:
        return None
    count = min(len(left), len(right))
    if count == 0:
        return None
    total = 0.0
    for first, second in zip(left[:count], right[:count], strict=True):
        first_frame = first.to_bytes(256, "big")
        second_frame = second.to_bytes(256, "big")
        absolute_difference = sum(
            abs(first_pixel - second_pixel)
            for first_pixel, second_pixel in zip(first_frame, second_frame, strict=True)
        )
        luminance = 1.0 - (absolute_difference / (255.0 * 256.0))
        first_edges = _horizontal_edges(first_frame)
        second_edges = _horizontal_edges(second_frame)
        edge_matches = sum(
            left_edge == right_edge
            for left_edge, right_edge in zip(first_edges, second_edges, strict=True)
        )
        edge_similarity = edge_matches / len(first_edges)
        total += (0.7 * luminance) + (0.3 * edge_similarity)
    return total / count


def _horizontal_edges(frame: bytes) -> tuple[bool, ...]:
    edges: list[bool] = []
    for row in range(16):
        offset = row * 16
        for column in range(15):
            edges.append(frame[offset + column] > frame[offset + column + 1])
    return tuple(edges)


def duration_similarity(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or left < 0 or right < 0:
        return None
    if left == 0 and right == 0:
        return 1.0
    larger = max(left, right)
    if larger == 0:
        return 1.0
    return max(0.0, min(left, right) / larger)


def _audio_compatible(
    left: MediaFingerprint,
    right: MediaFingerprint,
    similarity: float | None,
    threshold: float,
) -> bool:
    if left.has_audio != right.has_audio:
        return False
    if not left.has_audio:
        return True
    return similarity is not None and similarity >= threshold


def _close(value: float | None, threshold: float) -> bool:
    return value is None or value >= threshold


def _clearly_different(
    audio: float | None,
    video: float | None,
    threshold: float,
) -> bool:
    available = [value for value in (audio, video) if value is not None]
    return bool(available) and all(value < threshold for value in available)


def _inverse(value: float | None) -> float | None:
    return None if value is None else 1.0 - value


def _confidence(*values: float | None) -> float:
    available = [max(0.0, min(1.0, value)) for value in values if value is not None]
    if not available:
        return 0.0
    return sum(available) / len(available)
