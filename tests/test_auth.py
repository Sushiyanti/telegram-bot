"""Tests for configuration-driven admin authorization and its audit trail."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from media_bot.auth import admin_only, is_admin
from media_bot.config import configure_admin_ids
from media_bot.db.admins import AdminRepository
from media_bot.db.database import Database


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "bot.db")
    await db.initialize()
    return db


def make_context(database: Database) -> SimpleNamespace:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"database": database})
    )


def make_update(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )


async def test_is_admin_uses_configured_admin_ids_without_database() -> None:
    configure_admin_ids((123456789,))

    assert await is_admin(123456789) is True
    assert await is_admin(123456788) is False
    assert await is_admin(0) is False
    assert await is_admin(True) is False


async def test_is_admin_uses_database_fallback_and_configured_admin_survives(database: Database) -> None:
    configure_admin_ids((123456789,))
    admins = AdminRepository(database)
    await admins.seed_configured((123456789,))
    assert await admins.add(user_id=123456788, added_by=123456789)

    assert await is_admin(123456788, database) is True
    assert await is_admin(123456787, database) is False
    assert await is_admin(123456789, database) is True


async def test_configured_admin_seeding_is_idempotent_and_preserves_db_only_roles(database: Database) -> None:
    admins = AdminRepository(database)
    await admins.add(user_id=123456788, added_by=123456789)

    assert await admins.seed_configured((123456789,)) == 1
    assert await admins.seed_configured((123456789,)) == 0
    assert await admins.is_admin(123456788)
    assert await admins.is_admin(123456789)


async def test_admin_only_allows_configured_admin_without_audit(database: Database) -> None:
    configure_admin_ids((123456789,))
    calls: list[int] = []

    @admin_only
    async def restricted_command(update, context) -> None:
        del context
        calls.append(update.effective_user.id)

    update = make_update(123456789)
    await restricted_command(update, make_context(database))

    assert calls == [123456789]
    update.effective_message.reply_text.assert_not_awaited()
    async with database.connection() as connection:
        rows = await (await connection.execute("SELECT * FROM audit_log")).fetchall()
    assert rows == []


async def test_admin_only_denies_and_audits_non_admin(database: Database) -> None:
    configure_admin_ids((999999999,))
    calls: list[int] = []

    @admin_only
    async def restricted_command(update, context) -> None:
        del context
        calls.append(update.effective_user.id)

    update = make_update(123456789)
    await restricted_command(update, make_context(database))

    assert calls == []
    update.effective_message.reply_text.assert_awaited_once_with("Not authorized.")
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM audit_log")).fetchone()
    assert row["actor_id"] == 123456789
    assert row["action"] == "admin_denied"
    assert row["target_type"] == "command"
    assert row["target_id"] == "restricted_command"


async def test_audit_log_rejects_update_and_delete(database: Database) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """INSERT INTO audit_log
               (actor_id, action, target_type, target_id, created_at_ms)
               VALUES (1, 'admin_denied', 'command', 'sample_handler', 1)"""
        )
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await connection.execute("UPDATE audit_log SET target_id = 'changed' WHERE id = 1")
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await connection.execute("DELETE FROM audit_log WHERE id = 1")
