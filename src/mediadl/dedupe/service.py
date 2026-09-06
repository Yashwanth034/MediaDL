"""Cache-aware smart duplicate analysis orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from mediadl.core.errors import InputError
from mediadl.dedupe.repository import SmartDedupeRepository
from mediadl.dedupe.smart import SmartDuplicateClassifier, SmartDuplicateResult
from mediadl.engines.media_fingerprint import MediaFingerprint


class FingerprintEngine(Protocol):
    def fingerprint(self, path: Path) -> MediaFingerprint: ...


class SmartDedupeService:
    def __init__(
        self,
        engine: FingerprintEngine,
        repository: SmartDedupeRepository,
        classifier: SmartDuplicateClassifier | None = None,
    ) -> None:
        self.engine = engine
        self.repository = repository
        self.classifier = classifier or SmartDuplicateClassifier()

    def ensure_fingerprint(
        self,
        media_key: str,
        path: Path,
        *,
        file_id: int | None = None,
        refresh: bool = False,
    ) -> MediaFingerprint:
        if not refresh:
            cached = self.repository.load_fingerprint(media_key)
            if cached is not None:
                return cached
        generated = self.engine.fingerprint(path)
        self.repository.save_fingerprint(media_key, generated, file_id=file_id)
        return generated

    def compare_cached(self, left_media_key: str, right_media_key: str) -> SmartDuplicateResult:
        left = self.repository.load_fingerprint(left_media_key)
        right = self.repository.load_fingerprint(right_media_key)
        missing = [
            media_key
            for media_key, fingerprint in (
                (left_media_key, left),
                (right_media_key, right),
            )
            if fingerprint is None
        ]
        if missing:
            raise InputError(
                "Smart duplicate comparison requires cached fingerprints for: "
                + ", ".join(missing)
            )
        assert left is not None and right is not None
        result = self.classifier.compare(left, right)
        self.repository.record_comparison(left_media_key, right_media_key, result)
        return result
