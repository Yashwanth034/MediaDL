"""Persistence for smart media fingerprints and duplicate comparison evidence."""

from __future__ import annotations

import json
import sqlite3

from mediadl.core.errors import DatabaseError, InputError
from mediadl.dedupe.smart import SmartDuplicateResult
from mediadl.engines.media_fingerprint import MediaFingerprint
from mediadl.storage.database import Database

_FINGERPRINT_KIND = "media_smart"
_FINGERPRINT_ALGORITHM = "chromaprint-gray16-v1"
_FINGERPRINT_SCOPE = "sampled"


class SmartDedupeRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save_fingerprint(
        self,
        media_key: str,
        fingerprint: MediaFingerprint,
        *,
        file_id: int | None = None,
        platform: str = "youtube",
    ) -> None:
        media_id = self._media_id(media_key, platform)
        payload = _serialize_fingerprint(fingerprint)
        try:
            with self.database.transaction() as connection:
                if file_id is not None:
                    file_row = connection.execute(
                        "SELECT 1 FROM files WHERE id = ?",
                        (file_id,),
                    ).fetchone()
                    if file_row is None:
                        raise InputError(f"Unknown file ID for fingerprint: {file_id}")
                connection.execute(
                    """
                    INSERT INTO fingerprints(
                        media_item_id, file_id, kind, algorithm, scope, value, duration_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(media_item_id, kind, algorithm, scope) DO UPDATE SET
                        file_id = excluded.file_id,
                        value = excluded.value,
                        duration_seconds = excluded.duration_seconds,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    (
                        media_id,
                        file_id,
                        _FINGERPRINT_KIND,
                        _FINGERPRINT_ALGORITHM,
                        _FINGERPRINT_SCOPE,
                        payload,
                        fingerprint.duration_seconds,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"Could not persist media fingerprint: {exc}") from exc

    def load_fingerprint(
        self,
        media_key: str,
        *,
        platform: str = "youtube",
    ) -> MediaFingerprint | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT f.value
                FROM fingerprints f
                JOIN media_items m ON m.id = f.media_item_id
                WHERE m.platform = ? AND m.media_key = ?
                  AND f.kind = ? AND f.algorithm = ? AND f.scope = ?
                """,
                (
                    platform,
                    media_key,
                    _FINGERPRINT_KIND,
                    _FINGERPRINT_ALGORITHM,
                    _FINGERPRINT_SCOPE,
                ),
            ).fetchone()
        if row is None:
            return None
        return _deserialize_fingerprint(str(row[0]))

    def record_comparison(
        self,
        left_media_key: str,
        right_media_key: str,
        result: SmartDuplicateResult,
        *,
        platform: str = "youtube",
    ) -> int:
        if left_media_key == right_media_key:
            raise InputError("Smart duplicate comparison requires two different media IDs")
        left_id = self._media_id(left_media_key, platform)
        right_id = self._media_id(right_media_key, platform)
        evidence_json = json.dumps(
            {
                "audio_similarity": result.evidence.audio_similarity,
                "video_similarity": result.evidence.video_similarity,
                "duration_similarity": result.evidence.duration_similarity,
                "reason": result.reason,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO duplicate_groups(classification, confidence, evidence_json)
                VALUES (?, ?, ?)
                """,
                (result.classification.value, result.confidence, evidence_json),
            )
            group_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO duplicate_members(group_id, media_item_id, role)
                VALUES (?, ?, ?)
                """,
                (
                    (group_id, left_id, "reference"),
                    (group_id, right_id, "candidate"),
                ),
            )
        return group_id

    def _media_id(self, media_key: str, platform: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id FROM media_items WHERE platform = ? AND media_key = ?",
                (platform, media_key),
            ).fetchone()
        if row is None:
            raise InputError(f"Unknown media item for smart dedupe: {media_key}")
        return int(row[0])


def _serialize_fingerprint(fingerprint: MediaFingerprint) -> str:
    payload = {
        "duration_seconds": fingerprint.duration_seconds,
        "has_audio": fingerprint.has_audio,
        "has_video": fingerprint.has_video,
        "audio_words": list(fingerprint.audio_words)
        if fingerprint.audio_words is not None
        else None,
        "video_samples": (
            [f"{value:0512x}" for value in fingerprint.video_hashes]
            if fingerprint.video_hashes is not None
            else None
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _deserialize_fingerprint(value: str) -> MediaFingerprint:
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("fingerprint root is not an object")
        audio_raw = payload.get("audio_words")
        video_raw = payload.get("video_samples")
        audio = None if audio_raw is None else tuple(int(word) for word in audio_raw)
        video = None if video_raw is None else tuple(int(sample, 16) for sample in video_raw)
        if audio is not None and any(word < 0 or word > 0xFFFFFFFF for word in audio):
            raise ValueError("audio fingerprint word is outside uint32 range")
        if video is not None and any(sample < 0 or sample >= (1 << 2048) for sample in video):
            raise ValueError("video sample signature is outside 256-byte range")
        duration_raw = payload.get("duration_seconds")
        duration = None if duration_raw is None else float(duration_raw)
        has_audio = payload.get("has_audio")
        has_video = payload.get("has_video")
        if not isinstance(has_audio, bool) or not isinstance(has_video, bool):
            raise ValueError("stream-presence fields must be boolean")
        if duration is not None and duration < 0:
            raise ValueError("duration cannot be negative")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DatabaseError(f"Stored media fingerprint is invalid: {exc}") from exc
    return MediaFingerprint(
        duration_seconds=duration,
        has_audio=has_audio,
        has_video=has_video,
        audio_words=audio,
        video_hashes=video,
    )
