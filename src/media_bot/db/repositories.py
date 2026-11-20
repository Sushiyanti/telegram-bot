"""Transactional SQLite repositories for the bot's persistent domain records."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import aiosqlite

from media_bot.db.database import Database
from media_bot.db.models import (
    MediaItem,
    PendingUpload,
    PendingUploadMessage,
    RequiredChat,
    ShareLink,
    StorageMessageRef,
)


class RepositoryError(RuntimeError):
    """Base exception for repository operations."""


class InvalidRecord(RepositoryError, ValueError):
    """Raised when input data is invalid or conflicts with persisted data."""


class RecordNotFound(RepositoryError):
    """Raised when a requested persistent record does not exist."""


class InvalidTransition(RepositoryError):
    """Raised when a pending-upload state transition is not permitted."""


def _require_positive_int(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidRecord(f"{field} must be a positive integer")


def _require_time(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRecord(f"{field} must be a non-negative integer millisecond timestamp")


def _validate_file_size(value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise InvalidRecord("file_size_bytes must be a non-negative integer or None")


def _validate_text(value: str, field: str, *, max_length: int, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or len(value) > max_length or (not allow_empty and not value.strip()):
        raise InvalidRecord(f"{field} must be non-empty text no longer than {max_length} characters")


class ItemRepository:
    """Persist item metadata together with one or more storage message references."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_stored_item(self, item: MediaItem) -> MediaItem:
        _require_positive_int(item.owner_user_id, "owner_user_id")
        _validate_text(item.media_type, "media_type", max_length=32)
        _validate_file_size(item.file_size_bytes)
        if not isinstance(item.protect_content, bool):
            raise InvalidRecord("protect_content must be a boolean")
        if not item.storage_messages:
            raise InvalidRecord("A shareable item must have at least one storage message")
        sequences: set[int] = set()
        references: set[tuple[int, int]] = set()
        for reference in item.storage_messages:
            if isinstance(reference.chat_id, bool) or not isinstance(reference.chat_id, int) or reference.chat_id == 0:
                raise InvalidRecord("storage chat IDs must be non-zero integers")
            _require_positive_int(reference.message_id, "storage message ID")
            if isinstance(reference.sequence, bool) or not isinstance(reference.sequence, int) or reference.sequence < 0:
                raise InvalidRecord("storage message sequence must be a non-negative integer")
            if reference.sequence in sequences or (reference.chat_id, reference.message_id) in references:
                raise InvalidRecord("storage message references must be unique")
            sequences.add(reference.sequence)
            references.add((reference.chat_id, reference.message_id))
        now = item.created_at_ms if item.created_at_ms is not None else _now_ms()
        _require_time(now, "created_at_ms")

        try:
            async with self.database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                cursor = await connection.execute(
                    """INSERT INTO items
                       (owner_user_id, media_type, file_size_bytes, file_name, mime_type,
                        caption, protect_content, created_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.owner_user_id,
                        item.media_type,
                        item.file_size_bytes,
                        item.file_name,
                        item.mime_type,
                        item.caption,
                        int(item.protect_content),
                        now,
                    ),
                )
                item_id = int(cursor.lastrowid)
                for reference in item.storage_messages:
                    await connection.execute(
                        """INSERT INTO item_storage_messages
                           (item_id, storage_chat_id, storage_message_id, sequence)
                           VALUES (?, ?, ?, ?)""",
                        (item_id, reference.chat_id, reference.message_id, reference.sequence),
                    )
                await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Item or storage-message data violates database constraints") from exc
        return replace(item, id=item_id, created_at_ms=now)

    async def get(self, item_id: int) -> MediaItem | None:
        _require_positive_int(item_id, "item_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM items WHERE id = ?", (item_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            cursor = await connection.execute(
                """SELECT storage_chat_id, storage_message_id, sequence
                   FROM item_storage_messages WHERE item_id = ? ORDER BY sequence""",
                (item_id,),
            )
            storage_messages = tuple(
                StorageMessageRef(int(ref["storage_chat_id"]), int(ref["storage_message_id"]), int(ref["sequence"]))
                for ref in await cursor.fetchall()
            )
        return MediaItem(
            owner_user_id=int(row["owner_user_id"]),
            media_type=str(row["media_type"]),
            file_size_bytes=row["file_size_bytes"],
            storage_messages=storage_messages,
            file_name=row["file_name"],
            mime_type=row["mime_type"],
            caption=row["caption"],
            protect_content=bool(row["protect_content"]),
            id=int(row["id"]),
            created_at_ms=int(row["created_at_ms"]),
        )


class ShareLinkRepository:
    """Persist token digests and enforce revocation/expiry during lookup."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        *,
        item_id: int,
        token_hash: bytes,
        created_by_user_id: int,
        created_at_ms: int,
        expires_at_ms: int | None,
    ) -> ShareLink:
        _require_positive_int(item_id, "item_id")
        _require_positive_int(created_by_user_id, "created_by_user_id")
        _require_time(created_at_ms, "created_at_ms")
        if not isinstance(token_hash, bytes) or len(token_hash) != 32:
            raise InvalidRecord("token_hash must be a 32-byte SHA-256 digest")
        if expires_at_ms is not None:
            _require_time(expires_at_ms, "expires_at_ms")
            if expires_at_ms <= created_at_ms:
                raise InvalidRecord("expires_at_ms must be later than created_at_ms")
        try:
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    """INSERT INTO share_links
                       (item_id, token_hash, created_by_user_id, created_at_ms, expires_at_ms)
                       VALUES (?, ?, ?, ?, ?)""",
                    (item_id, token_hash, created_by_user_id, created_at_ms, expires_at_ms),
                )
                link_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Share link violates uniqueness or foreign-key constraints") from exc
        return ShareLink(link_id, item_id, token_hash, created_by_user_id, created_at_ms, expires_at_ms, None)

    async def find_active_by_hash(self, token_hash: bytes, *, now_ms: int) -> ShareLink | None:
        if not isinstance(token_hash, bytes) or len(token_hash) != 32:
            raise InvalidRecord("token_hash must be a 32-byte SHA-256 digest")
        _require_time(now_ms, "now_ms")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT * FROM share_links
                   WHERE token_hash = ? AND revoked_at_ms IS NULL
                     AND (expires_at_ms IS NULL OR expires_at_ms > ?)""",
                (token_hash, now_ms),
            )
            row = await cursor.fetchone()
        return None if row is None else _share_link_from_row(row)

    async def revoke_by_hash(self, token_hash: bytes, *, revoked_at_ms: int) -> bool:
        if not isinstance(token_hash, bytes) or len(token_hash) != 32:
            raise InvalidRecord("token_hash must be a 32-byte SHA-256 digest")
        _require_time(revoked_at_ms, "revoked_at_ms")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """UPDATE share_links SET revoked_at_ms = ?
                   WHERE token_hash = ? AND revoked_at_ms IS NULL AND created_at_ms <= ?""",
                (revoked_at_ms, token_hash, revoked_at_ms),
            )
            return cursor.rowcount == 1

    async def list_for_item(self, item_id: int) -> tuple[ShareLink, ...]:
        _require_positive_int(item_id, "item_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM share_links WHERE item_id = ? ORDER BY id", (item_id,)
            )
            rows = await cursor.fetchall()
        return tuple(_share_link_from_row(row) for row in rows)


class RequiredChatRepository:
    """Store required channel/group membership targets."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert(self, chat: RequiredChat) -> RequiredChat:
        if isinstance(chat.chat_id, bool) or not isinstance(chat.chat_id, int) or chat.chat_id == 0:
            raise InvalidRecord("chat_id must be a non-zero integer")
        if chat.chat_type not in {"channel", "group", "supergroup"}:
            raise InvalidRecord("chat_type must be channel, group, or supergroup")
        _validate_text(chat.title, "title", max_length=512)
        if chat.username is not None:
            _validate_text(chat.username, "username", max_length=64)
        if not isinstance(chat.is_enabled, bool):
            raise InvalidRecord("is_enabled must be a boolean")
        created = chat.created_at_ms if chat.created_at_ms is not None else _now_ms()
        updated = chat.updated_at_ms if chat.updated_at_ms is not None else _now_ms()
        _require_time(created, "created_at_ms")
        _require_time(updated, "updated_at_ms")
        if updated < created:
            raise InvalidRecord("updated_at_ms must not precede created_at_ms")
        async with self.database.connection() as connection:
            await connection.execute(
                """INSERT INTO required_chats
                   (chat_id, chat_type, title, username, is_enabled, created_at_ms, updated_at_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     chat_type = excluded.chat_type,
                     title = excluded.title,
                     username = excluded.username,
                     is_enabled = excluded.is_enabled,
                     updated_at_ms = MAX(required_chats.updated_at_ms, excluded.updated_at_ms)""",
                (chat.chat_id, chat.chat_type, chat.title, chat.username, int(chat.is_enabled), created, updated),
            )
            cursor = await connection.execute(
                "SELECT created_at_ms, updated_at_ms FROM required_chats WHERE chat_id = ?", (chat.chat_id,)
            )
            row = await cursor.fetchone()
            persisted_created = int(row["created_at_ms"])
            persisted_updated = int(row["updated_at_ms"])
        return replace(chat, created_at_ms=persisted_created, updated_at_ms=persisted_updated)

    async def list_enabled(self) -> tuple[RequiredChat, ...]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM required_chats WHERE is_enabled = 1 ORDER BY chat_id"
            )
            rows = await cursor.fetchall()
        return tuple(
            RequiredChat(
                chat_id=int(row["chat_id"]),
                chat_type=str(row["chat_type"]),
                title=str(row["title"]),
                username=row["username"],
                is_enabled=bool(row["is_enabled"]),
                created_at_ms=int(row["created_at_ms"]),
                updated_at_ms=int(row["updated_at_ms"]),
            )
            for row in rows
        )


