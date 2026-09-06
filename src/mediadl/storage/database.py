"""SQLite connection, initialization, and transaction boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mediadl.core.errors import DatabaseError
from mediadl.core.paths import AppPaths, get_app_paths
from mediadl.storage.migration_manager import MigrationManager


class Database:
    def __init__(self, path: str | Path, *, timeout_seconds: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.migrations = MigrationManager()

    def _prepare_parent(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        self._prepare_parent()
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            return connection
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not open MediaDL database: {exc}") from exc

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> int:
        with self.connection() as connection:
            return self.migrations.apply_all(connection)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def integrity_check(self) -> str:
        with self.connection() as connection:
            try:
                row = connection.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.Error as exc:
                raise DatabaseError(f"Database integrity check failed: {exc}") from exc
        return str(row[0]) if row is not None else "unknown"


def application_database(paths: AppPaths | None = None) -> Database:
    paths = paths or get_app_paths()
    return Database(paths.database_file)
