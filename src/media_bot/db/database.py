"""SQLite connection lifecycle, migrations, and WAL-safe backup/restore."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

import aiosqlite
from importlib.resources import files


class DatabaseError(RuntimeError):
    """Base error for database initialization and backup operations."""


class MigrationError(DatabaseError):
    """Raised when the database schema version is missing or unsupported."""


_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class Database:
    """Open short-lived SQLite connections with consistent safety settings."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise ValueError("busy_timeout_ms must be an integer")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")
        self.path = Path(path).expanduser()
        self.busy_timeout_ms = busy_timeout_ms

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield a configured connection; callers own transactions explicitly."""
        connection = await aiosqlite.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            await connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            await connection.close()

    async def initialize(self, *, storage_channel_id: int | None = None) -> None:
        """Create a private database file, apply migrations, and optionally seed Default."""
        try:
            if self.path.exists() and not self.path.is_file():
                raise DatabaseError("Database path exists but is not a file")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            os.chmod(self.path, 0o600)

            async with self.connection() as connection:
                cursor = await connection.execute("PRAGMA journal_mode = WAL")
                journal_row = await cursor.fetchone()
                if journal_row is None or str(journal_row[0]).lower() != "wal":
                    raise DatabaseError("SQLite could not enable WAL mode")

                cursor = await connection.execute("PRAGMA user_version")
                version_row = await cursor.fetchone()
                current_version = int(version_row[0])
                migrations = self._load_migrations()
                latest_version = migrations[-1][0] if migrations else 0
                if current_version > latest_version:
                    raise MigrationError(
                        f"Database schema version {current_version} is newer than this application supports ({latest_version})"
                    )
                if current_version and current_version not in {version for version, _ in migrations}:
                    raise MigrationError(
                        f"Database schema version {current_version} has no matching migration"
                    )

                for version, sql in migrations:
                    if version <= current_version:
                        continue
                    expected = current_version + 1
                    if version != expected:
                        raise MigrationError(
                            f"Missing SQL migration {expected:04d}; found {version:04d}"
                        )
                    try:
                        await connection.executescript(sql)
                    except sqlite3.Error as exc:
                        if connection.in_transaction:
                            await connection.rollback()
                        raise MigrationError(
                            f"Failed to apply database migration {version:04d}"
                        ) from exc
                    cursor = await connection.execute("PRAGMA user_version")
                    migrated_row = await cursor.fetchone()
                    current_version = int(migrated_row[0])
                    if current_version != version:
                        raise MigrationError(
                            f"Migration {version:04d} did not set the expected schema version"
                        )
                if storage_channel_id is not None:
                    if (
                        isinstance(storage_channel_id, bool)
                        or not isinstance(storage_channel_id, int)
                        or storage_channel_id == 0
                    ):
                        raise DatabaseError("storage_channel_id must be a non-zero integer")
                    await connection.execute("BEGIN IMMEDIATE")
                    await connection.execute(
                        """UPDATE categories SET storage_channel_id = ?
                           WHERE name = 'Default' AND storage_channel_id IS NULL""",
                        (storage_channel_id,),
                    )
                    await connection.execute(
                        """UPDATE items SET storage_channel_id = ?
                           WHERE storage_channel_id IS NULL""",
                        (storage_channel_id,),
                    )
                    await connection.commit()
        except (DatabaseError, MigrationError):
            raise
        except (OSError, sqlite3.Error, aiosqlite.Error) as exc:
            raise DatabaseError("Could not initialize the SQLite database") from exc

    @classmethod
    def latest_schema_version(cls) -> int:
        """Return the newest contiguous schema version packaged with this build."""
        migrations = cls._load_migrations()
        return migrations[-1][0] if migrations else 0

    async def health_check(self) -> tuple[int, bool]:
        """Read schema version and probe write access without persistent changes."""
        async with self.connection() as connection:
            cursor = await connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            version = int(row[0]) if row is not None else 0
            writable = False
            try:
                await connection.execute("BEGIN IMMEDIATE")
                await connection.execute(f"PRAGMA user_version = {version}")
                await connection.rollback()
                writable = True
            except (sqlite3.Error, aiosqlite.Error):
                if connection.in_transaction:
                    await connection.rollback()
            return version, writable

    @staticmethod
    def _load_migrations() -> list[tuple[int, str]]:
        migration_package = files("media_bot.db.migrations")
        migrations: list[tuple[int, str]] = []
        for resource in migration_package.iterdir():
            match = _MIGRATION_NAME.fullmatch(resource.name)
            if match is not None and resource.is_file():
                migrations.append((int(match.group(1)), resource.read_text(encoding="utf-8")))
        migrations.sort(key=lambda item: item[0])
        versions = [version for version, _ in migrations]
        if len(versions) != len(set(versions)):
            raise MigrationError("Multiple migration files use the same version")
        if versions and versions[0] != 1:
            raise MigrationError("The migration sequence must start at version 0001")
        if any(right != left + 1 for left, right in zip(versions, versions[1:])):
            raise MigrationError("The SQL migration sequence contains a version gap")
        return migrations

    async def backup_to(self, destination: Path) -> Path:
        """Create an atomic, mode-0600 SQLite snapshot using SQLite's backup API.

        The destination must not already exist. The online backup API includes
        committed WAL contents without requiring a manual filesystem copy.
        """
        destination = Path(destination).expanduser()
        self._ensure_distinct_paths(self.path, destination)
        if not self.path.is_file():
            raise DatabaseError("The source database does not exist")
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._temporary_path(destination.parent, destination.name)
        try:
            async with self.connection() as source:
                cursor = await source.execute("PRAGMA user_version")
                row = await cursor.fetchone()
                if row is None or int(row[0]) < 1:
                    raise DatabaseError("The source database is not initialized")
                async with aiosqlite.connect(temporary) as target:
                    await source.backup(target)
                    await target.commit()
            await self._validate_database_file(temporary)
            os.link(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    async def restore_from(cls, source: Path, destination: Path) -> Path:
        """Validate a backup and atomically restore it to the stopped bot's DB path.

        The running service must be stopped before calling this method. The
        backup is copied to a private temporary database and checked for
        integrity, foreign-key consistency, and a supported schema version
        before replacing the destination.
        """
        source = Path(source).expanduser()
        destination = Path(destination).expanduser()
        cls._ensure_distinct_paths(source, destination)
        if not source.is_file():
            raise DatabaseError("The backup file does not exist")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = cls._temporary_path(destination.parent, destination.name)
        try:
            async with aiosqlite.connect(source) as backup:
                async with aiosqlite.connect(temporary) as target:
                    await backup.backup(target)
                    await target.commit()
            await cls._validate_database_file(temporary)
            supported_versions = [version for version, _ in cls._load_migrations()]
            async with aiosqlite.connect(temporary) as check:
                cursor = await check.execute("PRAGMA user_version")
                row = await cursor.fetchone()
                version = int(row[0]) if row is not None else 0
            if not supported_versions or version not in supported_versions:
                raise MigrationError("The backup schema version is not supported by this application")

            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(destination) + suffix).unlink(missing_ok=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            await cls(destination).initialize()
            return destination
        except (sqlite3.Error, aiosqlite.Error) as exc:
            raise DatabaseError("Could not read or restore the SQLite backup") from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _temporary_path(directory: Path, prefix: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=f".{prefix}.", suffix=".tmp", dir=directory)
        os.close(descriptor)
        return Path(name)

    @staticmethod
    def _ensure_distinct_paths(source: Path, destination: Path) -> None:
        if source.resolve() == destination.resolve():
            raise DatabaseError("Source and destination database paths must differ")

    @classmethod
    async def _validate_database_file(cls, path: Path) -> None:
        uri = f"file:{quote(str(path.resolve()))}?mode=ro"
        try:
            async with aiosqlite.connect(uri, uri=True) as connection:
                cursor = await connection.execute("PRAGMA integrity_check")
                result = await cursor.fetchall()
                if len(result) != 1 or result[0][0] != "ok":
                    raise DatabaseError("Database integrity check failed")
                cursor = await connection.execute("PRAGMA foreign_key_check")
                if await cursor.fetchone() is not None:
                    raise DatabaseError("Database contains inconsistent foreign-key records")
        except (sqlite3.Error, aiosqlite.Error) as exc:
            raise DatabaseError("Could not validate the SQLite database file") from exc