class PendingUploadRepository:
    """Durably collect upload message references and recover nonterminal work."""

    _TRANSITIONS = {
        "collecting": {"ready", "failed", "cancelled"},
        "ready": {"processing", "failed", "cancelled"},
        "processing": {"ready", "completed", "failed", "cancelled"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    async def record_message(
        self,
        *,
        upload_id: str,
        owner_user_id: int,
        media_group_id: str,
        message: PendingUploadMessage,
        settle_at_ms: int,
    ) -> bool:
        _validate_text(upload_id, "upload_id", max_length=128)
        _require_positive_int(owner_user_id, "owner_user_id")
        _validate_text(media_group_id, "media_group_id", max_length=128)
        _require_time(settle_at_ms, "settle_at_ms")
        _require_positive_int(message.source_chat_id, "source_chat_id")
        _require_positive_int(message.source_message_id, "source_message_id")
        _require_time(message.received_at_ms, "received_at_ms")
        if settle_at_ms < message.received_at_ms:
            raise InvalidRecord("settle_at_ms cannot precede received_at_ms")
        _validate_text(message.media_type, "media_type", max_length=32)
        _validate_text(message.file_id, "file_id", max_length=2048)
        _validate_text(message.file_unique_id, "file_unique_id", max_length=256)
        _validate_file_size(message.file_size_bytes)
        if isinstance(message.sequence, bool) or not isinstance(message.sequence, int) or message.sequence < 0:
            raise InvalidRecord("sequence must be a non-negative integer")

        try:
            async with self.database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                cursor = await connection.execute(
                    "SELECT * FROM pending_uploads WHERE owner_user_id = ? AND media_group_id = ?",
                    (owner_user_id, media_group_id),
                )
                upload = await cursor.fetchone()
                if upload is None:
                    await connection.execute(
                        """INSERT INTO pending_uploads
                           (upload_id, owner_user_id, media_group_id, status, created_at_ms,
                            last_seen_at_ms, settle_at_ms)
                           VALUES (?, ?, ?, 'collecting', ?, ?, ?)""",
                        (upload_id, owner_user_id, media_group_id, message.received_at_ms, message.received_at_ms, settle_at_ms),
                    )
                else:
                    upload_id = str(upload["upload_id"])
                    if upload["status"] != "collecting":
                        raise InvalidTransition("Only collecting uploads can accept new messages")
                    await connection.execute(
                        """UPDATE pending_uploads
                           SET last_seen_at_ms = MAX(last_seen_at_ms, ?),
                               settle_at_ms = MAX(settle_at_ms, ?)
                           WHERE upload_id = ?""",
                        (message.received_at_ms, settle_at_ms, upload_id),
                    )

                cursor = await connection.execute(
                    "SELECT * FROM pending_upload_messages WHERE source_chat_id = ? AND source_message_id = ?",
                    (message.source_chat_id, message.source_message_id),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    same_upload = str(existing["upload_id"]) == upload_id
                    same_message = (
                        int(existing["sequence"]) == message.sequence
                        and str(existing["file_id"]) == message.file_id
                        and str(existing["file_unique_id"]) == message.file_unique_id
                    )
                    if not same_upload or not same_message:
                        raise InvalidRecord("Telegram source message conflicts with an existing pending upload")
                    await connection.commit()
                    return False

                await connection.execute(
                    """INSERT INTO pending_upload_messages
                       (upload_id, source_chat_id, source_message_id, sequence, media_type,
                        file_id, file_unique_id, file_size_bytes, file_name, mime_type, caption, received_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        upload_id,
                        message.source_chat_id,
                        message.source_message_id,
                        message.sequence,
                        message.media_type,
                        message.file_id,
                        message.file_unique_id,
                        message.file_size_bytes,
                        message.file_name,
                        message.mime_type,
                        message.caption,
                        message.received_at_ms,
                    ),
                )
                await connection.commit()
                return True
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Pending-upload data violates uniqueness or database constraints") from exc

    async def get(self, upload_id: str) -> PendingUpload | None:
        _validate_text(upload_id, "upload_id", max_length=128)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM pending_uploads WHERE upload_id = ?", (upload_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            messages = await _pending_messages(connection, upload_id)
        return _pending_upload_from_row(row, messages)

    async def get_by_owner_and_group(self, owner_user_id: int, media_group_id: str) -> PendingUpload | None:
        _require_positive_int(owner_user_id, "owner_user_id")
        _validate_text(media_group_id, "media_group_id", max_length=128)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT upload_id FROM pending_uploads WHERE owner_user_id = ? AND media_group_id = ?",
                (owner_user_id, media_group_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            upload_id = str(row["upload_id"])
            cursor = await connection.execute("SELECT * FROM pending_uploads WHERE upload_id = ?", (upload_id,))
            upload = await cursor.fetchone()
            messages = await _pending_messages(connection, upload_id)
        return _pending_upload_from_row(upload, messages)

    async def list_recoverable(self) -> tuple[PendingUpload, ...]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT * FROM pending_uploads
                   WHERE status IN ('collecting', 'ready', 'processing')
                   ORDER BY settle_at_ms, created_at_ms"""
            )
            rows = await cursor.fetchall()
            uploads = []
            for row in rows:
                messages = await _pending_messages(connection, str(row["upload_id"]))
                uploads.append(_pending_upload_from_row(row, messages))
        return tuple(uploads)

    async def transition(
        self,
        upload_id: str,
        new_status: str,
        *,
        at_ms: int,
        error_code: str | None = None,
    ) -> PendingUpload:
        _validate_text(upload_id, "upload_id", max_length=128)
        _require_time(at_ms, "at_ms")
        if new_status not in self._TRANSITIONS:
            raise InvalidTransition("Unknown pending-upload status")
        if error_code is not None:
            _validate_text(error_code, "error_code", max_length=128)
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("SELECT * FROM pending_uploads WHERE upload_id = ?", (upload_id,))
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                raise RecordNotFound("Pending upload does not exist")
            old_status = str(row["status"])
            if new_status not in self._TRANSITIONS[old_status]:
                await connection.rollback()
                raise InvalidTransition(f"Cannot change pending upload from {old_status} to {new_status}")
            if at_ms < int(row["created_at_ms"]):
                await connection.rollback()
                raise InvalidRecord("transition timestamp cannot precede upload creation")
            started = row["started_at_ms"]
            finished = row["finished_at_ms"]
            if new_status == "processing":
                started = at_ms
                finished = None
            elif new_status in {"completed", "failed", "cancelled"}:
                finished = at_ms
            await connection.execute(
                """UPDATE pending_uploads SET status = ?, started_at_ms = ?, finished_at_ms = ?, error_code = ?
                   WHERE upload_id = ?""",
                (new_status, started, finished, error_code if new_status == "failed" else None, upload_id),
            )
            await connection.commit()
        result = await self.get(upload_id)
        if result is None:
            raise RecordNotFound("Pending upload disappeared during transition")
        return result


def _now_ms() -> int:
    import time

    return time.time_ns() // 1_000_000


def _share_link_from_row(row: sqlite3.Row) -> ShareLink:
    return ShareLink(
        id=int(row["id"]),
        item_id=int(row["item_id"]),
        token_hash=bytes(row["token_hash"]),
        created_by_user_id=int(row["created_by_user_id"]),
        created_at_ms=int(row["created_at_ms"]),
        expires_at_ms=None if row["expires_at_ms"] is None else int(row["expires_at_ms"]),
        revoked_at_ms=None if row["revoked_at_ms"] is None else int(row["revoked_at_ms"]),
    )


async def _pending_messages(connection: aiosqlite.Connection, upload_id: str) -> tuple[PendingUploadMessage, ...]:
    cursor = await connection.execute(
        """SELECT * FROM pending_upload_messages WHERE upload_id = ? ORDER BY sequence""",
        (upload_id,),
    )
    rows = await cursor.fetchall()
    return tuple(
        PendingUploadMessage(
            source_chat_id=int(row["source_chat_id"]),
            source_message_id=int(row["source_message_id"]),
            sequence=int(row["sequence"]),
            media_type=str(row["media_type"]),
            file_id=str(row["file_id"]),
            file_unique_id=str(row["file_unique_id"]),
            file_size_bytes=row["file_size_bytes"],
            received_at_ms=int(row["received_at_ms"]),
            file_name=row["file_name"],
            mime_type=row["mime_type"],
            caption=row["caption"],
        )
        for row in rows
    )


def _pending_upload_from_row(row: sqlite3.Row, messages: tuple[PendingUploadMessage, ...]) -> PendingUpload:
    return PendingUpload(
        upload_id=str(row["upload_id"]),
        owner_user_id=int(row["owner_user_id"]),
        media_group_id=str(row["media_group_id"]),
        status=str(row["status"]),
        created_at_ms=int(row["created_at_ms"]),
        last_seen_at_ms=int(row["last_seen_at_ms"]),
        settle_at_ms=int(row["settle_at_ms"]),
        messages=messages,
        started_at_ms=None if row["started_at_ms"] is None else int(row["started_at_ms"]),
        finished_at_ms=None if row["finished_at_ms"] is None else int(row["finished_at_ms"]),
        error_code=row["error_code"],
    )
