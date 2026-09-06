"""Basic duplicate detection: source/profile identity, archives, and SHA-256."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from mediadl.core.errors import DatabaseError, InputError
from mediadl.core.formats import OutputFormat
from mediadl.storage.database import Database

_CHUNK_SIZE = 1024 * 1024
_PROFILE_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class BasicDuplicateKind(StrEnum):
    NEW = "new"
    SOURCE_PROFILE = "source_profile"
    EXACT_FILE = "exact_file"


@dataclass(frozen=True, slots=True)
class BasicDuplicateMatch:
    kind: BasicDuplicateKind
    media_key: str | None = None
    path: str | None = None
    download_id: int | None = None
    sha256: str | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.kind is not BasicDuplicateKind.NEW


def source_profile_key(
    *,
    platform: str,
    media_key: str,
    output_format: OutputFormat | str,
    quality: str,
) -> str:
    """Return an unambiguous identity for one source and requested output profile."""

    format_value = (
        output_format.value if isinstance(output_format, OutputFormat) else str(output_format)
    )
    parts = (platform.strip(), media_key.strip(), format_value.strip(), quality.strip())
    if any(not part for part in parts):
        raise InputError("Source profile identity fields cannot be empty")
    encoded = [quote(part, safe="") for part in parts]
    return "v1|" + "|".join(encoded)


def archive_profile_path(base_dir: Path, output_format: OutputFormat | str, quality: str) -> Path:
    format_value = (
        output_format.value if isinstance(output_format, OutputFormat) else str(output_format)
    )
    readable = _PROFILE_SAFE.sub("-", f"{format_value}-{quality}".strip()).strip("-.") or "profile"
    digest = hashlib.sha256(f"{format_value}\0{quality}".encode()).hexdigest()[:12]
    return base_dir / f"{readable[:80]}-{digest}.txt"


def sha256_file(path: Path, *, chunk_size: int = _CHUNK_SIZE) -> str:
    if chunk_size < 1:
        raise InputError("SHA-256 chunk size must be at least 1 byte")
    if not path.is_file():
        raise InputError(f"Cannot hash missing or non-file path: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
    except OSError as exc:
        raise InputError(f"Could not read file for SHA-256: {exc}") from exc
    return digest.hexdigest()


class DownloadArchive:
    """Small atomic yt-dlp-compatible archive file for one output profile."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def contains(self, extractor: str, media_key: str) -> bool:
        token = self._token(extractor, media_key)
        if not self.path.exists():
            return False
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return any(line.rstrip("\r\n") == token for line in handle)
        except OSError as exc:
            raise InputError(f"Could not read download archive: {exc}") from exc

    def add(self, extractor: str, media_key: str) -> bool:
        token = self._token(extractor, media_key)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existing = self._read_lines()
            if token in existing:
                return False
            updated = [*existing, token]
            temp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
            try:
                temp.write_text("\n".join(updated) + "\n", encoding="utf-8")
                temp.replace(self.path)
            except OSError as exc:
                with suppress(OSError):
                    temp.unlink(missing_ok=True)
                raise InputError(f"Could not update download archive: {exc}") from exc
            return True

    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            return [
                line.rstrip("\r\n")
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError as exc:
            raise InputError(f"Could not read download archive: {exc}") from exc

    @staticmethod
    def _token(extractor: str, media_key: str) -> str:
        extractor = extractor.strip()
        media_key = media_key.strip()
        if not extractor or not media_key or any(char.isspace() for char in extractor):
            raise InputError("Archive extractor and media ID must be non-empty")
        if "\n" in media_key or "\r" in media_key:
            raise InputError("Archive media ID cannot contain newlines")
        return f"{extractor} {media_key}"


class BasicDedupeService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def check_source_profile(
        self,
        *,
        media_key: str,
        output_format: OutputFormat | str,
        quality: str,
        platform: str = "youtube",
    ) -> BasicDuplicateMatch:
        archive_key = source_profile_key(
            platform=platform,
            media_key=media_key,
            output_format=output_format,
            quality=quality,
        )
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, output_path FROM downloads
                WHERE source_archive_key = ? AND status = 'completed'
                ORDER BY id DESC LIMIT 1
                """,
                (archive_key,),
            ).fetchone()
        if row is None:
            return BasicDuplicateMatch(BasicDuplicateKind.NEW, media_key=media_key)

        download_id = int(row[0])
        recorded_path = str(row[1]) if row[1] is not None else None
        if recorded_path is None or not Path(recorded_path).is_file():
            self._invalidate_stale_download(download_id)
            return BasicDuplicateMatch(BasicDuplicateKind.NEW, media_key=media_key)

        return BasicDuplicateMatch(
            BasicDuplicateKind.SOURCE_PROFILE,
            media_key=media_key,
            path=recorded_path,
            download_id=download_id,
        )

    def check_exact_file(self, path: Path) -> BasicDuplicateMatch:
        return self.check_exact_sha256(sha256_file(path), candidate_path=path)

    def check_exact_sha256(
        self,
        digest: str,
        *,
        candidate_path: Path | None = None,
    ) -> BasicDuplicateMatch:
        normalized = digest.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise InputError("SHA-256 digest must contain exactly 64 hexadecimal characters")
        while True:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT f.path, f.download_id, m.media_key
                    FROM files f
                    JOIN downloads d ON d.id = f.download_id
                    JOIN media_items m ON m.id = d.media_item_id
                    WHERE f.sha256 = ? AND d.status = 'completed'
                    ORDER BY f.id DESC LIMIT 1
                    """,
                    (normalized,),
                ).fetchone()
            if row is None:
                return BasicDuplicateMatch(
                    BasicDuplicateKind.NEW,
                    path=str(candidate_path) if candidate_path is not None else None,
                    sha256=normalized,
                )

            recorded_path = Path(str(row[0]))
            download_id = int(row[1])
            if recorded_path.is_file():
                return BasicDuplicateMatch(
                    BasicDuplicateKind.EXACT_FILE,
                    media_key=str(row[2]),
                    path=str(recorded_path),
                    download_id=download_id,
                    sha256=normalized,
                )
            self._invalidate_stale_download(download_id)

    def register_completed_file(
        self,
        *,
        media_key: str,
        output_format: OutputFormat | str,
        quality: str,
        path: Path,
        job_item_id: int | None = None,
        platform: str = "youtube",
        precomputed_sha256: str | None = None,
    ) -> BasicDuplicateMatch:
        if not path.is_file():
            raise InputError(f"Completed output file does not exist: {path}")
        source_match = self.check_source_profile(
            media_key=media_key,
            output_format=output_format,
            quality=quality,
            platform=platform,
        )
        if source_match.is_duplicate:
            return source_match
        exact_match = (
            self.check_exact_sha256(precomputed_sha256, candidate_path=path)
            if precomputed_sha256 is not None
            else self.check_exact_file(path)
        )
        if exact_match.is_duplicate:
            self.mark_source_completed(
                media_key=media_key,
                output_format=output_format,
                quality=quality,
                output_path=exact_match.path,
                job_item_id=job_item_id,
                platform=platform,
            )
            return exact_match

        digest = exact_match.sha256
        assert digest is not None
        archive_key = source_profile_key(
            platform=platform,
            media_key=media_key,
            output_format=output_format,
            quality=quality,
        )
        format_value = (
            output_format.value if isinstance(output_format, OutputFormat) else str(output_format)
        )
        try:
            with self.database.transaction() as connection:
                media_id = self._media_id(connection, platform, media_key)
                cursor = connection.execute(
                    """
                    INSERT INTO downloads(
                        media_item_id, job_item_id, format, quality, output_path,
                        source_archive_key, file_size, sha256, status, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', CURRENT_TIMESTAMP)
                    """,
                    (
                        media_id,
                        job_item_id,
                        format_value,
                        quality,
                        str(path),
                        archive_key,
                        path.stat().st_size,
                        digest,
                    ),
                )
                download_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO files(download_id, path, size_bytes, sha256)
                    VALUES (?, ?, ?, ?)
                    """,
                    (download_id, str(path), path.stat().st_size, digest),
                )
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"Could not register completed duplicate state: {exc}") from exc
        return BasicDuplicateMatch(
            BasicDuplicateKind.NEW,
            media_key=media_key,
            path=str(path),
            download_id=download_id,
            sha256=digest,
        )

    def mark_source_completed(
        self,
        *,
        media_key: str,
        output_format: OutputFormat | str,
        quality: str,
        output_path: str | None,
        job_item_id: int | None = None,
        platform: str = "youtube",
    ) -> int:
        existing = self.check_source_profile(
            media_key=media_key,
            output_format=output_format,
            quality=quality,
            platform=platform,
        )
        if existing.download_id is not None:
            return existing.download_id
        archive_key = source_profile_key(
            platform=platform,
            media_key=media_key,
            output_format=output_format,
            quality=quality,
        )
        format_value = (
            output_format.value if isinstance(output_format, OutputFormat) else str(output_format)
        )
        with self.database.transaction() as connection:
            media_id = self._media_id(connection, platform, media_key)
            cursor = connection.execute(
                """
                INSERT INTO downloads(
                    media_item_id, job_item_id, format, quality, output_path,
                    source_archive_key, status, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', CURRENT_TIMESTAMP)
                """,
                (media_id, job_item_id, format_value, quality, output_path, archive_key),
            )
            return int(cursor.lastrowid)

    def _invalidate_stale_download(self, download_id: int) -> None:
        """Retire a completed record whose output file no longer exists."""

        with self.database.transaction() as connection:
            connection.execute("DELETE FROM files WHERE download_id = ?", (download_id,))
            connection.execute(
                "UPDATE downloads SET status = 'stale' WHERE id = ? AND status = 'completed'",
                (download_id,),
            )

    @staticmethod
    def _media_id(connection: sqlite3.Connection, platform: str, media_key: str) -> int:
        row = connection.execute(
            "SELECT id FROM media_items WHERE platform = ? AND media_key = ?",
            (platform, media_key),
        ).fetchone()
        if row is None:
            raise InputError(f"Cannot register duplicate state for unknown media: {media_key}")
        return int(row[0])
