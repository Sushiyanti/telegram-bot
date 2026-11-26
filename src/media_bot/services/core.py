"""Core coordination services, separate from Telegram handlers and transfer backends."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import uuid
from dataclasses import dataclass

from media_bot.db.models import (
    CreatedShareLink,
    MediaItem,
    PendingUpload,
    PendingUploadMessage,
    RequiredChat,
    ShareLink,
)
from media_bot.db.repositories import (
    InvalidRecord,
    ItemRepository,
    PendingUploadRepository,
    RecordNotFound,
    RequiredChatRepository,
    ShareLinkRepository,
)
from media_bot.media_transfer import TransferCapabilities


class SharingUnavailable(RuntimeError):
    """Raised when an item cannot safely be offered through a share link."""


class UnsupportedFileSize(SharingUnavailable):
    """Raised when size is unknown or exceeds verified end-to-end capability."""


_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def hash_share_token(token: str) -> bytes:
    """Return a binary SHA-256 digest; raw bearer tokens are never persisted."""
    if not isinstance(token, str) or not token or not token.isascii() or len(token) > 200:
        raise ValueError("Share token must be non-empty ASCII text")
    return hashlib.sha256(token.encode("ascii")).digest()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class ItemService:
    """Coordinate storage metadata creation after a backend has stored messages."""

    def __init__(self, repository: ItemRepository) -> None:
        self.repository = repository

    async def create_stored_item(self, item: MediaItem) -> MediaItem:
        """Commit item metadata only with at least one storage message reference."""
        return await self.repository.create_stored_item(item)

    async def get(self, item_id: int) -> MediaItem | None:
        return await self.repository.get(item_id)


class ShareLinkService:
    """Create, look up, and revoke non-expiring-by-default bearer links."""

    def __init__(
        self,
        links: ShareLinkRepository,
        items: ItemRepository,
        capabilities: TransferCapabilities,
    ) -> None:
        self.links = links
        self.items = items
        self.capabilities = capabilities

    async def create(
        self,
        *,
        item_id: int,
        created_by_user_id: int,
        expires_at_ms: int | None = None,
        max_uses: int | None = None,
    ) -> CreatedShareLink:
        """Create a share only for stored items within a verified transfer limit."""
        item = await self.items.get(item_id)
        if item is None or not item.storage_messages:
            raise SharingUnavailable("The item is not available in private storage")
        if not self.capabilities.supports(item.file_size_bytes):
            raise UnsupportedFileSize(
                "The item's size is unknown or exceeds the selected backend's verified end-to-end capability"
            )
        now = _now_ms()
        if expires_at_ms is not None and (
            isinstance(expires_at_ms, bool)
            or not isinstance(expires_at_ms, int)
            or expires_at_ms <= now
        ):
            raise InvalidRecord("expires_at_ms must be a future integer millisecond timestamp")
        token = secrets.token_urlsafe(32)
        digest = hash_share_token(token)
        link = await self.links.create(
            item_id=item_id,
            token_hash=digest,
            created_by_user_id=created_by_user_id,
            created_at_ms=now,
            expires_at_ms=expires_at_ms,
            max_uses=max_uses,
        )
        return CreatedShareLink(link=link, token=token)

    async def find_active(self, token: str) -> ShareLink | None:
        """Resolve an active token, returning no record for malformed/revoked/expired links."""
        if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
            return None
        return await self.links.find_active_by_hash(hash_share_token(token), now_ms=_now_ms())

    async def consume(self, token: str) -> ShareLink | None:
        """Atomically claim one still-valid delivery for a bearer token."""
        if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
            return None
        return await self.links.consume_by_hash(hash_share_token(token), now_ms=_now_ms())

    async def revoke(self, token: str) -> bool:
        """Revoke a bearer link idempotently without exposing the stored digest."""
        if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
            return False
        return await self.links.revoke_by_hash(hash_share_token(token), revoked_at_ms=_now_ms())

    async def list_for_item(self, item_id: int) -> tuple[ShareLink, ...]:
        return await self.links.list_for_item(item_id)


class RequiredChatService:
    """Manage durable channel/group membership requirements."""

    def __init__(self, repository: RequiredChatRepository) -> None:
        self.repository = repository

    async def set_required_chat(self, chat: RequiredChat) -> RequiredChat:
        return await self.repository.upsert(chat)

    async def list_enabled(self) -> tuple[RequiredChat, ...]:
        return await self.repository.list_enabled()


@dataclass(frozen=True, slots=True)
class PendingMessageReceipt:
    """Upload record result, including whether a new source message was added."""

    upload: PendingUpload
    inserted: bool


class PendingUploadService:
    """Persist album parts so a future worker can recover after process restart."""

    def __init__(self, repository: PendingUploadRepository) -> None:
        self.repository = repository

    async def record_message(
        self,
        *,
        owner_user_id: int,
        media_group_id: str,
        message: PendingUploadMessage,
        settle_at_ms: int,
    ) -> PendingMessageReceipt:
        proposed_id = uuid.uuid4().hex
        inserted = await self.repository.record_message(
            upload_id=proposed_id,
            owner_user_id=owner_user_id,
            media_group_id=media_group_id,
            message=message,
            settle_at_ms=settle_at_ms,
        )
        upload = await self.repository.get_by_owner_and_group(owner_user_id, media_group_id)
        if upload is None:
            raise RecordNotFound("Pending upload disappeared after recording a message")
        return PendingMessageReceipt(upload=upload, inserted=inserted)

    async def list_recoverable(self, *, pipeline: str | None = None) -> tuple[PendingUpload, ...]:
        return await self.repository.list_recoverable(pipeline=pipeline)

    async def delete(self, upload_id: str) -> bool:
        """Remove a pending work record after completion or rejection."""
        return await self.repository.delete(upload_id)

    async def transition(
        self,
        upload_id: str,
        status: str,
        *,
        at_ms: int | None = None,
        error_code: str | None = None,
    ) -> PendingUpload:
        return await self.repository.transition(
            upload_id,
            status,
            at_ms=_now_ms() if at_ms is None else at_ms,
            error_code=error_code,
        )
