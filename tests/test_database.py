"""Network-free tests for SQLite schema, repositories, and backup recovery."""

from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

from media_bot.db.database import Database, DatabaseError
from media_bot.db.models import (
    MediaItem,
    PendingUploadMessage,
    RequiredChat,
    StorageMessageRef,
)
from media_bot.db.repositories import (
    InvalidRecord,
    InvalidTransition,
    ItemRepository,
    PendingUploadRepository,
    RequiredChatRepository,
    ShareLinkRepository,
)
from media_bot.media_transfer import TransferCapabilities
from media_bot.services import (
    PendingUploadService,
    RequiredChatService,
    ShareLinkService,
    UnsupportedFileSize,
    hash_share_token,
)


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "bot.sqlite3")
    await db.initialize()
    return db


def make_item(size: int | None = 123) -> MediaItem:
    return MediaItem(
        owner_user_id=123456789,
        media_type="document",
        file_size_bytes=size,
        storage_messages=(StorageMessageRef(-1001234567890, 101),),
        file_name="sample.bin",
        mime_type="application/octet-stream",
    )


async def test_fresh_initialization_sets_wal_foreign_keys_and_busy_timeout(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "bot.sqlite3", busy_timeout_ms=2300)
    await db.initialize()
    async with db.connection() as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        journal = await (await connection.execute("PRAGMA journal_mode")).fetchone()
        foreign_keys = await (await connection.execute("PRAGMA foreign_keys")).fetchone()
        timeout = await (await connection.execute("PRAGMA busy_timeout")).fetchone()
        tables = await (await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
    assert version[0] == 1
    assert journal[0].lower() == "wal"
    assert foreign_keys[0] == 1
    assert timeout[0] == 2300
    assert {row[0] for row in tables} >= {
        "items",
        "item_storage_messages",
        "share_links",
        "required_chats",
        "pending_uploads",
        "pending_upload_messages",
    }
    assert db.path.stat().st_mode & 0o777 == 0o600


async def test_migration_repeatability_is_idempotent(database: Database) -> None:
    await database.initialize()
    await database.initialize()
    async with database.connection() as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        indexes = await (await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )).fetchall()
    assert version[0] == 1
    assert len([row[0] for row in indexes if row[0] == "share_links_by_item_id"]) == 1


async def test_foreign_key_enforcement_rejects_missing_item(database: Database) -> None:
    async with database.connection() as connection:
        enabled = await (await connection.execute("PRAGMA foreign_keys")).fetchone()
        assert enabled[0] == 1
        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                """INSERT INTO item_storage_messages
                   (item_id, storage_chat_id, storage_message_id, sequence)
                   VALUES (999, -100123, 50, 0)"""
            )


async def test_item_and_storage_message_persist_after_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persistent.sqlite3"
    first = Database(path)
    await first.initialize()
    item = await ItemRepository(first).create_stored_item(make_item(4_000_000_000))
    assert item.id is not None

    reopened = Database(path)
    await reopened.initialize()
    loaded = await ItemRepository(reopened).get(item.id)
    assert loaded is not None
    assert loaded.file_size_bytes == 4_000_000_000
    assert loaded.storage_messages == (StorageMessageRef(-1001234567890, 101),)


async def test_invalid_or_inconsistent_records_are_rejected(database: Database) -> None:
    repository = ItemRepository(database)
    with pytest.raises(InvalidRecord):
        await repository.create_stored_item(make_item(-1))
    with pytest.raises(InvalidRecord):
        await repository.create_stored_item(
            MediaItem(123456789, "document", 1, ())
        )
    item = await repository.create_stored_item(make_item())
    assert item.id is not None
    async with database.connection() as connection:
        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                "UPDATE items SET file_size_bytes = -5 WHERE id = ?", (item.id,)
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                """INSERT INTO share_links
                   (item_id, token_hash, created_by_user_id, created_at_ms)
                   VALUES (?, ?, ?, ?)""",
                (item.id, b"short", 123456789, 1),
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                """INSERT INTO share_links
                   (item_id, token_hash, created_by_user_id, created_at_ms)
                   VALUES (?, ?, ?, ?)""",
                (999, b"x" * 32, 123456789, 1),
            )


