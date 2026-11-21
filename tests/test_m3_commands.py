"""M3 authorization, share-link, and delivery command coverage."""

from __future__ import annotations

import re
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from media_bot.auth import admin_only
from media_bot.config import Settings, configure_admin_ids
from media_bot.db.database import Database
from media_bot.db.models import MediaItem, StorageMessageRef
from media_bot.db.repositories import ItemRepository, ShareLinkRepository, UploaderRepository
from media_bot.handlers.sharing import (
    adduser_command,
    redeem_command,
    revoke_command,
    share_command,
    start_command,
)
from media_bot.services.core import ShareLinkService
from media_bot.media_transfer import TransferCapabilities


ADMIN_ID = 710001
USER_ID = 710002


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "m3.db")
    await db.initialize()
    return db


def settings(database: Database, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "bot_token": "123:test-token",
        "storage_channel_id": -1007123456789,
        "admin_ids": (ADMIN_ID,),
        "db_path": database.path,
        "log_level": "INFO",
        "env": "dev",
        "max_relay_file_bytes": 2_147_483_648,
        "default_protect_content": True,
        "uploader_mode": "admin",
        "uploader_ids": (),
        "uploader_redeem_code": "verify-me",
        "required_channel_id": None,
        "verified_transfer_max_file_bytes": 2_147_483_648,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_update(
    user_id: int = USER_ID,
    *,
    chat_type: str = "private",
) -> SimpleNamespace:
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type=chat_type, id=user_id),
        effective_message=message,
    )


def make_context(
    database: Database,
    args: list[str] | None = None,
    *,
    settings_override: dict[str, object] | None = None,
    bot: SimpleNamespace | None = None,
) -> SimpleNamespace:
    app_settings = settings(database, **(settings_override or {}))
    bot = bot or SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=9001)),
        get_me=AsyncMock(return_value=SimpleNamespace(username="ShareTestBot")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        get_chat=AsyncMock(return_value=SimpleNamespace(username="required_channel")),
    )
    return SimpleNamespace(
        args=args or [],
        bot=bot,
        application=SimpleNamespace(
            bot_data={"settings": app_settings, "database": database}
        ),
    )


async def add_item(
    database: Database,
    *,
    references: tuple[StorageMessageRef, ...] | None = None,
    owner_user_id: int = ADMIN_ID,
) -> int:
    item = MediaItem(
        owner_user_id=owner_user_id,
        media_type="album" if references and len(references) > 1 else "document",
        file_size_bytes=123,
        storage_messages=references or (StorageMessageRef(-1007123456789, 202, 0),),
        file_name="example.bin",
        mime_type="application/octet-stream",
        protect_content=False,
    )
    saved = await ItemRepository(database).create_stored_item(item)
    assert saved.id is not None
    return saved.id


async def create_link(
    database: Database,
    item_id: int,
    *,
    expires_at_ms: int | None = None,
    max_uses: int | None = None,
) -> str:
    service = ShareLinkService(
        ShareLinkRepository(database),
        ItemRepository(database),
        TransferCapabilities(2_147_483_648),
    )
    return (await service.create(
        item_id=item_id,
        created_by_user_id=ADMIN_ID,
        expires_at_ms=expires_at_ms,
        max_uses=max_uses,
    )).token


async def test_schema_v5_has_uploader_and_rate_limit_tables(database: Database) -> None:
    version, writable = await database.health_check()
    assert version == 5
    assert writable
    async with database.connection() as connection:
        rows = await (await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
    names = {row["name"] for row in rows}
    assert {"users", "uploaders", "redeem_rate_limits"} <= names


async def test_uploader_repository_add_and_revoke_are_database_backed(database: Database) -> None:
    repository = UploaderRepository(database)
    assert await repository.add(user_id=USER_ID, added_by=ADMIN_ID)
    assert await repository.is_active(USER_ID)
    grant = await repository.get(USER_ID)
    assert grant is not None and grant.added_by == ADMIN_ID and grant.revoked_at is None
    assert await repository.revoke(user_id=USER_ID, revoked_by=ADMIN_ID)
    assert not await repository.is_active(USER_ID)
    revoked = await repository.get(USER_ID)
    assert revoked is not None and revoked.revoked_at is not None


async def test_uploader_regrant_restores_revoked_grant(database: Database) -> None:
    repository = UploaderRepository(database)
    await repository.add(user_id=USER_ID, added_by=ADMIN_ID)
    await repository.revoke(user_id=USER_ID, revoked_by=ADMIN_ID)
    assert await repository.add(user_id=USER_ID, added_by=ADMIN_ID)
    assert await repository.is_active(USER_ID)


async def test_redeem_success_creates_grant_and_audit(database: Database) -> None:
    repository = UploaderRepository(database)
    assert await repository.redeem(
        user_id=USER_ID,
        supplied_code="verify-me",
        configured_code="verify-me",
        now_ms=1_000_000,
    ) == "success"
    assert await repository.is_active(USER_ID)
    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT action FROM audit_log WHERE actor_id = ?", (USER_ID,)
        )).fetchone()
    assert row["action"] == "redeem_success"


