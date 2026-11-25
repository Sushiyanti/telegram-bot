"""Transactional SQLite repositories for the bot's persistent domain records."""

from __future__ import annotations

import sqlite3
import hmac
import re
from datetime import datetime, timezone
from dataclasses import replace

import aiosqlite

from media_bot.db.database import Database
from media_bot.db.models import (
    ItemFile,
    ItemSummary,
    MediaItem,
    Offer,
    OfferDelivery,
    OfferEvent,
    OfferRequiredChat,
    OfferRequirement,
    OfferStats,
    PendingUpload,
    PendingUploadMessage,
    RequiredChat,
    ShareLink,
    StorageMessageRef,
    UploaderGrant,
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


def _require_nonzero_int(value: int, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value == 0
        or not -(1 << 63) <= value <= (1 << 63) - 1
    ):
        raise InvalidRecord(f"{field} must be a non-zero signed 64-bit integer")


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

    async def create_stored_item(
        self, item: MediaItem, *, clear_pending_upload_id: str | None = None
    ) -> MediaItem:
        _require_positive_int(item.owner_user_id, "owner_user_id")
        _validate_text(item.media_type, "media_type", max_length=32)
        _validate_file_size(item.file_size_bytes)
        if not isinstance(item.protect_content, bool):
            raise InvalidRecord("protect_content must be a boolean")
        if item.sha256 is not None and (
            not isinstance(item.sha256, bytes) or len(item.sha256) != 32
        ):
            raise InvalidRecord("sha256 must be a 32-byte digest or None")
        if not item.storage_messages:
            raise InvalidRecord("A shareable item must have at least one storage message")
        message_chat_ids = {reference.chat_id for reference in item.storage_messages}
        if len(message_chat_ids) != 1:
            raise InvalidRecord("All storage messages for an item must use the same channel")
        storage_channel_id = item.storage_channel_id
        if storage_channel_id is None:
            storage_channel_id = next(iter(message_chat_ids))
        elif storage_channel_id not in message_chat_ids:
            raise InvalidRecord("storage_channel_id must match the item's storage messages")
        _require_nonzero_int(storage_channel_id, "storage_channel_id")
        if item.category_id is not None:
            _require_positive_int(item.category_id, "category_id")
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
        file_sequences: set[int] = set()
        for stored_file in item.files:
            if (
                isinstance(stored_file.sequence, bool)
                or not isinstance(stored_file.sequence, int)
                or stored_file.sequence < 0
            ):
                raise InvalidRecord("item-file sequence must be a non-negative integer")
            if stored_file.sequence in file_sequences:
                raise InvalidRecord("item-file sequences must be unique")
            file_sequences.add(stored_file.sequence)
            _validate_text(stored_file.media_type, "media_type", max_length=32)
            _validate_text(stored_file.file_id, "file_id", max_length=2048)
            _validate_text(stored_file.file_unique_id, "file_unique_id", max_length=256)
            _validate_file_size(stored_file.file_size_bytes)
            if stored_file.file_size_bytes is None:
                raise InvalidRecord("stored item file size must be known")
            if stored_file.file_name is not None:
                _validate_text(stored_file.file_name, "file_name", max_length=1024)
            if stored_file.mime_type is not None:
                _validate_text(stored_file.mime_type, "mime_type", max_length=256)
        if item.files and (
            len(item.files) != len(item.storage_messages)
            or file_sequences != sequences
        ):
            raise InvalidRecord("item files and storage references must have matching sequences")
        now = item.created_at_ms if item.created_at_ms is not None else _now_ms()
        _require_time(now, "created_at_ms")

        try:
            async with self.database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                if item.category_id is None:
                    cursor = await connection.execute(
                        "SELECT id, storage_channel_id FROM categories WHERE name = 'Default' COLLATE NOCASE"
                    )
                    default_row = await cursor.fetchone()
                    if default_row is None:
                        raise InvalidRecord("Default category has not been initialized")
                    category_id = int(default_row["id"])
                    category_storage_id = default_row["storage_channel_id"]
                else:
                    cursor = await connection.execute(
                        "SELECT storage_channel_id FROM categories WHERE id = ?",
                        (item.category_id,),
                    )
                    category_row = await cursor.fetchone()
                    if category_row is None:
                        raise InvalidRecord("category_id does not exist")
                    category_id = item.category_id
                    category_storage_id = category_row["storage_channel_id"]
                if (
                    category_storage_id is not None
                    and int(category_storage_id) != storage_channel_id
                ):
                    raise InvalidRecord(
                        "storage_channel_id must match the selected category's storage channel"
                    )
                cursor = await connection.execute(
                    """INSERT INTO items
                       (owner_user_id, media_type, file_size_bytes, file_name, mime_type,
                        caption, protect_content, created_at_ms, sha256, category_id,
                        storage_channel_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.owner_user_id,
                        item.media_type,
                        item.file_size_bytes,
                        item.file_name,
                        item.mime_type,
                        item.caption,
                        int(item.protect_content),
                        now,
                        item.sha256,
                        category_id,
                        storage_channel_id,
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
                inserted_fingerprints: set[str] = set()
                for stored_file in item.files:
                    await connection.execute(
                        """INSERT INTO item_files
                           (item_id, owner_user_id, sequence, media_type, file_id,
                            file_unique_id, file_size_bytes, file_name, mime_type)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            item_id,
                            item.owner_user_id,
                            stored_file.sequence,
                            stored_file.media_type,
                            stored_file.file_id,
                            stored_file.file_unique_id,
                            stored_file.file_size_bytes,
                            stored_file.file_name,
                            stored_file.mime_type,
                        ),
                    )
                    if stored_file.file_unique_id not in inserted_fingerprints:
                        await connection.execute(
                            """INSERT INTO item_file_fingerprints
                               (owner_user_id, file_unique_id, item_id)
                               VALUES (?, ?, ?)""",
                            (item.owner_user_id, stored_file.file_unique_id, item_id),
                        )
                        inserted_fingerprints.add(stored_file.file_unique_id)
                if clear_pending_upload_id is not None:
                    _validate_text(
                        clear_pending_upload_id,
                        "pending_upload_id",
                        max_length=128,
                    )
                    cursor = await connection.execute(
                        "DELETE FROM pending_uploads WHERE upload_id = ?",
                        (clear_pending_upload_id,),
                    )
                    if cursor.rowcount != 1:
                        raise RecordNotFound(
                            "Pending upload disappeared before item commit"
                        )
                await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Item or storage-file data violates database constraints") from exc
        return replace(
            item,
            id=item_id,
            created_at_ms=now,
            category_id=category_id,
            storage_channel_id=storage_channel_id,
        )

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
            cursor = await connection.execute(
                "SELECT * FROM item_files WHERE item_id = ? ORDER BY sequence", (item_id,)
            )
            files = tuple(
                ItemFile(
                    sequence=int(file_row["sequence"]),
                    media_type=str(file_row["media_type"]),
                    file_id=str(file_row["file_id"]),
                    file_unique_id=str(file_row["file_unique_id"]),
                    file_size_bytes=int(file_row["file_size_bytes"]),
                    file_name=file_row["file_name"],
                    mime_type=file_row["mime_type"],
                )
                for file_row in await cursor.fetchall()
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
            sha256=None if row["sha256"] is None else bytes(row["sha256"]),
            files=files,
            category_id=None if row["category_id"] is None else int(row["category_id"]),
            storage_channel_id=(
                None if row["storage_channel_id"] is None else int(row["storage_channel_id"])
            ),
        )

    async def list_recent(self, limit: int = 10) -> tuple[ItemSummary, ...]:
        """Return recent item metadata without storage or Telegram file IDs."""
        _require_positive_int(limit, "limit")
        if limit > 100:
            raise InvalidRecord("limit must not exceed 100")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT id, owner_user_id, media_type, file_size_bytes, created_at_ms
                   FROM items ORDER BY created_at_ms DESC, id DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
        return tuple(
            ItemSummary(
                id=int(row["id"]),
                owner_user_id=int(row["owner_user_id"]),
                media_type=str(row["media_type"]),
                file_size_bytes=(
                    None if row["file_size_bytes"] is None else int(row["file_size_bytes"])
                ),
                created_at_ms=int(row["created_at_ms"]),
            )
            for row in rows
        )

    async def delete(self, item_id: int) -> tuple[StorageMessageRef, ...] | None:
        """Atomically remove an item and its restrictive dependents.

        Returns the storage references for Telegram-side cleanup, or ``None``
        when the item does not exist. Telegram calls are left to the command
        handler after the database transaction commits.
        """
        _require_positive_int(item_id, "item_id")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT id FROM items WHERE id = ?", (item_id,)
            )
            if await cursor.fetchone() is None:
                await connection.rollback()
                return None
            cursor = await connection.execute(
                """SELECT storage_chat_id, storage_message_id, sequence
                   FROM item_storage_messages WHERE item_id = ? ORDER BY sequence""",
                (item_id,),
            )
            references = tuple(
                StorageMessageRef(
                    int(row["storage_chat_id"]),
                    int(row["storage_message_id"]),
                    int(row["sequence"]),
                )
                for row in await cursor.fetchall()
            )
            for table in (
                "share_links",
                "item_file_fingerprints",
                "item_files",
                "item_storage_messages",
            ):
                await connection.execute(f"DELETE FROM {table} WHERE item_id = ?", (item_id,))
            await connection.execute("DELETE FROM items WHERE id = ?", (item_id,))
            await connection.commit()
        return references

    async def find_item_id_by_file_unique_id(
        self, owner_user_id: int, file_unique_id: str
    ) -> int | None:
        """Return this owner's stored item for a Telegram unique-file fingerprint."""
        _require_positive_int(owner_user_id, "owner_user_id")
        _validate_text(file_unique_id, "file_unique_id", max_length=256)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT item_id FROM item_file_fingerprints
                   WHERE owner_user_id = ? AND file_unique_id = ?""",
                (owner_user_id, file_unique_id),
            )
            row = await cursor.fetchone()
        return None if row is None else int(row["item_id"])


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
        max_uses: int | None = None,
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
        if max_uses is not None:
            _require_positive_int(max_uses, "max_uses")
        try:
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    """INSERT INTO share_links
                       (item_id, token_hash, created_by_user_id, created_at_ms, expires_at_ms, max_uses)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (item_id, token_hash, created_by_user_id, created_at_ms, expires_at_ms, max_uses),
                )
                link_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Share link violates uniqueness or foreign-key constraints") from exc
        return ShareLink(
            link_id, item_id, token_hash, created_by_user_id, created_at_ms,
            expires_at_ms, None, 0, max_uses
        )

    async def find_active_by_hash(self, token_hash: bytes, *, now_ms: int) -> ShareLink | None:
        if not isinstance(token_hash, bytes) or len(token_hash) != 32:
            raise InvalidRecord("token_hash must be a 32-byte SHA-256 digest")
        _require_time(now_ms, "now_ms")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT * FROM share_links
                   WHERE token_hash = ? AND revoked_at_ms IS NULL
                     AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                     AND (max_uses IS NULL OR use_count < max_uses)""",
                (token_hash, now_ms),
            )
            row = await cursor.fetchone()
        return None if row is None else _share_link_from_row(row)

    async def consume_by_hash(self, token_hash: bytes, *, now_ms: int) -> ShareLink | None:
        """Atomically consume one delivery use, refusing expired, revoked, or exhausted links."""
        if not isinstance(token_hash, bytes) or len(token_hash) != 32:
            raise InvalidRecord("token_hash must be a 32-byte SHA-256 digest")
        _require_time(now_ms, "now_ms")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """UPDATE share_links SET use_count = use_count + 1
                   WHERE token_hash = ? AND revoked_at_ms IS NULL
                     AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                     AND (max_uses IS NULL OR use_count < max_uses)""",
                (token_hash, now_ms),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                return None
            cursor = await connection.execute(
                "SELECT * FROM share_links WHERE token_hash = ?", (token_hash,)
            )
            row = await cursor.fetchone()
            await connection.commit()
        return None if row is None else _share_link_from_row(row)

    async def consume_by_id(self, link_id: int, *, now_ms: int) -> ShareLink | None:
        """Atomically consume one still-valid use of a linked offer share link."""
        _require_positive_int(link_id, "link_id")
        _require_time(now_ms, "now_ms")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """UPDATE share_links SET use_count = use_count + 1
                   WHERE id = ? AND revoked_at_ms IS NULL
                     AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                     AND (max_uses IS NULL OR use_count < max_uses)""",
                (link_id, now_ms),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                return None
            row = await (await connection.execute(
                "SELECT * FROM share_links WHERE id = ?", (link_id,)
            )).fetchone()
            await connection.commit()
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


class UploaderRepository:
    """Persist uploader grants, redemption throttles, and their audit events."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def is_active(self, user_id: int) -> bool:
        _require_positive_int(user_id, "user_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT 1 FROM uploaders WHERE user_id = ? AND revoked_at IS NULL",
                (user_id,),
            )
            return await cursor.fetchone() is not None

    async def get(self, user_id: int) -> UploaderGrant | None:
        _require_positive_int(user_id, "user_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM uploaders WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
        return None if row is None else UploaderGrant(
            user_id=int(row["user_id"]),
            added_by=None if row["added_by"] is None else int(row["added_by"]),
            added_at=str(row["added_at"]),
            revoked_at=row["revoked_at"],
        )

    async def list_active_page(
        self, *, offset: int = 0, limit: int = 10
    ) -> tuple[int, tuple[UploaderGrant, ...]]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise InvalidRecord("offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise InvalidRecord("limit must be between 1 and 1,000")
        async with self.database.connection() as connection:
            count_row = await (await connection.execute(
                "SELECT COUNT(*) FROM uploaders WHERE revoked_at IS NULL"
            )).fetchone()
            cursor = await connection.execute(
                """SELECT user_id, added_by, added_at, revoked_at FROM uploaders
                   WHERE revoked_at IS NULL ORDER BY user_id LIMIT ? OFFSET ?""",
                (limit, offset),
            )
            rows = await cursor.fetchall()
        return int(count_row[0]), tuple(
            UploaderGrant(
                user_id=int(row["user_id"]),
                added_by=None if row["added_by"] is None else int(row["added_by"]),
                added_at=str(row["added_at"]),
                revoked_at=row["revoked_at"],
            ) for row in rows
        )

    async def add(self, *, user_id: int, added_by: int | None) -> bool:
        """Insert or restore a grant and append an audit event atomically."""
        _require_positive_int(user_id, "user_id")
        if added_by is not None:
            _require_positive_int(added_by, "added_by")
        now_ms = _now_ms()
        added_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            cursor = await connection.execute(
                "SELECT revoked_at FROM uploaders WHERE user_id = ?", (user_id,)
            )
            existing = await cursor.fetchone()
            changed = existing is None or existing["revoked_at"] is not None
            if existing is None:
                await connection.execute(
                    "INSERT INTO uploaders (user_id, added_by, added_at) VALUES (?, ?, ?)",
                    (user_id, added_by, added_at),
                )
            elif changed:
                await connection.execute(
                    """UPDATE uploaders SET added_by = ?, added_at = ?, revoked_at = NULL
                       WHERE user_id = ?""",
                    (added_by, added_at, user_id),
                )
            await connection.execute(
                """INSERT INTO audit_log
                   (actor_id, action, target_type, target_id, created_at_ms)
                   VALUES (?, 'uploader_added', 'user', ?, ?)""",
                (added_by or user_id, str(user_id), now_ms),
            )
            await connection.commit()
        return changed

    async def revoke(self, *, user_id: int, revoked_by: int) -> bool:
        """Revoke a grant, recording the administrative action atomically."""
        _require_positive_int(user_id, "user_id")
        _require_positive_int(revoked_by, "revoked_by")
        now_ms = _now_ms()
        revoked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """UPDATE uploaders SET revoked_at = ?
                   WHERE user_id = ? AND revoked_at IS NULL""",
                (revoked_at, user_id),
            )
            changed = cursor.rowcount == 1
            action = "uploader_revoked" if changed else "uploader_revoke_missing"
            await connection.execute(
                """INSERT INTO audit_log
                   (actor_id, action, target_type, target_id, created_at_ms)
                   VALUES (?, ?, 'user', ?, ?)""",
                (revoked_by, action, str(user_id), now_ms),
            )
            await connection.commit()
        return changed

    async def redeem(
        self,
        *,
        user_id: int,
        supplied_code: str,
        configured_code: str | None,
        now_ms: int | None = None,
        cooldown_ms: int = 30_000,
    ) -> str:
        """Redeem once per cooldown; return success, invalid, or rate_limited."""
        _require_positive_int(user_id, "user_id")
        _validate_text(supplied_code, "supplied_code", max_length=256)
        if configured_code is not None:
            _validate_text(configured_code, "configured_code", max_length=256)
        if isinstance(cooldown_ms, bool) or not isinstance(cooldown_ms, int) or cooldown_ms < 0:
            raise InvalidRecord("cooldown_ms must be a non-negative integer")
        attempt_at = _now_ms() if now_ms is None else now_ms
        _require_time(attempt_at, "now_ms")

        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            cursor = await connection.execute(
                "SELECT last_attempt_at_ms FROM redeem_rate_limits WHERE user_id = ?",
                (user_id,),
            )
            previous = await cursor.fetchone()
            if previous is not None and attempt_at - int(previous["last_attempt_at_ms"]) < cooldown_ms:
                await connection.execute(
                    """INSERT INTO audit_log
                       (actor_id, action, target_type, target_id, created_at_ms)
                       VALUES (?, 'redeem_fail', 'user', ?, ?)""",
                    (user_id, str(user_id), attempt_at),
                )
                await connection.commit()
                return "rate_limited"

            await connection.execute(
                """INSERT INTO redeem_rate_limits (user_id, last_attempt_at_ms)
                   VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET last_attempt_at_ms = excluded.last_attempt_at_ms""",
                (user_id, attempt_at),
            )
            matches = configured_code is not None and hmac.compare_digest(
                supplied_code.encode("utf-8"), configured_code.encode("utf-8")
            )
            if matches:
                added_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                await connection.execute(
                    """INSERT INTO uploaders (user_id, added_by, added_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET
                         added_by = excluded.added_by,
                         added_at = excluded.added_at,
                         revoked_at = NULL""",
                    (user_id, user_id, added_at),
                )
                action = "redeem_success"
                result = "success"
            else:
                action = "redeem_fail"
                result = "invalid"
            await connection.execute(
                """INSERT INTO audit_log
                   (actor_id, action, target_type, target_id, created_at_ms)
                   VALUES (?, ?, 'user', ?, ?)""",
                (user_id, action, str(user_id), attempt_at),
            )
            await connection.commit()
        return result


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
        if chat.invite_url is not None:
            _validate_text(chat.invite_url, "invite_url", max_length=512)
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
                   (chat_id, chat_type, title, username, is_enabled, created_at_ms, updated_at_ms, invite_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     chat_type = excluded.chat_type,
                     title = excluded.title,
                     username = excluded.username,
                     is_enabled = excluded.is_enabled,
                     updated_at_ms = MAX(required_chats.updated_at_ms, excluded.updated_at_ms),
                     invite_url = excluded.invite_url""",
                (chat.chat_id, chat.chat_type, chat.title, chat.username, int(chat.is_enabled), created, updated, chat.invite_url),
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
                invite_url=row["invite_url"],
            )
            for row in rows
        )


class OfferRepository:
    """Persist reusable public offers addressed by hashed slugs."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(self, offer: Offer) -> Offer:
        _require_positive_int(offer.item_id, "item_id")
        _require_positive_int(offer.created_by_user_id, "created_by_user_id")
        _validate_offer_hash(offer.slug_hash)
        _validate_text(offer.title, "title", max_length=256)
        if offer.slug_hint is not None:
            _validate_text(offer.slug_hint, "slug_hint", max_length=128)
        if offer.slug_plain is not None:
            _validate_slug_plain(offer.slug_plain)
        if offer.description is not None:
            _validate_text(offer.description, "description", max_length=4096)
        if not isinstance(offer.is_enabled, bool):
            raise InvalidRecord("is_enabled must be a boolean")
        _require_time(offer.created_at_ms, "created_at_ms")
        _require_time(offer.updated_at_ms, "updated_at_ms")
        if offer.updated_at_ms < offer.created_at_ms:
            raise InvalidRecord("updated_at_ms must not precede created_at_ms")
        try:
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    """INSERT INTO offers
                       (item_id, slug_hash, slug_plain, slug_hint, title, description, is_enabled,
                        created_by_user_id, created_at_ms, updated_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (offer.item_id, offer.slug_hash, offer.slug_plain, offer.slug_hint, offer.title,
                     offer.description, int(offer.is_enabled), offer.created_by_user_id,
                     offer.created_at_ms, offer.updated_at_ms),
                )
                offer_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Offer violates uniqueness or foreign-key constraints") from exc
        return replace(offer, id=offer_id)

    async def get_by_slug_hash(self, slug_hash: bytes, *, active_only: bool = True) -> Offer | None:
        _validate_offer_hash(slug_hash)
        query = "SELECT * FROM offers WHERE slug_hash = ?"
        parameters: tuple[object, ...] = (slug_hash,)
        if active_only:
            query += " AND is_enabled = 1"
        async with self.database.connection() as connection:
            row = await (await connection.execute(query, parameters)).fetchone()
        return None if row is None else _offer_from_row(row)

    async def exists_slug_hash(self, slug_hash: bytes) -> bool:
        """Return whether a panel-generated offer owns this token hash."""
        _validate_offer_hash(slug_hash)
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                "SELECT 1 FROM offers WHERE slug_hash = ? AND slug_plain IS NOT NULL LIMIT 1",
                (slug_hash,),
            )).fetchone()
        return row is not None

    async def get_by_id(self, offer_id: int, *, active_only: bool = False) -> Offer | None:
        _require_positive_int(offer_id, "offer_id")
        query = "SELECT * FROM offers WHERE id = ?"
        parameters: tuple[object, ...] = (offer_id,)
        if active_only:
            query += " AND is_enabled = 1"
        async with self.database.connection() as connection:
            row = await (await connection.execute(query, parameters)).fetchone()
        return None if row is None else _offer_from_row(row)

    async def list_active(self, *, item_id: int | None = None) -> tuple[Offer, ...]:
        if item_id is not None:
            _require_positive_int(item_id, "item_id")
        async with self.database.connection() as connection:
            if item_id is None:
                cursor = await connection.execute(
                    "SELECT * FROM offers WHERE is_enabled = 1 ORDER BY updated_at_ms DESC, id DESC"
                )
            else:
                cursor = await connection.execute(
                    "SELECT * FROM offers WHERE is_enabled = 1 AND item_id = ? ORDER BY updated_at_ms DESC, id DESC",
                    (item_id,),
                )
            rows = await cursor.fetchall()
        return tuple(_offer_from_row(row) for row in rows)

    async def disable(self, offer_id: int, *, updated_at_ms: int) -> bool:
        _require_positive_int(offer_id, "offer_id")
        _require_time(updated_at_ms, "updated_at_ms")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "UPDATE offers SET is_enabled = 0, updated_at_ms = MAX(updated_at_ms, ?) WHERE id = ? AND is_enabled = 1",
                (updated_at_ms, offer_id),
            )
        return cursor.rowcount == 1

    async def assign_item(self, offer_id: int, item_id: int, *, updated_at_ms: int) -> bool:
        _require_positive_int(offer_id, "offer_id")
        _require_positive_int(item_id, "item_id")
        _require_time(updated_at_ms, "updated_at_ms")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "UPDATE offers SET item_id = ?, updated_at_ms = MAX(updated_at_ms, ?) WHERE id = ?",
                (item_id, updated_at_ms, offer_id),
            )
        return cursor.rowcount == 1


class OfferRequirementRepository:
    """Manage ordered channel and reusable-rule assignments for an offer."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def add_required_chat(self, offer_id: int, chat_id: int, *, sort_order: int = 0) -> bool:
        _require_positive_int(offer_id, "offer_id")
        _require_nonzero_int(chat_id, "chat_id")
        _require_order(sort_order)
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            if await (await connection.execute("SELECT 1 FROM offers WHERE id = ?", (offer_id,))).fetchone() is None:
                await connection.rollback()
                raise InvalidRecord("Offer not found")
            if await (await connection.execute("SELECT 1 FROM required_chats WHERE chat_id = ?", (chat_id,))).fetchone() is None:
                await connection.rollback()
                raise InvalidRecord("Channel is not registered; add it via the wizard first")
            existing = await (await connection.execute(
                """SELECT 1 FROM rule_assignments ra JOIN requirements r ON r.id = ra.requirement_id
                   WHERE ra.scope_type = 'OFFER' AND ra.offer_id = ?
                     AND r.type = 'JOIN_CHAT' AND r.target_chat_id = ? LIMIT 1""",
                (offer_id, chat_id),
            )).fetchone()
            if existing is not None:
                await connection.rollback()
                return False
            cursor = await connection.execute(
                """INSERT INTO requirements (type, target, target_chat_id, settings_json, created_at_ms)
                   VALUES ('JOIN_CHAT', ?, ?, json_object('is_fake', 0, 'wait_seconds', 0), ?)""",
                (str(chat_id), chat_id, _now_ms()),
            )
            requirement_id = int(cursor.lastrowid)
            cursor = await connection.execute(
                """INSERT INTO rule_assignments (requirement_id, scope_type, offer_id, sort_order)
                   VALUES (?, 'OFFER', ?, ?)""", (requirement_id, offer_id, sort_order)
            )
            await connection.commit()
        return cursor.rowcount == 1

    async def remove_required_chat(self, offer_id: int, chat_id: int) -> bool:
        _require_positive_int(offer_id, "offer_id")
        _require_nonzero_int(chat_id, "chat_id")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """DELETE FROM rule_assignments
                   WHERE scope_type = 'OFFER' AND offer_id = ?
                     AND requirement_id IN (SELECT id FROM requirements
                                            WHERE type = 'JOIN_CHAT' AND target_chat_id = ?)""",
                (offer_id, chat_id),
            )
            await connection.execute(
                """DELETE FROM requirements WHERE type = 'JOIN_CHAT' AND target_chat_id = ?
                   AND NOT EXISTS (SELECT 1 FROM rule_assignments ra WHERE ra.requirement_id = requirements.id)""",
                (chat_id,),
            )
            await connection.commit()
        return cursor.rowcount == 1

    async def list_required_chats(self, offer_id: int, *, enabled_only: bool = True) -> tuple[OfferRequiredChat, ...]:
        _require_positive_int(offer_id, "offer_id")
        async with self.database.connection() as connection:
            rows = await (await connection.execute(
                """WITH RECURSIVE inherited(category_id) AS (
                       SELECT i.category_id FROM offers o JOIN items i ON i.id = o.item_id
                       WHERE o.id = ? AND i.category_id IS NOT NULL
                       UNION
                       SELECT cl.child_category_id FROM category_links cl
                       JOIN inherited i ON cl.parent_category_id = i.category_id
                   )
                   SELECT DISTINCT ? AS offer_id, r.target_chat_id AS chat_id,
                          ra.sort_order, 1 AS is_enabled
                   FROM rule_assignments ra JOIN requirements r ON r.id = ra.requirement_id
                   WHERE r.type = 'JOIN_CHAT' AND (
                         ra.scope_type = 'GLOBAL'
                         OR (ra.scope_type = 'CATEGORY' AND ra.category_id IN (SELECT category_id FROM inherited))
                         OR (ra.scope_type = 'OFFER' AND ra.offer_id = ?)
                   ) ORDER BY CASE ra.scope_type WHEN 'GLOBAL' THEN 0 WHEN 'CATEGORY' THEN 1 ELSE 2 END, ra.sort_order, r.target_chat_id""",
                (offer_id, offer_id, offer_id))).fetchall()
        return tuple(_offer_required_chat_from_row(row) for row in rows)

    async def add_requirement(self, offer_id: int, requirement_id: int, *, sort_order: int = 0) -> bool:
        _require_positive_int(offer_id, "offer_id")
        _require_positive_int(requirement_id, "requirement_id")
        _require_order(sort_order)
        async with self.database.connection() as connection:
            if await (await connection.execute("SELECT 1 FROM offers WHERE id = ?", (offer_id,))).fetchone() is None:
                raise InvalidRecord("Offer not found")
            if await (await connection.execute("SELECT 1 FROM requirements WHERE id = ?", (requirement_id,))).fetchone() is None:
                raise InvalidRecord("Requirement not found")
            cursor = await connection.execute(
                """INSERT INTO rule_assignments (offer_id, requirement_id, scope_type, sort_order)
                   VALUES (?, ?, 'OFFER', ?)""", (offer_id, requirement_id, sort_order)
            )
        return cursor.rowcount == 1

    async def remove_requirement(self, offer_id: int, requirement_id: int) -> bool:
        _require_positive_int(offer_id, "offer_id")
        _require_positive_int(requirement_id, "requirement_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "DELETE FROM rule_assignments WHERE scope_type = 'OFFER' AND offer_id = ? AND requirement_id = ?",
                (offer_id, requirement_id),
            )
        return cursor.rowcount == 1

    async def list_requirements(self, offer_id: int) -> tuple[OfferRequirement, ...]:
        _require_positive_int(offer_id, "offer_id")
        async with self.database.connection() as connection:
            rows = await (await connection.execute(
                """WITH RECURSIVE inherited(category_id) AS (
                       SELECT i.category_id FROM offers o JOIN items i ON i.id = o.item_id
                       WHERE o.id = ? AND i.category_id IS NOT NULL
                       UNION
                       SELECT cl.child_category_id FROM category_links cl
                       JOIN inherited i ON cl.parent_category_id = i.category_id
                   )
                   SELECT DISTINCT ? AS offer_id, ra.requirement_id, ra.sort_order
                   FROM rule_assignments ra JOIN requirements r ON r.id = ra.requirement_id
                   WHERE r.type <> 'JOIN_CHAT' AND (
                         ra.scope_type = 'GLOBAL'
                         OR (ra.scope_type = 'CATEGORY' AND ra.category_id IN (SELECT category_id FROM inherited))
                         OR (ra.scope_type = 'OFFER' AND ra.offer_id = ?)
                   ) ORDER BY CASE ra.scope_type WHEN 'GLOBAL' THEN 0 WHEN 'CATEGORY' THEN 1 ELSE 2 END, ra.sort_order, ra.requirement_id""",
                (offer_id, offer_id, offer_id))).fetchall()
        return tuple(_offer_requirement_from_row(row) for row in rows)

    async def get_requirement(self, requirement_id: int):
        """Resolve an assigned requirement through the canonical rule repository."""
        from media_bot.db.rules import RequirementRepository
        return await RequirementRepository(self.database).get(requirement_id)


class OfferDeliveryRepository:
    """Maintain idempotent per-user unlocks and repeat delivery counts."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def record_unlock(self, offer_id: int, user_id: int, *, at_ms: int) -> tuple[OfferDelivery, bool]:
        _require_positive_int(offer_id, "offer_id")
        _require_positive_int(user_id, "user_id")
        _require_time(at_ms, "at_ms")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """INSERT INTO offer_deliveries
                   (offer_id, user_id, first_unlocked_at_ms, last_delivered_at_ms, delivery_count)
                   VALUES (?, ?, ?, ?, 0) ON CONFLICT(offer_id, user_id) DO NOTHING""",
                (offer_id, user_id, at_ms, at_ms),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                await connection.execute(
                    """INSERT OR IGNORE INTO offer_events
                       (offer_id, user_id, event_type, created_at_ms)
                       VALUES (?, ?, 'unlock', ?)""", (offer_id, user_id, at_ms)
                )
            row = await (await connection.execute(
                "SELECT * FROM offer_deliveries WHERE offer_id = ? AND user_id = ?",
                (offer_id, user_id),
            )).fetchone()
            await connection.commit()
        return _offer_delivery_from_row(row), inserted

    async def record_delivery(self, offer_id: int, user_id: int, *, at_ms: int) -> OfferDelivery:
        _require_positive_int(offer_id, "offer_id")
        _require_positive_int(user_id, "user_id")
        _require_time(at_ms, "at_ms")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """UPDATE offer_deliveries SET last_delivered_at_ms = ?, delivery_count = delivery_count + 1
                   WHERE offer_id = ? AND user_id = ?""", (at_ms, offer_id, user_id)
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                raise RecordNotFound("Offer unlock does not exist for this user")
            await connection.execute(
                """INSERT INTO offer_events (offer_id, user_id, event_type, created_at_ms)
                   VALUES (?, ?, 'delivery', ?)""", (offer_id, user_id, at_ms)
            )
            row = await (await connection.execute(
                "SELECT * FROM offer_deliveries WHERE offer_id = ? AND user_id = ?",
                (offer_id, user_id),
            )).fetchone()
            await connection.commit()
        return _offer_delivery_from_row(row)


class OfferEventRepository:
    """Append offer events and aggregate conversion statistics."""

    _EVENT_TYPES = {"start", "verification", "unlock", "delivery", "delivery_failure"}

    def __init__(self, database: Database) -> None:
        self.database = database

    async def append(
        self,
        offer_id: int,
        user_id: int,
        event_type: str,
        *,
        at_ms: int,
        verification_result: str | None = None,
        metadata_json: str = "{}",
    ) -> OfferEvent | None:
        _require_positive_int(offer_id, "offer_id")
        _require_positive_int(user_id, "user_id")
        _require_time(at_ms, "at_ms")
        if event_type not in self._EVENT_TYPES:
            raise InvalidRecord("Unsupported offer event type")
        if not isinstance(metadata_json, str) or len(metadata_json) > 4096:
            raise InvalidRecord("metadata_json must be text no longer than 4096 characters")
        async with self.database.connection() as connection:
            if event_type == "unlock":
                cursor = await connection.execute(
                    """INSERT OR IGNORE INTO offer_events
                       (offer_id, user_id, event_type, verification_result, created_at_ms, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (offer_id, user_id, event_type, verification_result, at_ms, metadata_json),
                )
            else:
                cursor = await connection.execute(
                    """INSERT INTO offer_events
                       (offer_id, user_id, event_type, verification_result, created_at_ms, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (offer_id, user_id, event_type, verification_result, at_ms, metadata_json),
                )
            if cursor.rowcount != 1:
                return None
            event_id = int(cursor.lastrowid)
            row = await (await connection.execute(
                "SELECT * FROM offer_events WHERE id = ?", (event_id,)
            )).fetchone()
        return _offer_event_from_row(row)

    async def stats(self, offer_id: int | None = None) -> tuple[OfferStats, ...]:
        if offer_id is not None:
            _require_positive_int(offer_id, "offer_id")
        async with self.database.connection() as connection:
            if offer_id is None:
                cursor = await connection.execute(
                    """SELECT offers.id AS offer_id,
                       COALESCE(SUM(CASE WHEN events.event_type = 'start' THEN 1 ELSE 0 END), 0) AS starts,
                       COUNT(DISTINCT CASE WHEN events.event_type = 'start' THEN events.user_id END) AS distinct_start_users,
                       COUNT(DISTINCT CASE WHEN events.event_type = 'unlock' THEN events.user_id END) AS distinct_unlock_users,
                       COALESCE(SUM(CASE WHEN events.event_type = 'unlock' THEN 1 ELSE 0 END), 0) AS unlocks,
                       COALESCE(SUM(CASE WHEN events.event_type = 'delivery' THEN 1 ELSE 0 END), 0) AS deliveries
                       FROM offers LEFT JOIN offer_events AS events ON events.offer_id = offers.id
                       GROUP BY offers.id ORDER BY offers.id"""
                )
            else:
                cursor = await connection.execute(
                    """SELECT offers.id AS offer_id,
                       COALESCE(SUM(CASE WHEN events.event_type = 'start' THEN 1 ELSE 0 END), 0) AS starts,
                       COUNT(DISTINCT CASE WHEN events.event_type = 'start' THEN events.user_id END) AS distinct_start_users,
                       COUNT(DISTINCT CASE WHEN events.event_type = 'unlock' THEN events.user_id END) AS distinct_unlock_users,
                       COALESCE(SUM(CASE WHEN events.event_type = 'unlock' THEN 1 ELSE 0 END), 0) AS unlocks,
                       COALESCE(SUM(CASE WHEN events.event_type = 'delivery' THEN 1 ELSE 0 END), 0) AS deliveries
                       FROM offers LEFT JOIN offer_events AS events ON events.offer_id = offers.id
                       WHERE offers.id = ? GROUP BY offers.id ORDER BY offers.id""",
                    (offer_id,),
                )
            rows = await cursor.fetchall()
        result: list[OfferStats] = []
        for row in rows:
            starts = int(row["starts"])
            distinct_starts = int(row["distinct_start_users"])
            distinct_unlocks = int(row["distinct_unlock_users"])
            result.append(OfferStats(
                offer_id=int(row["offer_id"]), starts=starts,
                distinct_start_users=distinct_starts, unlocks=int(row["unlocks"]),
                distinct_unlock_users=distinct_unlocks, deliveries=int(row["deliveries"]),
                conversion_rate_percent=(distinct_unlocks / distinct_starts * 100.0) if distinct_starts else 0.0,
            ))
        return tuple(result)


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
                            last_seen_at_ms, settle_at_ms, pipeline)
                           VALUES (?, ?, ?, 'collecting', ?, ?, ?, 'm2')""",
                        (upload_id, owner_user_id, media_group_id, message.received_at_ms, message.received_at_ms, settle_at_ms),
                    )
                else:
                    upload_id = str(upload["upload_id"])
                    if str(upload["pipeline"]) != "m2":
                        raise InvalidTransition("Only M2 uploads can accept messages")
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

    async def list_recoverable(
        self, *, pipeline: str | None = None
    ) -> tuple[PendingUpload, ...]:
        if pipeline not in {None, "legacy", "m2"}:
            raise InvalidRecord("pipeline must be legacy, m2, or None")
        async with self.database.connection() as connection:
            query = """SELECT * FROM pending_uploads
                       WHERE status IN ('collecting', 'ready', 'processing')"""
            parameters: tuple[str, ...] = ()
            if pipeline is not None:
                query += " AND pipeline = ?"
                parameters = (pipeline,)
            query += " ORDER BY settle_at_ms, created_at_ms"
            cursor = await connection.execute(query, parameters)
            rows = await cursor.fetchall()
            uploads = []
            for row in rows:
                messages = await _pending_messages(connection, str(row["upload_id"]))
                uploads.append(_pending_upload_from_row(row, messages))
        return tuple(uploads)

    async def assign_category(
        self,
        upload_id: str,
        *,
        owner_user_id: int,
        category_id: int,
        selection_pending: bool = False,
    ) -> bool:
        """Assign a category only to its owner's ready upload."""
        _validate_text(upload_id, "upload_id", max_length=128)
        _require_positive_int(owner_user_id, "owner_user_id")
        _require_positive_int(category_id, "category_id")
        if not isinstance(selection_pending, bool):
            raise InvalidRecord("selection_pending must be a boolean")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT owner_user_id, status FROM pending_uploads WHERE upload_id = ?",
                (upload_id,),
            )
            upload = await cursor.fetchone()
            if (
                upload is None
                or int(upload["owner_user_id"]) != owner_user_id
                or str(upload["status"]) not in {"collecting", "ready"}
            ):
                await connection.rollback()
                return False
            cursor = await connection.execute(
                "SELECT 1 FROM categories WHERE id = ?", (category_id,)
            )
            if await cursor.fetchone() is None:
                await connection.rollback()
                return False
            await connection.execute(
                """UPDATE pending_uploads SET category_id = ?, category_selection_pending = ?
                   WHERE upload_id = ?""",
                (category_id, int(selection_pending), upload_id),
            )
            await connection.commit()
        return True

    async def mark_category_selection_pending(
        self, upload_id: str, *, owner_user_id: int
    ) -> bool:
        """Persist a no-category choice state before showing an inline keyboard."""
        _validate_text(upload_id, "upload_id", max_length=128)
        _require_positive_int(owner_user_id, "owner_user_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """UPDATE pending_uploads SET category_selection_pending = 1
                   WHERE upload_id = ? AND owner_user_id = ?
                     AND status IN ('collecting', 'ready') AND category_id IS NULL""",
                (upload_id, owner_user_id),
            )
            return cursor.rowcount == 1

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

    async def delete(self, upload_id: str) -> bool:
        """Remove a pending upload and its message rows after terminal handling."""
        _validate_text(upload_id, "upload_id", max_length=128)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "DELETE FROM pending_uploads WHERE upload_id = ?", (upload_id,)
            )
            return cursor.rowcount == 1


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
        use_count=int(row["use_count"]),
        max_uses=None if row["max_uses"] is None else int(row["max_uses"]),
    )


def _validate_offer_hash(value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise InvalidRecord("slug_hash must be a 32-byte SHA-256 digest")


def _require_order(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRecord("sort_order must be a non-negative integer")


def _validate_slug_plain(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
        raise InvalidRecord("slug_plain must be 1-64 base64url characters")


def _offer_from_row(row: sqlite3.Row) -> Offer:
    return Offer(
        id=int(row["id"]), item_id=int(row["item_id"]), slug_hash=bytes(row["slug_hash"]),
        slug_hint=row["slug_hint"], title=str(row["title"]), description=row["description"],
        is_enabled=bool(row["is_enabled"]), created_by_user_id=int(row["created_by_user_id"]),
        created_at_ms=int(row["created_at_ms"]), updated_at_ms=int(row["updated_at_ms"]),
        slug_plain=row["slug_plain"] if "slug_plain" in row.keys() else None,
        share_link_id=(int(row["share_link_id"]) if "share_link_id" in row.keys() and row["share_link_id"] is not None else None),
    )


def _offer_required_chat_from_row(row: sqlite3.Row) -> OfferRequiredChat:
    return OfferRequiredChat(int(row["offer_id"]), int(row["chat_id"]), int(row["sort_order"]), bool(row["is_enabled"]))


def _offer_requirement_from_row(row: sqlite3.Row) -> OfferRequirement:
    return OfferRequirement(int(row["offer_id"]), int(row["requirement_id"]), int(row["sort_order"]))


def _offer_delivery_from_row(row: sqlite3.Row) -> OfferDelivery:
    return OfferDelivery(
        int(row["offer_id"]), int(row["user_id"]), int(row["first_unlocked_at_ms"]),
        int(row["last_delivered_at_ms"]), int(row["delivery_count"]),
    )


def _offer_event_from_row(row: sqlite3.Row) -> OfferEvent:
    return OfferEvent(
        id=int(row["id"]), offer_id=int(row["offer_id"]), user_id=int(row["user_id"]),
        event_type=str(row["event_type"]), verification_result=row["verification_result"],
        created_at_ms=int(row["created_at_ms"]), metadata_json=str(row["metadata_json"]),
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
        pipeline=str(row["pipeline"]),
        category_id=None if row["category_id"] is None else int(row["category_id"]),
        category_selection_pending=bool(row["category_selection_pending"]),
    )
