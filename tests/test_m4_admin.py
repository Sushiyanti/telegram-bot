"""M4 admin management and missing-storage resilience coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from media_bot.auth import admin_only
from media_bot.config import Settings, configure_admin_ids
from media_bot.db.database import Database
from media_bot.db.models import ItemFile, MediaItem
from media_bot.db.repositories import ItemRepository, ShareLinkRepository, UploaderRepository
from media_bot.handlers.admin import delete_command, list_command
from media_bot.handlers.sharing import revoke_command, start_command
from media_bot.media_transfer import StorageMessageRef, TransferCapabilities
from media_bot.services.core import ShareLinkService


ADMIN_ID = 710001
USER_ID = 710002
STORAGE_CHANNEL_ID = -1007123456789


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "m4.db")
    await db.initialize()
    return db


def settings(database: Database, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "bot_token": "123:test-token",
        "storage_channel_id": STORAGE_CHANNEL_ID,
        "admin_ids": (ADMIN_ID,),
        "db_path": database.path,
        "log_level": "INFO",
        "env": "dev",
        "max_relay_file_bytes": 2_147_483_648,
        "default_protect_content": True,
        "uploader_mode": "admin",
        "uploader_ids": (),
        "uploader_redeem_code": None,
        "required_channel_id": None,
        "verified_transfer_max_file_bytes": 2_147_483_648,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def make_update(user_id: int = USER_ID) -> SimpleNamespace:
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type="private", id=user_id),
        effective_message=message,
    )


def make_context(
    database: Database,
    args: list[str] | None = None,
    *,
    bot: SimpleNamespace | None = None,
) -> SimpleNamespace:
    telegram_bot = bot or SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=9001)),
        delete_message=AsyncMock(),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        get_chat=AsyncMock(return_value=SimpleNamespace(username="required_channel")),
    )
    return SimpleNamespace(
        args=args or [],
        bot=telegram_bot,
        application=SimpleNamespace(
            bot_data={"settings": settings(database), "database": database}
        ),
    )


async def add_item(
    database: Database,
    *,
    owner_user_id: int = USER_ID,
    message_id: int = 202,
    created_at_ms: int = 1_700_000_000_000,
    with_file_metadata: bool = False,
) -> int:
    stored_files = ()
    if with_file_metadata:
        stored_files = (
            ItemFile(
                sequence=0,
                media_type="document",
                file_id="CONFIDENTIAL_FILE_ID",
                file_unique_id="fingerprint-private-01",
                file_size_bytes=321,
                file_name="private.bin",
                mime_type="application/octet-stream",
            ),
        )
    saved = await ItemRepository(database).create_stored_item(
        MediaItem(
            owner_user_id=owner_user_id,
            media_type="document",
            file_size_bytes=321,
            storage_messages=(StorageMessageRef(STORAGE_CHANNEL_ID, message_id, 0),),
            file_name="private.bin",
            mime_type="application/octet-stream",
            created_at_ms=created_at_ms,
            files=stored_files,
        )
    )
    assert saved.id is not None
    return saved.id


async def create_link(database: Database, item_id: int) -> str:
    service = ShareLinkService(
        ShareLinkRepository(database),
        ItemRepository(database),
        TransferCapabilities(2_147_483_648),
    )
    return (
        await service.create(item_id=item_id, created_by_user_id=ADMIN_ID)
    ).token


async def test_admin_list_shows_recent_metadata_but_never_file_ids(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    await add_item(database, with_file_metadata=True)
    update = make_update(ADMIN_ID)

    await admin_only(list_command)(update, make_context(database))

    response = update.effective_message.reply_text.await_args.args[0]
    assert "Recent items:" in response
    assert "uploader 710002" in response
    assert "document" in response
    assert "321 B" in response
    assert "2023-11-14 22:13 UTC" in response
    assert "CONFIDENTIAL_FILE_ID" not in response
    assert "fingerprint-private-01" not in response


async def test_list_defaults_to_ten_recent_items(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_ids = []
    for index in range(12):
        item_ids.append(
            await add_item(
                database,
                message_id=500 + index,
                created_at_ms=1_700_000_000_000 + index,
            )
        )
    update = make_update(ADMIN_ID)

    await admin_only(list_command)(update, make_context(database))

    lines = update.effective_message.reply_text.await_args.args[0].splitlines()[1:]
    listed_ids = [int(line.split("|", maxsplit=1)[0].strip()) for line in lines]
    assert listed_ids == list(reversed(item_ids[-10:]))


async def test_list_refuses_non_admin_and_audits_denial(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_id = await add_item(database)
    update = make_update(USER_ID)

    await admin_only(list_command)(update, make_context(database))

    update.effective_message.reply_text.assert_awaited_once_with("Not authorized.")
    assert await ItemRepository(database).get(item_id) is not None
    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT action, target_id FROM audit_log WHERE actor_id = ?", (USER_ID,)
        )).fetchone()
    assert row["action"] == "admin_denied"
    assert row["target_id"] == "list_command"


async def test_admin_delete_cleans_database_dependencies_and_storage_posts(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_id = await add_item(database, with_file_metadata=True)
    token = await create_link(database, item_id)
    update = make_update(ADMIN_ID)
    context = make_context(database, [str(item_id)])

    await admin_only(delete_command)(update, context)

    assert await ItemRepository(database).get(item_id) is None
    assert await ShareLinkRepository(database).find_active_by_hash(
        __import__("hashlib").sha256(token.encode("ascii")).digest(),
        now_ms=1_800_000_000_000,
    ) is None
    async with database.connection() as connection:
        for table in (
            "item_storage_messages",
            "share_links",
            "item_files",
            "item_file_fingerprints",
        ):
            row = await (await connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE item_id = ?", (item_id,)
            )).fetchone()
            assert int(row[0]) == 0
    context.bot.delete_message.assert_awaited_once_with(
        chat_id=STORAGE_CHANNEL_ID, message_id=202
    )
    update.effective_message.reply_text.assert_awaited_once_with(
        f"Item {item_id} deleted; removed 1 storage message(s)."
    )


async def test_delete_refuses_non_admin_and_leaves_item_intact(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_id = await add_item(database)
    update = make_update(USER_ID)
    context = make_context(database, [str(item_id)])

    await admin_only(delete_command)(update, context)

    update.effective_message.reply_text.assert_awaited_once_with("Not authorized.")
    assert await ItemRepository(database).get(item_id) is not None
    context.bot.delete_message.assert_not_awaited()


async def test_delete_handles_already_missing_storage_message(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_id = await add_item(database)
    update = make_update(ADMIN_ID)
    bot = SimpleNamespace(
        delete_message=AsyncMock(side_effect=BadRequest("Message to delete not found"))
    )
    context = make_context(database, [str(item_id)], bot=bot)

    await admin_only(delete_command)(update, context)

    assert await ItemRepository(database).get(item_id) is None
    assert "deleted from the database" in update.effective_message.reply_text.await_args.args[0]
    assert "Could not delete 1 storage message" in update.effective_message.reply_text.await_args.args[0]


async def test_revoke_cannot_remove_a_configured_admin_grant(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    uploader_repository = UploaderRepository(database)
    await uploader_repository.add(user_id=ADMIN_ID, added_by=ADMIN_ID)
    update = make_update(ADMIN_ID)

    await admin_only(revoke_command)(update, make_context(database, [str(ADMIN_ID)]))

    assert await uploader_repository.is_active(ADMIN_ID)
    update.effective_message.reply_text.assert_awaited_once_with(
        "Administrators cannot be revoked."
    )


async def test_missing_storage_copy_returns_exact_fallback_and_removes_item(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    item_id = await add_item(database, with_file_metadata=True)
    token = await create_link(database, item_id)
    update = make_update(USER_ID)
    bot = SimpleNamespace(
        copy_message=AsyncMock(side_effect=BadRequest("Message to copy not found")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
    )
    context = make_context(database, [token], bot=bot)

    await start_command(update, context)

    update.effective_message.reply_text.assert_awaited_once_with(
        "This file is no longer available"
    )
    assert await ItemRepository(database).get(item_id) is None
    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT COUNT(*) FROM item_file_fingerprints WHERE item_id = ?", (item_id,)
        )).fetchone()
    assert int(row[0]) == 0


async def test_revoke_non_admin_is_denied_without_database_change(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    repository = UploaderRepository(database)
    await repository.add(user_id=USER_ID, added_by=ADMIN_ID)
    update = make_update(USER_ID)

    await admin_only(revoke_command)(update, make_context(database, [str(USER_ID)]))

    assert await repository.is_active(USER_ID)
    update.effective_message.reply_text.assert_awaited_once_with("Not authorized.")
