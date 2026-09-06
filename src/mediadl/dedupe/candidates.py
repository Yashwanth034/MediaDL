"""Bounded metadata candidate screening for expensive smart duplicate analysis."""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from mediadl.core.errors import InputError

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_STOPWORDS = {
    "official",
    "video",
    "audio",
    "lyrics",
    "lyric",
    "music",
    "hd",
    "4k",
    "1080p",
    "remastered",
    "remaster",
}


class CandidateMedia(Protocol):
    media_key: str
    title: str
    duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class CandidatePair:
    left_media_key: str
    right_media_key: str
    metadata_score: float


@dataclass(frozen=True, slots=True)
class _IndexedCandidate:
    media: CandidateMedia
    tokens: frozenset[str]


class CandidateScreener:
    """Incrementally screen one ordered collection with bounded recent history."""

    def __init__(
        self,
        *,
        max_candidates_per_item: int = 12,
        token_history: int = 64,
        duration_history: int = 8,
        duration_bucket_seconds: float = 5.0,
    ) -> None:
        if max_candidates_per_item < 1:
            raise InputError("Smart-dedupe candidate limit must be at least 1")
        if token_history < 1 or duration_history < 1:
            raise InputError("Smart-dedupe candidate history limits must be at least 1")
        if duration_bucket_seconds <= 0:
            raise InputError("Smart-dedupe duration bucket must be positive")
        self.max_candidates_per_item = max_candidates_per_item
        self.duration_bucket_seconds = duration_bucket_seconds
        self._token_index: dict[str, deque[_IndexedCandidate]] = defaultdict(
            lambda: deque(maxlen=token_history)
        )
        self._duration_index: dict[int, deque[_IndexedCandidate]] = defaultdict(
            lambda: deque(maxlen=duration_history)
        )
        # One representative per normalized title deliberately survives history eviction.
        self._exact_title: dict[str, _IndexedCandidate] = {}

    def consider(self, item: CandidateMedia) -> tuple[CandidatePair, ...]:
        """Return bounded candidates for item, then add it to screening history."""

        tokens = _title_tokens(item.title)
        core = " ".join(sorted(tokens))
        candidates: dict[str, _IndexedCandidate] = {}

        if core and core in self._exact_title:
            previous = self._exact_title[core]
            candidates[previous.media.media_key] = previous
        for token in tokens:
            for previous in self._token_index[token]:
                candidates[previous.media.media_key] = previous

        bucket = _duration_bucket(item.duration_seconds, self.duration_bucket_seconds)
        if bucket is not None:
            for neighbor in (bucket - 1, bucket, bucket + 1):
                for previous in self._duration_index[neighbor]:
                    candidates[previous.media.media_key] = previous

        scored: list[tuple[float, str, _IndexedCandidate]] = []
        for previous in candidates.values():
            previous_media = previous.media
            if not _duration_compatible(previous_media.duration_seconds, item.duration_seconds):
                continue
            score = _metadata_score(
                previous.tokens,
                tokens,
                previous_media.duration_seconds,
                item.duration_seconds,
            )
            scored.append((score, previous_media.media_key, previous))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))

        pairs = tuple(
            CandidatePair(
                left_media_key=previous.media.media_key,
                right_media_key=item.media_key,
                metadata_score=score,
            )
            for score, _, previous in scored[: self.max_candidates_per_item]
        )

        indexed = _IndexedCandidate(item, tokens)
        if core:
            self._exact_title[core] = indexed
        for token in tokens:
            self._token_index[token].append(indexed)
        if bucket is not None:
            self._duration_index[bucket].append(indexed)
        return pairs


def iter_candidate_pairs(
    items: Sequence[CandidateMedia],
    *,
    max_candidates_per_item: int = 12,
    token_history: int = 64,
    duration_history: int = 8,
    duration_bucket_seconds: float = 5.0,
) -> Iterator[CandidatePair]:
    """Yield a bounded set of plausible pairs without O(N²) materialization."""

    screener = CandidateScreener(
        max_candidates_per_item=max_candidates_per_item,
        token_history=token_history,
        duration_history=duration_history,
        duration_bucket_seconds=duration_bucket_seconds,
    )
    for item in items:
        yield from screener.consider(item)


def _title_tokens(title: str) -> frozenset[str]:
    return frozenset(
        token.casefold()
        for token in _TOKEN_RE.findall(title)
        if len(token) >= 2 and token.casefold() not in _STOPWORDS
    )


def _duration_bucket(duration: float | None, bucket_seconds: float) -> int | None:
    if duration is None or duration < 0:
        return None
    return math.floor(duration / bucket_seconds)


def _duration_compatible(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return True
    tolerance = max(5.0, 0.03 * max(left, right))
    return abs(left - right) <= tolerance


def _metadata_score(
    left_tokens: frozenset[str],
    right_tokens: frozenset[str],
    left_duration: float | None,
    right_duration: float | None,
) -> float:
    union = left_tokens | right_tokens
    title_score = len(left_tokens & right_tokens) / len(union) if union else 0.0
    duration_score = 0.5
    if left_duration is not None and right_duration is not None:
        larger = max(left_duration, right_duration)
        duration_score = 1.0 if larger == 0 else min(left_duration, right_duration) / larger
    return (0.75 * title_score) + (0.25 * duration_score)
