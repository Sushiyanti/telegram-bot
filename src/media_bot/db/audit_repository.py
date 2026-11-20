"""Append-only persistence for security-relevant authorization events."""

from __future__ import annotations

import sqlite3

from media_bot.db.database import Database
from media_bot.db.repositories import (
    InvalidRecord,
    _now_ms,
    _require_positive_int,
    _validate_text,
)


class AuditLogRepository:
    """Record security events without update or delete operations."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def record_event(
        self,
        *,
        actor_id: int,
        action: str,
        target_type: str,
        target_id: str,
    ) -> int:
        """Append a sanitized domain event to the audit log."""
        _require_positive_int(actor_id, "actor_id")
        _validate_text(action, "action", max_length=64)
        _validate_text(target_type, "target_type", max_length=64)
        _validate_text(target_id, "target_id", max_length=128)
        try:
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    """INSERT INTO audit_log
                       (actor_id, action, target_type, target_id, created_at_ms)
                       VALUES (?, ?, ?, ?, ?)""",
                    (actor_id, action, target_type, target_id, _now_ms()),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Audit event violates database constraints") from exc

    async def record_admin_denied(self, *, actor_id: int, target_id: str) -> int:
        """Append an admin_denied event for the named command handler."""
        return await self.record_event(
            actor_id=actor_id,
            action="admin_denied",
            target_type="command",
            target_id=target_id,
        )
