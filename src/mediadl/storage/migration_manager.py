"""Versioned, checksum-verified SQLite migrations."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources

from mediadl.core.errors import DatabaseError


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    resource_name: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial", "001_initial.sql"),
    Migration(2, "index_cache", "002_index_cache.sql"),
    Migration(3, "job_resume", "003_job_resume.sql"),
)


def _resource_text(name: str) -> str:
    package = resources.files("mediadl.storage.migrations")
    return package.joinpath(name).read_text(encoding="utf-8")


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _iter_sql_statements(script: str) -> Iterable[str]:
    buffer: list[str] = []
    for line in script.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()
    remainder = "\n".join(buffer).strip()
    if remainder:
        raise DatabaseError("Migration SQL ended with an incomplete statement")


class MigrationManager:
    """Apply known migrations exactly once and detect edited historical migrations."""

    def ensure_migration_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    def current_version(self, connection: sqlite3.Connection) -> int:
        self.ensure_migration_table(connection)
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return int(row[0])

    def validate_applied(self, connection: sqlite3.Connection) -> None:
        self.ensure_migration_table(connection)
        known = {migration.version: migration for migration in MIGRATIONS}
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        for row in rows:
            version = int(row[0])
            migration = known.get(version)
            if migration is None:
                raise DatabaseError(
                    f"Database migration {version} is newer than this MediaDL build"
                )
            sql = _resource_text(migration.resource_name)
            expected_checksum = _checksum(sql)
            if row[1] != migration.name or row[2] != expected_checksum:
                raise DatabaseError(
                    f"Migration {version} does not match the installed MediaDL schema"
                )

    def apply_all(self, connection: sqlite3.Connection) -> int:
        self.ensure_migration_table(connection)
        self.validate_applied(connection)
        applied_versions = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }

        for migration in MIGRATIONS:
            if migration.version in applied_versions:
                continue
            self._apply_one(connection, migration)

        self.validate_applied(connection)
        return self.current_version(connection)

    def _apply_one(self, connection: sqlite3.Connection, migration: Migration) -> None:
        sql = _resource_text(migration.resource_name)
        checksum = _checksum(sql)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _iter_sql_statements(sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)",
                (migration.version, migration.name, checksum),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise DatabaseError(
                f"Could not apply database migration {migration.version}: {exc}"
            ) from exc
