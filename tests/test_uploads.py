"""Network-free tests for M2 private-DM upload handling."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from media_bot.application import build_application
from media_bot.config import Settings
from media_bot.db.database import Database
from media_bot.db.models import PendingUploadMessage
from media_bot.db.repositories import ItemRepository, UploaderRepository
from media_bot.handlers.uploads import UploadHandler


ADMIN_ID = 12345
ALLOWLIST_ID = 23456
OTHER_ID = 34567
STORAGE_CHAT_ID = -10076543


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "upload-test.db")
    await db.initialize()
    return db


def make_settings(
    db_path: Path,
    *,
    mode: str = "admin",
    uploader_ids: tuple[int, ...] = (),
    max_bytes: int = 2_147_483_648,
    protect_default: bool = False,
    env: str = "dev",
) -> Settings:
    return Settings(
        bot_token="123:dummy",
        storage_channel_id=STORAGE_CHAT_ID,
        admin_ids=(ADMIN_ID,),
        db_path=db_path,
        log_level="INFO",
        env=env,
        max_relay_file_bytes=max_bytes,
        default_protect_content=protect_default,
        uploader_mode=mode,
        uploader_ids=uploader_ids,
    )


def make_bot(*, single_id: int = 701, album_ids: tuple[int, ...] = (801, 802, 803)):
    return SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=single_id)),
        copy_messages=AsyncMock(
            return_value=tuple(SimpleNamespace(message_id=value) for value in album_ids)
        ),
        send_message=AsyncMock(),
    )


def make_update(
    *,
    user_id: int = ADMIN_ID,
    message_id: int = 101,
    media_group_id: str | None = None,
    file_size: int | None = 23,
    file_unique_id: str | None = "synthetic-unique-1",
    file_id: str | None = "synthetic-file-1",
    media_type: str = "document",
    protected: bool = False,
    caption: str | None = "test caption",
):
    media = SimpleNamespace(
        file_id=file_id,
        file_unique_id=file_unique_id,
        file_size=file_size,
        file_name="fixture.bin",
        mime_type="application/octet-stream",
        width=100,
        height=100,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=user_id, type="private"),
        message_id=message_id,
        media_group_id=media_group_id,
        caption=caption,
        has_protected_content=protected,
        protect_content=False,
        reply_text=AsyncMock(),
        document=None,
        video=None,
        audio=None,
        voice=None,
        animation=None,
        video_note=None,
        photo=None,
    )
    if media_type == "photo":
        message.photo = [media]
    else:
        setattr(message, media_type, media)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), effective_message=message
    )
    return update, message


def make_context(bot):
    return SimpleNamespace(bot=bot)


async def audit_rows(database: Database):
    async with database.connection() as connection:
        return await (
            await connection.execute("SELECT * FROM audit_log ORDER BY id")
        ).fetchall()


async def pending_count(database: Database) -> int:
    async with database.connection() as connection:
        row = await (
            await connection.execute("SELECT COUNT(*) FROM pending_uploads")
        ).fetchone()
    return int(row[0])


@pytest.mark.parametrize(
    ("mode", "user_id", "uploader_ids", "allowed"),
    [
        ("admin", ADMIN_ID, (), True),
        ("admin", OTHER_ID, (), False),
        ("allowlist", ALLOWLIST_ID, (ALLOWLIST_ID,), True),
        ("allowlist", ADMIN_ID, (ALLOWLIST_ID,), True),
        ("all", OTHER_ID, (), True),
    ],
)
async def test_uploader_gating_modes(
    tmp_path: Path,
    mode: str,
    user_id: int,
    uploader_ids: tuple[int, ...],
    allowed: bool,
) -> None:
    database = Database(tmp_path / f"{mode}-{user_id}.db")
    await database.initialize()
    settings = make_settings(
        database.path, mode=mode, uploader_ids=uploader_ids
    )
    handler = UploadHandler(settings, database)
    bot = make_bot()
    update, message = make_update(user_id=user_id)

    await handler.handle_message(update, make_context(bot))

    if allowed:
        bot.copy_message.assert_awaited_once()
        assert message.reply_text.await_args.args[0].startswith("Stored.")
        assert await audit_rows(database) == []
    else:
        bot.copy_message.assert_not_awaited()
        message.reply_text.assert_awaited_once_with("Uploads are not open.")
        rows = await audit_rows(database)
        assert len(rows) == 1
        assert rows[0]["action"] == "upload_denied"
        assert rows[0]["actor_id"] == user_id
    assert await pending_count(database) == 0


@pytest.mark.parametrize(
    ("file_size", "protected", "expected_reason", "expected_reply"),
    [
        (None, False, "size_unknown", "Cannot store this media: size unavailable."),
        (51, False, "too_large", "Cannot store this media: file too large."),
        (20, True, "protected_source", "Cannot store protected media."),
    ],
)
async def test_early_rejections_do_not_copy_or_leave_pending_rows(
    database: Database,
    file_size: int | None,
    protected: bool,
    expected_reason: str,
    expected_reply: str,
) -> None:
    handler = UploadHandler(
        make_settings(database.path, max_bytes=50), database
    )
    bot = make_bot()
    update, message = make_update(file_size=file_size, protected=protected)

    await handler.handle_message(update, make_context(bot))

    bot.copy_message.assert_not_awaited()
    bot.copy_messages.assert_not_awaited()
    message.reply_text.assert_awaited_once_with(expected_reply)
    rows = await audit_rows(database)
    assert len(rows) == 1
    assert rows[0]["action"] == "upload_rejected"
    assert rows[0]["target_id"] == expected_reason
    assert await pending_count(database) == 0


async def test_early_rejection_discards_already_buffered_album_parts(
    database: Database,
) -> None:
    handler = UploadHandler(
        make_settings(database.path, max_bytes=50),
        database,
        debounce_seconds=0.2,
    )
    bot = make_bot()
    first_update, _ = make_update(
        message_id=151,
        media_group_id="synthetic-invalid-album",
        file_size=20,
        file_unique_id="valid-first-part",
    )
    invalid_update, invalid_message = make_update(
        message_id=152,
        media_group_id="synthetic-invalid-album",
        file_size=51,
        file_unique_id="oversized-second-part",
    )
    later_update, later_message = make_update(
        message_id=153,
        media_group_id="synthetic-invalid-album",
        file_size=20,
        file_unique_id="later-third-part",
    )

    await handler.handle_message(first_update, make_context(bot))
    await handler.handle_message(invalid_update, make_context(bot))
    await handler.handle_message(later_update, make_context(bot))

    bot.copy_message.assert_not_awaited()
    bot.copy_messages.assert_not_awaited()
    assert invalid_message.reply_text.await_args.args[0] == (
        "Cannot store this media: file too large."
    )
    later_message.reply_text.assert_awaited_once_with("This upload was rejected.")
    assert await pending_count(database) == 0
    rows = await audit_rows(database)
    assert [row["target_id"] for row in rows] == ["too_large", "album_rejected"]


async def test_single_document_success_persists_metadata_and_safe_confirmation(
    database: Database,
) -> None:
    handler = UploadHandler(
        make_settings(database.path, protect_default=True), database
    )
    bot = make_bot(single_id=98765)
    update, message = make_update(
        file_id="synthetic-private-file-id",
        file_unique_id="synthetic-private-unique-id",
        file_size=1234,
    )

    await handler.handle_message(update, make_context(bot))

    bot.copy_message.assert_awaited_once_with(
        chat_id=STORAGE_CHAT_ID,
        from_chat_id=ADMIN_ID,
        message_id=101,
        protect_content=False,
    )
    bot.copy_messages.assert_not_awaited()
    reply = message.reply_text.await_args.args[0]
    assert "Stored." in reply
    assert "Item ID: 1" in reply
    assert "Size: 1234 bytes" in reply
    assert "synthetic-private-file-id" not in reply
    assert "98765" not in reply

    item = await ItemRepository(database).get(1)
    assert item is not None
    assert item.owner_user_id == ADMIN_ID
    assert item.file_size_bytes == 1234
    assert item.sha256 is None
    assert item.protect_content is True
    assert len(item.files) == 1
    assert item.files[0].file_id == "synthetic-private-file-id"
    assert item.files[0].file_unique_id == "synthetic-private-unique-id"
    assert item.storage_messages[0].message_id == 98765
    assert await pending_count(database) == 0
    assert handler._group_locks == {}
    assert handler._group_lock_users == {}


async def test_recovery_uses_source_chat_id_for_pending_m2_upload(
    database: Database,
) -> None:
    handler = UploadHandler(make_settings(database.path), database)
    bot = make_bot()
    received_at_ms = time.time_ns() // 1_000_000
    source_chat_id = 87654321
    await handler.pending.record_message(
        owner_user_id=ADMIN_ID,
        media_group_id="synthetic-recovered-album",
        message=PendingUploadMessage(
            source_chat_id=source_chat_id,
            source_message_id=501,
            sequence=501,
            media_type="document",
            file_id="synthetic-recovery-file",
            file_unique_id="synthetic-recovery-unique",
            file_size_bytes=12,
            received_at_ms=received_at_ms,
        ),
        settle_at_ms=received_at_ms + 10_000,
    )
    application = SimpleNamespace(bot=bot)

    await handler.recover(application)

    assert tuple(handler._album_tasks) == (
        (source_chat_id, "synthetic-recovered-album"),
    )
    await handler.shutdown(application)
    assert await pending_count(database) == 1


async def test_recovery_ignores_legacy_pending_uploads(database: Database) -> None:
    handler = UploadHandler(make_settings(database.path), database)
    bot = make_bot()
    received_at_ms = time.time_ns() // 1_000_000
    receipt = await handler.pending.record_message(
        owner_user_id=ADMIN_ID,
        media_group_id="synthetic-legacy-album",
        message=PendingUploadMessage(
            source_chat_id=ADMIN_ID,
            source_message_id=601,
            sequence=601,
            media_type="document",
            file_id="synthetic-legacy-file",
            file_unique_id="synthetic-legacy-unique",
            file_size_bytes=12,
            received_at_ms=received_at_ms,
        ),
        settle_at_ms=received_at_ms + 10_000,
    )
    async with database.connection() as connection:
        await connection.execute(
            "UPDATE pending_uploads SET pipeline = 'legacy' WHERE upload_id = ?",
            (receipt.upload.upload_id,),
        )
    application = SimpleNamespace(bot=bot)

    await handler.recover(application)

    assert handler._album_tasks == {}
    bot.copy_message.assert_not_awaited()
    bot.copy_messages.assert_not_awaited()
    assert await pending_count(database) == 1


@pytest.mark.parametrize(
    "media_type",
    ["document", "photo", "video", "audio", "voice", "animation", "video_note"],
)
async def test_supported_media_types_are_accepted(
    tmp_path: Path, media_type: str
) -> None:
    database = Database(tmp_path / f"{media_type}.db")
    await database.initialize()
    handler = UploadHandler(make_settings(database.path), database)
    bot = make_bot()
    update, _message = make_update(media_type=media_type)

    await handler.handle_message(update, make_context(bot))

    bot.copy_message.assert_awaited_once()
    item = await ItemRepository(database).get(1)
    assert item is not None
    assert item.files[0].media_type == media_type
    assert await pending_count(database) == 0


async def test_album_debounce_uses_one_copy_messages_call_and_persists_one_item(
    database: Database,
) -> None:
    handler = UploadHandler(
        make_settings(database.path), database, debounce_seconds=0.02
    )
    bot = make_bot(album_ids=(901, 902, 903))
    messages = []
    for index, size in enumerate((10, 20, 30)):
        update, message = make_update(
            message_id=201 + index,
            media_group_id="synthetic-album-1",
            file_size=size,
            file_unique_id=f"synthetic-album-file-{index}",
            file_id=f"synthetic-album-id-{index}",
            caption="album caption" if index == 0 else None,
        )
        messages.append(message)
        await handler.handle_message(update, make_context(bot))

    bot.copy_messages.assert_not_awaited()
    await asyncio.sleep(0.06)

    bot.copy_message.assert_not_awaited()
    bot.copy_messages.assert_awaited_once_with(
        chat_id=STORAGE_CHAT_ID,
        from_chat_id=ADMIN_ID,
        message_ids=[201, 202, 203],
        protect_content=False,
    )
    item = await ItemRepository(database).get(1)
    assert item is not None
    assert item.media_type == "album"
    assert item.file_size_bytes == 60
    assert item.caption == "album caption"
    assert [part.sequence for part in item.files] == [201, 202, 203]
    assert [ref.message_id for ref in item.storage_messages] == [901, 902, 903]
    assert await pending_count(database) == 0
    confirmation = messages[-1].reply_text.await_args.args[0]
    assert "Stored." in confirmation
    assert "Item ID: 1" in confirmation
    assert "Messages: 3" in confirmation
    assert "Total size: 60 bytes" in confirmation
    assert all(str(value) not in confirmation for value in (901, 902, 903))


async def test_album_partial_copy_is_rejected_without_item_or_pending_rows(
    database: Database,
) -> None:
    handler = UploadHandler(
        make_settings(database.path), database, debounce_seconds=0.01
    )
    bot = make_bot(album_ids=(901, 902))
    bot.copy_messages.return_value = (SimpleNamespace(message_id=901),)
    messages = []
    for index in range(3):
        update, message = make_update(
            message_id=301 + index,
            media_group_id="synthetic-partial-album",
            file_size=10,
            file_unique_id=f"partial-file-{index}",
            file_id=f"partial-id-{index}",
        )
        messages.append(message)
        await handler.handle_message(update, make_context(bot))

    settle_tasks = tuple(handler._album_tasks.values())
    assert settle_tasks
    await asyncio.gather(*settle_tasks)

    bot.copy_messages.assert_awaited_once()
    async with database.connection() as connection:
        item_count = int(
            (await (await connection.execute("SELECT COUNT(*) FROM items")).fetchone())[0]
        )
    assert item_count == 0
    assert await pending_count(database) == 0
    assert messages[-1].reply_text.await_args.args[0] == "Storage temporarily unavailable."
    rows = await audit_rows(database)
    assert [row["action"] for row in rows] == ["storage_copy_failed"]


async def test_duplicate_file_for_same_uploader_is_rejected_before_second_copy(
    database: Database,
) -> None:
    handler = UploadHandler(make_settings(database.path), database)
    bot = make_bot()
    first_update, _ = make_update(
        message_id=401,
        file_id="synthetic-duplicate-file-1",
        file_unique_id="same-uploader-fingerprint",
    )
    second_update, second_message = make_update(
        message_id=402,
        file_id="synthetic-duplicate-file-2",
        file_unique_id="same-uploader-fingerprint",
    )

    await handler.handle_message(first_update, make_context(bot))
    await handler.handle_message(second_update, make_context(bot))

    bot.copy_message.assert_awaited_once()
    assert second_message.reply_text.await_args.args[0] == "Already stored. Item ID: 1."
    rows = await audit_rows(database)
    assert len(rows) == 1
    assert rows[0]["action"] == "upload_rejected"
    assert rows[0]["target_id"] == "already_stored:item_id=1"
    assert await pending_count(database) == 0


async def test_storage_copy_failure_has_audit_and_no_item_or_pending_row(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    handler = UploadHandler(make_settings(database.path), database)
    bot = make_bot()
    bot.copy_message.side_effect = RuntimeError("synthetic API error with file secret")
    update, message = make_update(
        file_id="synthetic-no-log-file-id",
        file_unique_id="synthetic-failed-file",
    )

    await handler.handle_message(update, make_context(bot))

    message.reply_text.assert_awaited_once_with("Storage temporarily unavailable.")
    assert "synthetic API error with file secret" not in caplog.text
    assert "synthetic-no-log-file-id" not in caplog.text
    async with database.connection() as connection:
        item_count = int(
            (await (await connection.execute("SELECT COUNT(*) FROM items")).fetchone())[0]
        )
    assert item_count == 0
    assert await pending_count(database) == 0
    rows = await audit_rows(database)
    assert len(rows) == 1
    assert rows[0]["action"] == "storage_copy_failed"


async def test_redeemed_uploader_can_upload_through_m2_pipeline(
    database: Database,
) -> None:
    result = await UploaderRepository(database).redeem(
        user_id=OTHER_ID,
        supplied_code="one-time-test-code",
        configured_code="one-time-test-code",
    )
    assert result == "success"
    handler = UploadHandler(make_settings(database.path), database, debounce_seconds=0)
    bot = make_bot()
    update, message = make_update(user_id=OTHER_ID)

    await handler.handle_message(update, make_context(bot))

    bot.copy_message.assert_awaited_once()
    assert message.reply_text.await_args.args[0].startswith("Stored.")
    item_id = await ItemRepository(database).find_item_id_by_file_unique_id(
        OTHER_ID, "synthetic-unique-1"
    )
    assert item_id is not None
    await handler.shutdown(None)


async def test_configured_uploader_id_joins_union_outside_allowlist_mode(
    database: Database,
) -> None:
    handler = UploadHandler(
        make_settings(database.path, mode="admin", uploader_ids=(OTHER_ID,)), database
    )
    bot = make_bot()
    update, message = make_update(user_id=OTHER_ID)

    await handler.handle_message(update, make_context(bot))

    bot.copy_message.assert_awaited_once()
    assert message.reply_text.await_args.args[0].startswith("Stored.")
    await handler.shutdown(None)