async def test_redeem_failure_is_audited_without_persisting_code(database: Database) -> None:
    repository = UploaderRepository(database)
    assert await repository.redeem(
        user_id=USER_ID,
        supplied_code="wrong-secret",
        configured_code="verify-me",
        now_ms=2_000_000,
    ) == "invalid"
    assert not await repository.is_active(USER_ID)
    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT action, target_id FROM audit_log WHERE actor_id = ?", (USER_ID,)
        )).fetchone()
        limits = await (await connection.execute(
            "SELECT COUNT(*) FROM redeem_rate_limits WHERE user_id = ?", (USER_ID,)
        )).fetchone()
    assert row["action"] == "redeem_fail"
    assert row["target_id"] == str(USER_ID)
    assert int(limits[0]) == 1


async def test_redeem_rate_limits_second_attempt_within_30_seconds(database: Database) -> None:
    repository = UploaderRepository(database)
    assert await repository.redeem(
        user_id=USER_ID, supplied_code="bad", configured_code="verify-me", now_ms=3_000_000
    ) == "invalid"
    assert await repository.redeem(
        user_id=USER_ID, supplied_code="verify-me", configured_code="verify-me", now_ms=3_029_999
    ) == "rate_limited"
    assert not await repository.is_active(USER_ID)
    async with database.connection() as connection:
        rows = await (await connection.execute(
            "SELECT action FROM audit_log WHERE actor_id = ? ORDER BY id", (USER_ID,)
        )).fetchall()
    assert [row["action"] for row in rows] == ["redeem_fail", "redeem_fail"]


async def test_redeem_allows_attempt_after_cooldown(database: Database) -> None:
    repository = UploaderRepository(database)
    await repository.redeem(
        user_id=USER_ID, supplied_code="bad", configured_code="verify-me", now_ms=4_000_000
    )
    result = await repository.redeem(
        user_id=USER_ID, supplied_code="verify-me", configured_code="verify-me", now_ms=4_030_000
    )
    assert result == "success"
    assert await repository.is_active(USER_ID)


async def test_redeem_command_success_and_failure_responses(database: Database) -> None:
    update = make_update()
    context = make_context(database, ["verify-me"])
    await redeem_command(update, context)
    update.effective_message.reply_text.assert_awaited_once_with("Uploader access verified.")

    other_update = make_update(user_id=USER_ID + 1)
    other_context = make_context(database, ["not-it"])
    await redeem_command(other_update, other_context)
    other_update.effective_message.reply_text.assert_awaited_once_with("Invalid verification code.")


async def test_redeem_command_requires_private_chat(database: Database) -> None:
    update = make_update(chat_type="group")
    await redeem_command(update, make_context(database, ["verify-me"]))
    update.effective_message.reply_text.assert_awaited_once_with(
        "Redeem uploader codes in a private chat with the bot."
    )
    assert not await UploaderRepository(database).is_active(USER_ID)


async def test_admin_adduser_and_revoke_commands(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    update = make_update(user_id=ADMIN_ID)
    context = make_context(database, [str(USER_ID)])
    await admin_only(adduser_command)(update, context)
    assert await UploaderRepository(database).is_active(USER_ID)
    update.effective_message.reply_text.assert_awaited_once_with("Uploader access granted.")

    revoke_update = make_update(user_id=ADMIN_ID)
    await admin_only(revoke_command)(revoke_update, make_context(database, [str(USER_ID)]))
    assert not await UploaderRepository(database).is_active(USER_ID)
    revoke_update.effective_message.reply_text.assert_awaited_once_with("Uploader access revoked.")


@pytest.mark.parametrize("command", [adduser_command, revoke_command])
async def test_admin_uploader_commands_refuse_non_admins_and_audit(
    database: Database, command
) -> None:
    configure_admin_ids((ADMIN_ID,))
    update = make_update(user_id=USER_ID)
    context = make_context(database, [str(USER_ID)])
    await admin_only(command)(update, context)
    update.effective_message.reply_text.assert_awaited_once_with("Not authorized.")
    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT action, target_id FROM audit_log WHERE actor_id = ?", (USER_ID,)
        )).fetchone()
    assert row["action"] == "admin_denied"
    assert row["target_id"] == command.__name__


