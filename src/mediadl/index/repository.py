"""SQLite-backed source/media index and metadata freshness cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mediadl.core.errors import InputError
from mediadl.index.models import IndexSummary
from mediadl.sources.models import MediaItemStub, ScanResult, SourceDescriptor
from mediadl.storage.database import Database


class IndexRepository:
    """Persist scans without duplicating media shared by multiple collections."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def index_scan(self, scan: ScanResult, *, complete_scan: bool) -> IndexSummary:
        with self.database.transaction() as connection:
            source_id = self._upsert_source(connection, scan.source, scan.title)
            active_before = {
                int(row[0])
                for row in connection.execute(
                    "SELECT media_item_id FROM source_media WHERE source_id = ? AND is_present = 1",
                    (source_id,),
                ).fetchall()
            }

            added = 0
            metadata_updated = 0
            stats_updated = 0
            unchanged = 0
            seen: set[int] = set()

            for position, item in enumerate(scan.items):
                media_id, was_added, metadata_changed = self._upsert_media(
                    connection,
                    source_id,
                    item,
                )
                stats_changed = self._upsert_stats(connection, media_id, item, was_added)
                self._upsert_membership(connection, source_id, media_id, position)
                seen.add(media_id)

                if was_added:
                    added += 1
                else:
                    if metadata_changed:
                        metadata_updated += 1
                    if stats_changed:
                        stats_updated += 1
                    if not metadata_changed and not stats_changed:
                        unchanged += 1

            missing = 0
            if complete_scan:
                missing_ids = active_before.difference(seen)
                if missing_ids:
                    placeholders = ",".join("?" for _ in missing_ids)
                    connection.execute(
                        f"UPDATE source_media SET is_present = 0, last_seen_at = CURRENT_TIMESTAMP "
                        f"WHERE source_id = ? AND media_item_id IN ({placeholders})",
                        (source_id, *sorted(missing_ids)),
                    )
                    missing = len(missing_ids)

            cursor = connection.execute(
                """
                INSERT INTO source_scans(
                    source_id,
                    complete_scan,
                    item_count,
                    reported_count,
                    skipped_entries,
                    added_count,
                    metadata_updated_count,
                    stats_updated_count,
                    unchanged_count,
                    missing_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    int(complete_scan),
                    scan.item_count,
                    scan.reported_count,
                    scan.skipped_entries,
                    added,
                    metadata_updated,
                    stats_updated,
                    unchanged,
                    missing,
                ),
            )
            scan_id = int(cursor.lastrowid)

        return IndexSummary(
            source_id=source_id,
            scan_id=scan_id,
            added_count=added,
            metadata_updated_count=metadata_updated,
            stats_updated_count=stats_updated,
            unchanged_count=unchanged,
            missing_count=missing,
            complete_scan=complete_scan,
        )

    def list_source_items(self, source: SourceDescriptor) -> tuple[MediaItemStub, ...]:
        with self.database.connection() as connection:
            source_row = connection.execute(
                """
                SELECT id FROM sources
                WHERE platform = ? AND source_type = ? AND source_key = ?
                """,
                (source.platform, source.kind.value, source.source_key),
            ).fetchone()
            if source_row is None:
                return ()
            rows = connection.execute(
                """
                SELECT
                    m.media_key,
                    m.title,
                    m.url,
                    m.duration_seconds,
                    m.upload_date,
                    s.view_count,
                    s.like_count,
                    s.comment_count,
                    m.channel,
                    m.channel_id,
                    m.availability,
                    m.media_type
                FROM source_media sm
                JOIN media_items m ON m.id = sm.media_item_id
                LEFT JOIN media_stats s ON s.media_item_id = m.id
                WHERE sm.source_id = ? AND sm.is_present = 1
                ORDER BY sm.position ASC, m.id ASC
                """,
                (int(source_row[0]),),
            ).fetchall()

        return tuple(
            MediaItemStub(
                media_key=str(row[0]),
                title=str(row[1]),
                url=str(row[2]),
                duration_seconds=float(row[3]) if row[3] is not None else None,
                upload_date=str(row[4]) if row[4] is not None else None,
                view_count=int(row[5]) if row[5] is not None else None,
                like_count=int(row[6]) if row[6] is not None else None,
                comment_count=int(row[7]) if row[7] is not None else None,
                channel=str(row[8]) if row[8] is not None else None,
                channel_id=str(row[9]) if row[9] is not None else None,
                availability=str(row[10]),
                media_type=str(row[11]),
            )
            for row in rows
        )

    def needs_refresh(
        self,
        media_key: str,
        group_name: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        if not group_name.strip():
            raise InputError("Metadata refresh group cannot be empty")
        now = _utc(now)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT r.status, r.expires_at
                FROM media_items m
                LEFT JOIN metadata_refreshes r
                    ON r.media_item_id = m.id AND r.group_name = ?
                WHERE m.platform = 'youtube' AND m.media_key = ?
                """,
                (group_name, media_key),
            ).fetchone()
        if row is None or row[0] is None:
            return True
        if str(row[0]) != "ok":
            return True
        expires_at = _parse_time(row[1])
        return expires_at is None or expires_at <= now

    def mark_refresh(
        self,
        media_key: str,
        group_name: str,
        *,
        ttl_seconds: int,
        fetched_at: datetime | None = None,
        status: str = "ok",
        error_message: str | None = None,
    ) -> None:
        if ttl_seconds < 0:
            raise InputError("Metadata refresh TTL cannot be negative")
        if not group_name.strip():
            raise InputError("Metadata refresh group cannot be empty")
        fetched = _utc(fetched_at)
        expires = fetched + timedelta(seconds=ttl_seconds)

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM media_items WHERE platform = 'youtube' AND media_key = ?",
                (media_key,),
            ).fetchone()
            if row is None:
                raise InputError(f"Cannot mark refresh for unknown media item: {media_key}")
            connection.execute(
                """
                INSERT INTO metadata_refreshes(
                    media_item_id, group_name, fetched_at, expires_at, status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_item_id, group_name) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at,
                    status = excluded.status,
                    error_message = excluded.error_message
                """,
                (
                    int(row[0]),
                    group_name,
                    _format_time(fetched),
                    _format_time(expires),
                    status,
                    error_message,
                ),
            )

    @staticmethod
    def _upsert_source(connection: Any, source: SourceDescriptor, title: str) -> int:
        row = connection.execute(
            """
            SELECT id FROM sources
            WHERE platform = ? AND source_type = ? AND source_key = ?
            """,
            (source.platform, source.kind.value, source.source_key),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO sources(platform, source_type, source_key, url, title)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source.platform, source.kind.value, source.source_key, source.url, title),
            )
            return int(cursor.lastrowid)

        source_id = int(row[0])
        connection.execute(
            """
            UPDATE sources
            SET url = ?, title = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (source.url, title, source_id),
        )
        return source_id

    @classmethod
    def _upsert_media(
        cls,
        connection: Any,
        source_id: int,
        item: MediaItemStub,
    ) -> tuple[int, bool, bool]:
        row = connection.execute(
            """
            SELECT id, url, title, channel, channel_id, upload_date,
                   duration_seconds, media_type, availability
            FROM media_items
            WHERE platform = 'youtube' AND media_key = ?
            """,
            (item.media_key,),
        ).fetchone()

        values = (
            item.url,
            item.title,
            item.channel,
            item.channel_id,
            item.upload_date,
            item.duration_seconds,
            item.media_type,
            item.availability,
        )
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO media_items(
                    platform, media_key, source_id, url, title, channel, channel_id,
                    upload_date, duration_seconds, media_type, availability
                ) VALUES ('youtube', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item.media_key, source_id, *values),
            )
            return int(cursor.lastrowid), True, True

        media_id = int(row[0])
        existing = tuple(row[index] for index in range(1, 9))
        changed = existing != values
        if changed:
            connection.execute(
                """
                UPDATE media_items SET
                    url = ?, title = ?, channel = ?, channel_id = ?, upload_date = ?,
                    duration_seconds = ?, media_type = ?, availability = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (*values, media_id),
            )
        return media_id, False, changed

    @staticmethod
    def _upsert_stats(
        connection: Any,
        media_id: int,
        item: MediaItemStub,
        was_added: bool,
    ) -> bool:
        incoming = (item.view_count, item.like_count, item.comment_count)
        if incoming == (None, None, None):
            return False
        row = connection.execute(
            "SELECT view_count, like_count, comment_count FROM media_stats WHERE media_item_id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO media_stats(media_item_id, view_count, like_count, comment_count)
                VALUES (?, ?, ?, ?)
                """,
                (media_id, *incoming),
            )
            return not was_added
        existing = (row[0], row[1], row[2])
        if existing == incoming:
            return False
        connection.execute(
            """
            UPDATE media_stats
            SET view_count = ?, like_count = ?, comment_count = ?, fetched_at = CURRENT_TIMESTAMP
            WHERE media_item_id = ?
            """,
            (*incoming, media_id),
        )
        return True

    @staticmethod
    def _upsert_membership(
        connection: Any,
        source_id: int,
        media_id: int,
        position: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO source_media(source_id, media_item_id, position, is_present)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(source_id, media_item_id) DO UPDATE SET
                position = excluded.position,
                is_present = 1,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (source_id, media_id, position),
        )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None
