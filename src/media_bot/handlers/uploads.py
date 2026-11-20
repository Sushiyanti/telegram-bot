"""Private-DM media ingestion using Telegram message-copy methods only."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from telegram import Update
from telegram.ext import ContextTypes

from media_bot.auth import can_upload
from media_bot.config import Settings
from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database
from media_bot.db.models import ItemFile, MediaItem, PendingUploadMessage
from media_bot.db.repositories import ItemRepository, PendingUploadRepository
from media_bot.media_transfer import StorageMessageRef
from media_bot.services import PendingUploadService


logger = logging.getLogger(__name__)
DEFAULT_ALBUM_DEBOUNCE_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class _MessageMedia:
    media_type: str
    file_id: str | None
    file_unique_id: str | None
    file_size_bytes: int | None
    file_name: str | None
    mime_type: str | None


class _CopyResultError(RuntimeError):
    """Raised when Telegram does not return a complete, usable copy result."""


def _extract_media(message: Any) -> _MessageMedia | None:
    """Select the Telegram file object for one supported message type."""
    for media_type in (
        "document",
        "video",
        "audio",
        "voice",
        "animation",
        "video_note",
    ):
        media = getattr(message, media_type, None)
        if media is not None:
            return _MessageMedia(
                media_type=media_type,
                file_id=getattr(media, "file_id", None),
                file_unique_id=getattr(media, "file_unique_id", None),
                file_size_bytes=getattr(media, "file_size", None),
                file_name=getattr(media, "file_name", None) or None,
                mime_type=getattr(media, "mime_type", None) or None,
            )

    photos = getattr(message, "photo", None)
    if photos:
        photo = max(
            photos,
            key=lambda value: (
                getattr(value, "width", 0) * getattr(value, "height", 0),
                getattr(value, "file_size", None) or 0,
            ),
        )
        return _MessageMedia(
            media_type="photo",
            file_id=getattr(photo, "file_id", None),
            file_unique_id=getattr(photo, "file_unique_id", None),
            file_size_bytes=getattr(photo, "file_size", None),
            file_name=None,
            mime_type=None,
        )
    return None


class UploadHandler:
    """Gate, buffer, copy, deduplicate, and persist private-chat uploads."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        debounce_seconds: float = DEFAULT_ALBUM_DEBOUNCE_SECONDS,
    ) -> None:
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds cannot be negative")
        self.settings = settings
        self.database = database
        self.items = ItemRepository(database)
        self.pending = PendingUploadService(PendingUploadRepository(database))
        self.audit = AuditLogRepository(database)
        self.debounce_seconds = debounce_seconds
        self._album_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._group_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._group_lock_users: dict[tuple[int, str], int] = {}
        self._rejected_groups: dict[tuple[int, str], float] = {}
        self._process_lock = asyncio.Lock()

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        chat = getattr(message, "chat", None)
        if chat is None or getattr(chat, "type", None) != "private":
            return
        media = _extract_media(message)
        if media is None:
            return

        user_id = int(user.id)
        bot = context.bot
        if not can_upload(user_id, self.settings):
            await self._audit(
                actor_id=user_id,
                action="upload_denied",
                target_id="uploader_policy",
            )
            await self._reply(message, bot, user_id, "Uploads are not open.")
            return

        chat_id = int(chat.id)
        source_message_id = int(message.message_id)
        raw_group_id = getattr(message, "media_group_id", None)
        is_album = raw_group_id is not None
        group_id = (
            str(raw_group_id)
            if is_album
            else f"single:{chat_id}:{source_message_id}"
        )
        key = (chat_id, group_id)

        async with self._locked_group(key):
            now_monotonic = time.monotonic()
            for rejected_key, expires_at in tuple(self._rejected_groups.items()):
                if expires_at <= now_monotonic:
                    self._rejected_groups.pop(rejected_key, None)
            if self._rejected_groups.get(key, 0) > now_monotonic:
                await self._reject(
                    message,
                    bot,
                    user_id,
                    "album_rejected",
                    "This upload was rejected.",
                )
                return
            self._rejected_groups.pop(key, None)

            rejection = self._pre_copy_rejection(message, media)
            if rejection is not None:
                await self._reject(
                    message,
                    bot,
                    user_id,
                    rejection[0],
                    rejection[1],
                    group_key=key if is_album else None,
                )
                return
            if not media.file_id or not media.file_unique_id:
                await self._reject(
                    message,
                    bot,
                    user_id,
                    "metadata_unknown",
                    "Cannot store this media.",
                    group_key=key if is_album else None,
                )
                return

            source_chat_id = int(chat.id)
            received_at_ms = time.time_ns() // 1_000_000
            settle_at_ms = received_at_ms + int(self.debounce_seconds * 1000)
            pending_message = PendingUploadMessage(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                sequence=(
                    int(message.message_id)
                    if is_album
                    else 0
                ),
                media_type=media.media_type,
                file_id=media.file_id,
                file_unique_id=media.file_unique_id,
                file_size_bytes=media.file_size_bytes,
                received_at_ms=received_at_ms,
                file_name=media.file_name,
                mime_type=media.mime_type,
                caption=getattr(message, "caption", None),
            )
            try:
                receipt = await self.pending.record_message(
                    owner_user_id=user_id,
                    media_group_id=group_id,
                    message=pending_message,
                    settle_at_ms=settle_at_ms,
                )
            except Exception as exc:
                logger.error(
                    "upload_pending_record_failed",
                    extra={"event_name": "upload_pending_record_failed", "exception_type": type(exc).__name__},
                )
                if is_album:
                    self._rejected_groups[key] = time.monotonic() + max(
                        2 * self.debounce_seconds, 0.1
                    )
                    pending_task = self._album_tasks.pop(key, None)
                    if pending_task is not None and not pending_task.done():
                        pending_task.cancel()
                    try:
                        prior_upload = await self.pending.repository.get_by_owner_and_group(
                            user_id, group_id
                        )
                        if prior_upload is not None and prior_upload.pipeline == "m2":
                            await self._delete_pending(prior_upload.upload_id)
                    except Exception as cleanup_exc:
                        logger.error(
                            "pending_upload_cleanup_failed",
                            extra={"event_name": "pending_upload_cleanup_failed", "exception_type": type(cleanup_exc).__name__},
                        )
                await self._audit(
                    actor_id=user_id,
                    action="storage_copy_failed",
                    target_id="pending_record",
                )
                await self._reply(
                    message, bot, user_id, "Storage temporarily unavailable."
                )
                return

            if is_album:
                if receipt.inserted:
                    self._schedule_album(
                        key,
                        receipt.upload.upload_id,
                        user_id,
                        bot,
                        message,
                    )
                return

            await self._process_upload(
                receipt.upload.upload_id,
                user_id,
                chat_id,
                group_id,
                bot,
                message,
            )

    def _pre_copy_rejection(
        self, message: Any, media: _MessageMedia
    ) -> tuple[str, str] | None:
        size = media.file_size_bytes
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return "size_unknown", "Cannot store this media: size unavailable."
        if size > self.settings.max_relay_file_bytes:
            return "too_large", "Cannot store this media: file too large."
        is_protected = bool(
            getattr(message, "has_protected_content", False)
            or getattr(message, "protect_content", False)
        )
        if is_protected:
            return "protected_source", "Cannot store protected media."
        return None

    def _schedule_album(
        self,
        key: tuple[int, str],
        upload_id: str,
        user_id: int,
        bot: Any,
        reply_message: Any,
        *,
        delay: float | None = None,
    ) -> None:
        prior = self._album_tasks.get(key)
        if prior is not None and not prior.done():
            prior.cancel()
        task = asyncio.create_task(
            self._settle_album(
                key,
                upload_id,
                user_id,
                bot,
                reply_message,
                self.debounce_seconds if delay is None else delay,
            ),
            name="telegram-album-settle",
        )
        self._album_tasks[key] = task

    async def _settle_album(
        self,
        key: tuple[int, str],
        upload_id: str,
        user_id: int,
        bot: Any,
        reply_message: Any | None,
        delay: float,
    ) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            async with self._locked_group(key):
                await self._process_upload(
                    upload_id,
                    user_id,
                    key[0],
                    key[1],
                    bot,
                    reply_message,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "album_settle_failed",
                extra={"event_name": "album_settle_failed", "exception_type": type(exc).__name__},
            )
        finally:
            if self._album_tasks.get(key) is asyncio.current_task():
                self._album_tasks.pop(key, None)

    @asynccontextmanager
    async def _locked_group(
        self, key: tuple[int, str]
    ) -> AsyncIterator[None]:
        """Serialize one album key without retaining idle locks indefinitely."""
        lock = self._group_locks.setdefault(key, asyncio.Lock())
        self._group_lock_users[key] = self._group_lock_users.get(key, 0) + 1
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            remaining = self._group_lock_users[key] - 1
            if remaining == 0:
                self._group_lock_users.pop(key, None)
                if self._group_locks.get(key) is lock:
                    self._group_locks.pop(key, None)
            else:
                self._group_lock_users[key] = remaining

    async def _process_upload(
        self,
        upload_id: str,
        user_id: int,
        chat_id: int,
        group_id: str,
        bot: Any,
        reply_message: Any | None,
    ) -> None:
        async with self._process_lock:
            is_album = not group_id.startswith("single:")
            upload = None
            try:
                upload = await self.pending.repository.get(upload_id)
                if upload is None or upload.owner_user_id != user_id:
                    return
                if upload.status == "collecting":
                    upload = await self.pending.transition(upload_id, "ready")
                if upload.status == "ready":
                    upload = await self.pending.transition(upload_id, "processing")
            except asyncio.CancelledError:
                await self._fail_upload(
                    upload_id,
                    user_id,
                    bot,
                    None,
                    len(upload.messages) if upload is not None else 0,
                    "cancelled_before_copy",
                    notify=False,
                )
                raise
            except Exception as exc:
                await self._fail_upload(
                    upload_id,
                    user_id,
                    bot,
                    reply_message,
                    len(upload.messages) if upload is not None else 0,
                    type(exc).__name__,
                )
                return
            if upload.status != "processing" or not upload.messages:
                await self._fail_upload(
                    upload_id,
                    user_id,
                    bot,
                    reply_message,
                    len(upload.messages),
                    "invalid_pending_state",
                )
                return

            try:
                for media_file in upload.messages:
                    existing_item_id = await self.items.find_item_id_by_file_unique_id(
                        user_id, media_file.file_unique_id
                    )
                    if existing_item_id is not None:
                        await self._delete_pending(upload_id)
                        await self._audit(
                            actor_id=user_id,
                            action="upload_rejected",
                            target_id=f"already_stored:item_id={existing_item_id}",
                        )
                        await self._reply(
                            reply_message,
                            bot,
                            chat_id,
                            f"Already stored. Item ID: {existing_item_id}.",
                        )
                        return
            except asyncio.CancelledError:
                await self._fail_upload(
                    upload_id,
                    user_id,
                    bot,
                    None,
                    len(upload.messages),
                    "cancelled_during_dedupe",
                    notify=False,
                )
                raise
            except Exception as exc:
                await self._fail_upload(
                    upload_id,
                    user_id,
                    bot,
                    reply_message,
                    len(upload.messages),
                    type(exc).__name__,
                )
                return

            try:
                ordered_messages = tuple(sorted(upload.messages, key=lambda part: part.sequence))
                if not is_album:
                    copied = (
                        await bot.copy_message(
                            chat_id=self.settings.storage_channel_id,
                            from_chat_id=ordered_messages[0].source_chat_id,
                            message_id=ordered_messages[0].source_message_id,
                            protect_content=False,
                        ),
                    )
                else:
                    copied = await bot.copy_messages(
                        chat_id=self.settings.storage_channel_id,
                        from_chat_id=ordered_messages[0].source_chat_id,
                        message_ids=[part.source_message_id for part in ordered_messages],
                        protect_content=False,
                    )
                if copied is None or len(copied) != len(ordered_messages):
                    raise _CopyResultError("Telegram returned a partial copy result")
                copied_ids = tuple(self._returned_message_id(value) for value in copied)
                if any(value is None for value in copied_ids):
                    raise _CopyResultError("Telegram returned an invalid copy result")

                total_bytes = sum(int(part.file_size_bytes) for part in ordered_messages)
                item_files = tuple(
                    ItemFile(
                        sequence=part.sequence,
                        media_type=part.media_type,
                        file_id=part.file_id,
                        file_unique_id=part.file_unique_id,
                        file_size_bytes=int(part.file_size_bytes),
                        file_name=part.file_name,
                        mime_type=part.mime_type,
                    )
                    for part in ordered_messages
                )
                references = tuple(
                    StorageMessageRef(
                        self.settings.storage_channel_id,
                        int(message_id),
                        part.sequence,
                    )
                    for part, message_id in zip(ordered_messages, copied_ids, strict=True)
                )
                first_named = next(
                    (part for part in ordered_messages if part.file_name is not None),
                    ordered_messages[0],
                )
                item = MediaItem(
                    owner_user_id=user_id,
                    media_type=("album" if is_album else ordered_messages[0].media_type),
                    file_size_bytes=total_bytes,
                    storage_messages=references,
                    file_name=first_named.file_name,
                    mime_type=first_named.mime_type,
                    caption=next(
                        (part.caption for part in ordered_messages if part.caption),
                        None,
                    ),
                    protect_content=self.settings.default_protect_content,
                    sha256=None,
                    files=item_files,
                )
                saved = await self.items.create_stored_item(
                    item,
                    clear_pending_upload_id=upload_id,
                )
            except asyncio.CancelledError:
                await self._fail_upload(
                    upload_id,
                    user_id,
                    bot,
                    None,
                    len(upload.messages),
                    "cancelled_during_copy",
                    notify=False,
                )
                raise
            except Exception as exc:
                await self._fail_upload(
                    upload_id,
                    user_id,
                    bot,
                    reply_message,
                    len(upload.messages),
                    type(exc).__name__,
                )
                return

            if not is_album:
                confirmation = (
                    f"Stored. Item ID: {saved.id} | "
                    f"Size: {total_bytes} bytes."
                )
            else:
                confirmation = (
                    f"Stored. Item ID: {saved.id} | "
                    f"Messages: {len(ordered_messages)} | "
                    f"Total size: {total_bytes} bytes."
                )
            await self._reply(reply_message, bot, chat_id, confirmation)

    @staticmethod
    def _returned_message_id(value: Any) -> int | None:
        message_id = getattr(value, "message_id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return None
        return message_id

    async def _reject(
        self,
        message: Any,
        bot: Any,
        user_id: int,
        reason: str,
        reply: str,
        *,
        group_key: tuple[int, str] | None = None,
    ) -> None:
        if group_key is not None:
            self._rejected_groups[group_key] = time.monotonic() + max(
                2 * self.debounce_seconds, 0.1
            )
            task = self._album_tasks.pop(group_key, None)
            if task is not None and not task.done():
                task.cancel()
            try:
                upload = await self.pending.repository.get_by_owner_and_group(
                    user_id, group_key[1]
                )
                if upload is not None and upload.pipeline == "m2":
                    await self._delete_pending(upload.upload_id)
            except Exception as exc:
                logger.error(
                    "pending_upload_cleanup_failed",
                    extra={"event_name": "pending_upload_cleanup_failed", "exception_type": type(exc).__name__},
                )
        await self._audit(
            actor_id=user_id,
            action="upload_rejected",
            target_id=reason,
        )
        await self._reply(message, bot, user_id, reply)

    async def _fail_upload(
        self,
        upload_id: str,
        user_id: int,
        bot: Any,
        reply_message: Any | None,
        file_count: int,
        error_type: str,
        *,
        notify: bool = True,
    ) -> None:
        logger.error(
            "storage_copy_failed",
            extra={
                "event_name": "storage_copy_failed",
                "exception_type": error_type,
                "file_count": file_count,
            },
        )
        await self._audit(
            actor_id=user_id,
            action="storage_copy_failed",
            target_id="storage_copy",
        )
        await self._delete_pending(upload_id)
        if notify:
            await self._reply(
                reply_message,
                bot,
                user_id,
                "Storage temporarily unavailable.",
            )

    async def _delete_pending(self, upload_id: str) -> None:
        try:
            await self.pending.delete(upload_id)
        except Exception as exc:
            logger.error(
                "pending_upload_cleanup_failed",
                extra={"event_name": "pending_upload_cleanup_failed", "exception_type": type(exc).__name__},
            )

    async def _audit(self, *, actor_id: int, action: str, target_id: str) -> None:
        try:
            await self.audit.record_event(
                actor_id=actor_id,
                action=action,
                target_type="upload",
                target_id=target_id,
            )
        except Exception as exc:
            logger.error(
                "upload_audit_write_failed",
                extra={"event_name": "upload_audit_write_failed", "exception_type": type(exc).__name__},
            )

    @staticmethod
    async def _reply(
        message: Any | None, bot: Any, chat_id: int, text: str
    ) -> None:
        try:
            if message is not None:
                await message.reply_text(text)
            else:
                await bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            logger.error(
                "upload_reply_failed",
                extra={"event_name": "upload_reply_failed", "exception_type": type(exc).__name__},
            )

    async def recover(self, application: Any) -> None:
        """Resume settled collecting uploads after a restart; discard uncertain copies."""
        recoverable = await self.pending.list_recoverable(pipeline="m2")
        now_ms = time.time_ns() // 1_000_000
        for upload in recoverable:
            if not upload.messages:
                await self._fail_upload(
                    upload.upload_id,
                    upload.owner_user_id,
                    application.bot,
                    None,
                    0,
                    "empty_pending_upload",
                )
                continue
            key = (upload.messages[0].source_chat_id, upload.media_group_id)
            if upload.status == "processing":
                await self._fail_upload(
                    upload.upload_id,
                    upload.owner_user_id,
                    application.bot,
                    None,
                    len(upload.messages),
                    "interrupted_processing",
                )
                continue
            delay = (
                max(0.0, (upload.settle_at_ms - now_ms) / 1000)
                if upload.status == "collecting"
                else 0.0
            )
            self._schedule_album(
                key,
                upload.upload_id,
                upload.owner_user_id,
                application.bot,
                None,
                delay=delay,
            )

    async def shutdown(self, application: Any) -> None:
        """Cancel debounce jobs and allow active processing to clean pending state."""
        del application
        tasks = tuple(self._album_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