async def test_share_command_returns_bot_deeplink_and_persists_only_hash(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_id = await add_item(database)
    update = make_update(user_id=ADMIN_ID)
    context = make_context(database, [str(item_id)])
    await share_command(update, context)
    reply = update.effective_message.reply_text.await_args.args[0]
    match = re.fullmatch(r"https://t\.me/ShareTestBot\?start=([A-Za-z0-9_-]{43})", reply)
    assert match is not None
    raw_token = match.group(1)
    link = (await ShareLinkRepository(database).list_for_item(item_id))[0]
    assert raw_token.encode("ascii") not in link.token_hash
    assert link.token_hash != raw_token.encode("ascii")
    context.bot.get_me.assert_awaited_once()


async def test_share_denies_user_without_uploader_grant(database: Database) -> None:
    update = make_update(user_id=USER_ID)
    await share_command(update, make_context(database, ["1"]))
    update.effective_message.reply_text.assert_awaited_once_with("Not authorized.")


async def test_share_creation_fails_closed_without_verified_transfer_limit(
    database: Database,
) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_id = await add_item(database)
    update = make_update(user_id=ADMIN_ID)
    context = make_context(
        database,
        [str(item_id)],
        settings_override={"verified_transfer_max_file_bytes": None},
    )
    await share_command(update, context)
    update.effective_message.reply_text.assert_awaited_once_with(
        "Could not create a share link for that item."
    )
    assert await ShareLinkRepository(database).list_for_item(item_id) == ()


async def test_share_allows_active_uploader(database: Database) -> None:
    item_id = await add_item(database, owner_user_id=USER_ID)
    await UploaderRepository(database).add(user_id=USER_ID, added_by=ADMIN_ID)
    update = make_update(user_id=USER_ID)
    await share_command(update, make_context(database, [str(item_id)]))
    assert "https://t.me/ShareTestBot?start=" in update.effective_message.reply_text.await_args.args[0]


async def test_uploader_cannot_share_another_uploaders_item(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    uploader_a = USER_ID
    uploader_b = USER_ID + 1
    item_id = await add_item(database, owner_user_id=uploader_a)
    await UploaderRepository(database).add(user_id=uploader_a, added_by=ADMIN_ID)
    await UploaderRepository(database).add(user_id=uploader_b, added_by=ADMIN_ID)

    update = make_update(user_id=uploader_b)
    context = make_context(database, [str(item_id)])
    await share_command(update, context)

    update.effective_message.reply_text.assert_awaited_once_with(
        "You can only share your own files."
    )
    context.bot.get_me.assert_not_awaited()
    assert await ShareLinkRepository(database).list_for_item(item_id) == ()


async def test_admin_can_share_another_uploaders_item(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_id = await add_item(database, owner_user_id=USER_ID)
    update = make_update(user_id=ADMIN_ID)
    context = make_context(database, [str(item_id)])

    await share_command(update, context)

    assert "https://t.me/ShareTestBot?start=" in update.effective_message.reply_text.await_args.args[0]
    assert len(await ShareLinkRepository(database).list_for_item(item_id)) == 1


async def test_start_with_no_token_sends_welcome(database: Database) -> None:
    update = make_update()
    await start_command(update, make_context(database))
    update.effective_message.reply_text.assert_awaited_once_with(
        "Welcome. Open a valid share link to receive a file."
    )


async def test_start_delivers_valid_link_to_member_with_protection(database: Database) -> None:
    item_id = await add_item(database)
    token = await create_link(database, item_id)
    update = make_update()
    context = make_context(
        database,
        [token],
        settings_override={"required_channel_id": -100555},
    )
    await start_command(update, context)
    context.bot.get_chat_member.assert_awaited_once_with(chat_id=-100555, user_id=USER_ID)
    context.bot.copy_message.assert_awaited_once_with(
        chat_id=USER_ID,
        from_chat_id=-1007123456789,
        message_id=202,
        protect_content=True,
    )
    update.effective_message.reply_text.assert_awaited_once_with("Delivered.")
    link = (await ShareLinkRepository(database).list_for_item(item_id))[0]
    assert link.use_count == 1


async def test_start_delivers_all_messages_for_album(database: Database) -> None:
    item_id = await add_item(
        database,
        references=(
            StorageMessageRef(-1007123456789, 202, 0),
            StorageMessageRef(-1007123456789, 203, 1),
        ),
    )
    token = await create_link(database, item_id)
    update = make_update()
    context = make_context(database, [token])
    await start_command(update, context)
    assert context.bot.copy_message.await_count == 2
    assert [call.kwargs["message_id"] for call in context.bot.copy_message.await_args_list] == [202, 203]


async def test_start_fails_closed_without_verified_transfer_limit(database: Database) -> None:
    item_id = await add_item(database)
    token = await create_link(database, item_id)
    update = make_update()
    context = make_context(
        database,
        [token],
        settings_override={"verified_transfer_max_file_bytes": None},
    )
    await start_command(update, context)
    context.bot.copy_message.assert_not_awaited()
    assert "not verified" in update.effective_message.reply_text.await_args.args[0]
    link = (await ShareLinkRepository(database).list_for_item(item_id))[0]
    assert link.use_count == 0


@pytest.mark.parametrize("status", ["left", "kicked"])
async def test_start_refuses_nonmember_without_consuming_link(
    database: Database, status: str
) -> None:
    item_id = await add_item(database)
    token = await create_link(database, item_id)
    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status)),
        get_chat=AsyncMock(return_value=SimpleNamespace(username="sample_channel")),
    )
    update = make_update()
    context = make_context(
        database, [token], settings_override={"required_channel_id": -100777}, bot=bot
    )
    await start_command(update, context)
    bot.copy_message.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once_with(
        "Join @sample_channel to access."
    )
    link = (await ShareLinkRepository(database).list_for_item(item_id))[0]
    assert link.use_count == 0