async def test_required_chats_persist_and_upsert(database: Database) -> None:
    service = RequiredChatService(RequiredChatRepository(database))
    original = RequiredChat(-100333, "channel", "Membership Channel", "members", True)
    saved = await service.set_required_chat(original)
    assert saved.chat_id == -100333
    assert [chat.title for chat in await service.list_enabled()] == ["Membership Channel"]

    updated = await service.set_required_chat(
        RequiredChat(-100333, "channel", "Renamed Channel", "members", False)
    )
    assert updated.created_at_ms == saved.created_at_ms
    assert await service.list_enabled() == ()


async def test_pending_upload_records_are_recoverable_and_transition_safe(database: Database) -> None:
    service = PendingUploadService(PendingUploadRepository(database))
    first = PendingUploadMessage(
        source_chat_id=123456789,
        source_message_id=501,
        sequence=0,
        media_type="photo",
        file_id="file-id-a",
        file_unique_id="unique-a",
        file_size_bytes=345678,
        received_at_ms=1_800_000_000_000,
        caption="album",
    )
    result = await service.record_message(
        owner_user_id=123456789,
        media_group_id="album-remote-id",
        message=first,
        settle_at_ms=1_800_000_002_000,
    )
    assert result.inserted is True
    second = PendingUploadMessage(
        source_chat_id=123456789,
        source_message_id=502,
        sequence=1,
        media_type="photo",
        file_id="file-id-b",
        file_unique_id="unique-b",
        file_size_bytes=456789,
        received_at_ms=1_800_000_000_500,
    )
    await service.record_message(
        owner_user_id=123456789,
        media_group_id="album-remote-id",
        message=second,
        settle_at_ms=1_800_000_002_500,
    )
    duplicate = await service.record_message(
        owner_user_id=123456789,
        media_group_id="album-remote-id",
        message=second,
        settle_at_ms=1_800_000_002_500,
    )
    assert duplicate.inserted is False

    await database.initialize()
    recoverable = await service.list_recoverable()
    assert len(recoverable) == 1
    upload = recoverable[0]
    assert upload.status == "collecting"
    assert [message.source_message_id for message in upload.messages] == [501, 502]
    assert upload.messages[0].file_size_bytes == 345678

    ready = await service.transition(upload.upload_id, "ready", at_ms=1_800_000_003_000)
    processing = await service.transition(upload.upload_id, "processing", at_ms=1_800_000_004_000)
    completed = await service.transition(upload.upload_id, "completed", at_ms=1_800_000_005_000)
    assert ready.status == "ready"
    assert processing.started_at_ms == 1_800_000_004_000
    assert completed.finished_at_ms == 1_800_000_005_000
    assert await service.list_recoverable() == ()
    with pytest.raises(InvalidTransition):
        await service.transition(upload.upload_id, "ready", at_ms=1_800_000_006_000)


async def test_pending_upload_rejects_source_message_conflicts(database: Database) -> None:
    repository = PendingUploadRepository(database)
    message = PendingUploadMessage(
        source_chat_id=123456789,
        source_message_id=700,
        sequence=0,
        media_type="document",
        file_id="file-id",
        file_unique_id="unique-id",
        file_size_bytes=None,
        received_at_ms=1_800_000_000_000,
    )
    await repository.record_message(
        upload_id="upload-a",
        owner_user_id=123456789,
        media_group_id="group-a",
        message=message,
        settle_at_ms=1_800_000_001_000,
    )
    conflicting = replace(message, file_id="different-file-id")
    with pytest.raises(InvalidRecord):
        await repository.record_message(
            upload_id="upload-b",
            owner_user_id=123456789,
            media_group_id="group-b",
            message=conflicting,
            settle_at_ms=1_800_000_001_000,
        )


