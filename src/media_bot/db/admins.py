"""Persistence for database-backed administrator roles."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from media_bot.db.database import Database
from media_bot.db.repositories import InvalidRecord, _require_positive_int


@dataclass(frozen=True, slots=True)
class AdminGrant:
    user_id: int
    added_by: int | None
    created_at: str


class AdminRepository:
    """Persist database admins while allowing the environment to bootstrap access."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def seed_configured(self, user_ids: Iterable[int]) -> int:
        """Idempotently ensure every configured super-admin has a database row."""
        ids = tuple(user_ids)
        if len(set(ids)) != len(ids):
            raise InvalidRecord("Configured admin IDs must be unique")
        for user_id in ids:
            _require_positive_int(user_id, "user_id")
        created_at = _now_iso()
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            inserted = 0
            for user_id in ids:
                await connection.execute(
                    "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
                )
                cursor = await connection.execute(
                    """INSERT INTO admins (user_id, added_by, created_at)
                       VALUES (?, NULL, ?)
                       ON CONFLICT(user_id) DO NOTHING""",
                    (user_id, created_at),
                )
                inserted += max(0, cursor.rowcount)
            await connection.commit()
        return inserted

    async def is_admin(self, user_id: int) -> bool:
        _require_positive_int(user_id, "user_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
            return await cursor.fetchone() is not None

    async def list_all(self) -> tuple[AdminGrant, ...]:
        total, grants = await self.list_page(offset=0, limit=1_000_000)
        del total
        return grants

    async def list_page(
        self, *, offset: int = 0, limit: int = 10
    ) -> tuple[int, tuple[AdminGrant, ...]]:
        _validate_page(offset, limit)
        async with self.database.connection() as connection:
            total_row = await (await connection.execute("SELECT COUNT(*) FROM admins")).fetchone()
            cursor = await connection.execute(
                "SELECT user_id, added_by, created_at FROM admins ORDER BY user_id LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cursor.fetchall()
        return int(total_row[0]), tuple(
            AdminGrant(
                user_id=int(row["user_id"]),
                added_by=None if row["added_by"] is None else int(row["added_by"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    async def add(self, *, user_id: int, added_by: int) -> bool:
        """Grant the role and append an audit record in one transaction."""
        _require_positive_int(user_id, "user_id")
        _require_positive_int(added_by, "added_by")
        created_at = _now_iso()
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            cursor = await connection.execute(
                """INSERT INTO admins (user_id, added_by, created_at)
                   VALUES (?, ?, ?) ON CONFLICT(user_id) DO NOTHING""",
                (user_id, added_by, created_at),
            )
            changed = cursor.rowcount == 1
            if changed:
                await connection.execute(
                    """INSERT INTO audit_log
                       (actor_id, action, target_type, target_id, created_at_ms)
                       VALUES (?, 'admin_added', 'user', ?, ?)""",
                    (added_by, str(user_id), _now_ms()),
                )
            await connection.commit()
        return changed

    async def remove(self, *, user_id: int, removed_by: int) -> bool:
        """Remove a database role and audit the change atomically."""
        _require_positive_int(user_id, "user_id")
        _require_positive_int(removed_by, "removed_by")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            changed = cursor.rowcount == 1
            action = "admin_removed" if changed else "admin_remove_missing"
            await connection.execute(
                """INSERT INTO audit_log
                   (actor_id, action, target_type, target_id, created_at_ms)
                   VALUES (?, ?, 'user', ?, ?)""",
                (removed_by, action, str(user_id), _now_ms()),
            )
            await connection.commit()
        return changed


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _validate_page(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidRecord("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000_000:
        raise InvalidRecord("limit must be between 1 and 1,000,000")