async def test_start_fails_closed_when_membership_check_errors(database: Database) -> None:
    item_id = await add_item(database)
    token = await create_link(database, item_id)
    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        get_chat_member=AsyncMock(side_effect=BadRequest("Not enough rights to get members")),
    )
    update = make_update()
    context = make_context(
        database, [token], settings_override={"required_channel_id": -100777}, bot=bot
    )
    await start_command(update, context)
    bot.copy_message.assert_not_awaited()
    assert update.effective_message.reply_text.await_args.args[0] == (
        "Membership could not be verified. Please contact the bot administrator."
    )
    link = (await ShareLinkRepository(database).list_for_item(item_id))[0]
    assert link.use_count == 0


async def test_start_rejects_expired_token(database: Database) -> None:
    item_id = await add_item(database)
    now_ms = time.time_ns() // 1_000_000
    token = await create_link(database, item_id, expires_at_ms=now_ms + 20)
    await asyncio.sleep(0.03)
    update = make_update()
    context = make_context(database, [token])
    await start_command(update, context)
    context.bot.copy_message.assert_not_awaited()
    assert "invalid, expired" in update.effective_message.reply_text.await_args.args[0]


async def test_start_refuses_exhausted_use_limit(database: Database) -> None:
    item_id = await add_item(database)
    token = await create_link(database, item_id, max_uses=1)
    service = ShareLinkService(
        ShareLinkRepository(database),
        ItemRepository(database),
        TransferCapabilities(2_147_483_648),
    )
    assert await service.consume(token) is not None
    update = make_update()
    context = make_context(database, [token])
    await start_command(update, context)
    context.bot.copy_message.assert_not_awaited()
    assert "exhausted" in update.effective_message.reply_text.await_args.args[0]


async def test_share_link_use_limit_is_consumed_atomically(database: Database) -> None:
    item_id = await add_item(database)
    token = await create_link(database, item_id, max_uses=1)
    repository = ShareLinkRepository(database)
    digest = __import__("hashlib").sha256(token.encode("ascii")).digest()
    outcomes = await asyncio.gather(
        repository.consume_by_hash(digest, now_ms=time.time_ns() // 1_000_000),
        repository.consume_by_hash(digest, now_ms=time.time_ns() // 1_000_000),
    )
    assert sum(outcome is not None for outcome in outcomes) == 1


async def test_start_rejects_malformed_token(database: Database) -> None:
    update = make_update()
    context = make_context(database, ["not-a-secure-token"])
    await start_command(update, context)
    context.bot.copy_message.assert_not_awaited()
    assert "invalid, expired" in update.effective_message.reply_text.await_args.args[0]