async def test_share_link_creation_hashing_lookup_and_revocation(database: Database) -> None:
    items = ItemRepository(database)
    item = await items.create_stored_item(make_item(4_000_000_000))
    links = ShareLinkRepository(database)
    service = ShareLinkService(
        links,
        items,
        TransferCapabilities(verified_max_file_size_bytes=5_000_000_000),
    )
    created = await service.create(item_id=item.id, created_by_user_id=123456789)
    assert len(created.token) == 43
    assert created.link.expires_at_ms is None
    assert await service.find_active(created.token) == created.link
    assert await service.find_active("malformed") is None
    assert await service.revoke(created.token) is True
    assert await service.find_active(created.token) is None
    assert await service.revoke(created.token) is False

    async with database.connection() as connection:
        rows = await (await connection.execute("SELECT token_hash FROM share_links")).fetchall()
    assert rows[0]["token_hash"] == hash_share_token(created.token)
    assert created.token.encode() not in rows[0]["token_hash"]
    assert hash_share_token(created.token) != created.token.encode()


async def test_multiple_links_may_reference_same_item(database: Database) -> None:
    items = ItemRepository(database)
    item = await items.create_stored_item(make_item())
    service = ShareLinkService(
        ShareLinkRepository(database),
        items,
        TransferCapabilities(verified_max_file_size_bytes=1_000),
    )
    first = await service.create(item_id=item.id, created_by_user_id=123456789)
    second = await service.create(item_id=item.id, created_by_user_id=123456789)
    assert first.token != second.token
    assert len(await service.list_for_item(item.id)) == 2
    assert await service.find_active(first.token) == first.link
    assert await service.find_active(second.token) == second.link


async def test_expired_link_is_not_active(database: Database) -> None:
    items = ItemRepository(database)
    item = await items.create_stored_item(make_item())
    links = ShareLinkRepository(database)
    now = 1_800_000_000_000
    link = await links.create(
        item_id=item.id,
        token_hash=hash_share_token("fixed-test-token"),
        created_by_user_id=123456789,
        created_at_ms=now,
        expires_at_ms=now + 100,
    )
    assert await links.find_active_by_hash(link.token_hash, now_ms=now + 99) == link
    assert await links.find_active_by_hash(link.token_hash, now_ms=now + 100) is None


async def test_unknown_or_excessive_transfer_size_never_creates_share_link(database: Database) -> None:
    items = ItemRepository(database)
    oversized = await items.create_stored_item(make_item(4_000_000_001))
    links = ShareLinkRepository(database)
    unknown_backend = ShareLinkService(links, items, TransferCapabilities(None))
    with pytest.raises(UnsupportedFileSize):
        await unknown_backend.create(item_id=oversized.id, created_by_user_id=123456789)
    bounded_backend = ShareLinkService(links, items, TransferCapabilities(4_000_000_000))
    with pytest.raises(UnsupportedFileSize):
        await bounded_backend.create(item_id=oversized.id, created_by_user_id=123456789)
    assert await links.list_for_item(oversized.id) == ()


async def test_backup_and_restore_online_snapshot(tmp_path: Path) -> None:
    source_path = tmp_path / "live" / "bot.sqlite3"
    database = Database(source_path)
    await database.initialize()
    repository = ItemRepository(database)
    first = await repository.create_stored_item(make_item(99))
    backup_path = await database.backup_to(tmp_path / "backups" / "snapshot.sqlite3")
    assert backup_path.is_file()
    assert backup_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        await database.backup_to(backup_path)

    second = await repository.create_stored_item(
        MediaItem(123456789, "video", 100, (StorageMessageRef(-1001234567890, 102),))
    )
    assert second.id != first.id
    await Database.restore_from(backup_path, source_path)
    restored = ItemRepository(Database(source_path))
    assert await restored.get(first.id) is not None
    assert await restored.get(second.id) is None


async def test_restore_rejects_invalid_backup_without_replacing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "bot.sqlite3"
    database = Database(destination)
    await database.initialize()
    item = await ItemRepository(database).create_stored_item(make_item())
    invalid_backup = tmp_path / "invalid.sqlite3"
    invalid_backup.write_bytes(b"not a sqlite database")
    with pytest.raises(DatabaseError):
        await Database.restore_from(invalid_backup, destination)
    assert await ItemRepository(database).get(item.id) is not None
