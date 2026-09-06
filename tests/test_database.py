import sqlite3
from pathlib import Path

import pytest

from mediadl.core.errors import DatabaseError
from mediadl.storage.database import Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "mediadl.sqlite3")
    assert db.initialize() == 3
    return db


def test_initial_migration_creates_expected_tables(database: Database) -> None:
    expected = {
        "schema_migrations",
        "sources",
        "media_items",
        "media_stats",
        "jobs",
        "job_items",
        "downloads",
        "files",
        "fingerprints",
        "duplicate_groups",
        "duplicate_members",
        "failures",
        "settings",
        "source_media",
        "source_scans",
        "metadata_refreshes",
    }
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

    assert expected.issubset({str(row[0]) for row in rows})
    assert database.integrity_check() == "ok"


def test_migrations_are_idempotent(database: Database) -> None:
    assert database.initialize() == 3
    assert database.initialize() == 3

    with database.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 3


def test_foreign_keys_are_enforced(database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO media_items(platform, media_key, source_id, url, title)
            VALUES ('youtube', 'abc', 999, 'https://example.invalid/abc', 'Example')
            """
        )


def test_transaction_rolls_back_on_failure(database: Database) -> None:
    with pytest.raises(RuntimeError), database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO sources(platform, source_type, source_key, url, title)
            VALUES ('youtube', 'channel', 'chan', 'https://example.invalid/chan', 'Channel')
            """
        )
        raise RuntimeError("force rollback")

    with database.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert count == 0


def test_negative_stats_are_rejected(database: Database) -> None:
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO media_items(platform, media_key, url, title)
            VALUES ('youtube', 'abc', 'https://example.invalid/abc', 'Example')
            """
        )
        media_id = int(cursor.lastrowid)

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            "INSERT INTO media_stats(media_item_id, view_count) VALUES (?, ?)",
            (media_id, -1),
        )


def test_migration_checksum_tampering_is_detected(database: Database) -> None:
    with database.transaction() as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1"
        )

    with pytest.raises(DatabaseError, match="does not match"):
        database.initialize()
