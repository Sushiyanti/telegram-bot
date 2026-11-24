"""Storage and delivery transfer seam, independent of links and database operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class StorageMessageRef:
    """Stable reference to a message stored in the private Telegram channel."""

    chat_id: int
    message_id: int
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class TransferCapabilities:
    """End-to-end file-size limit verified for a specific transfer backend."""

    verified_max_file_size_bytes: int | None

    def __post_init__(self) -> None:
        value = self.verified_max_file_size_bytes
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError("verified_max_file_size_bytes must be a non-negative integer or None")

    def supports(self, file_size_bytes: int | None) -> bool:
        """Fail closed when size or end-to-end capability is unknown."""
        limit = self.verified_max_file_size_bytes
        return (
            file_size_bytes is not None
            and isinstance(file_size_bytes, int)
            and not isinstance(file_size_bytes, bool)
            and file_size_bytes >= 0
            and limit is not None
            and file_size_bytes <= limit
        )


class MediaTransferBackend(Protocol):
    """Small interface for replacing the Telegram transfer method later.

    Concrete implementations belong to a later milestone. This contract only
    copies messages into storage and to recipients; it does not own link policy,
    administrator checks, membership, or database persistence.
    """

    @property
    def capabilities(self) -> TransferCapabilities:
        """Return only experimentally verified end-to-end limits."""
        ...

    async def copy_to_storage(
        self,
        source_chat_id: int,
        source_message_ids: Sequence[int],
        storage_chat_id: int,
        *,
        protect_content: bool,
    ) -> tuple[StorageMessageRef, ...]:
        """Copy one or more source messages to private storage."""
        ...

    async def copy_to_recipient(
        self,
        storage_messages: Sequence[StorageMessageRef],
        recipient_chat_id: int,
        *,
        protect_content: bool,
    ) -> None:
        """Copy stored messages into a recipient chat."""
        ...


async def send_one(
    bot: object,
    reference: StorageMessageRef,
    recipient_chat_id: int,
    *,
    protect_content: bool,
) -> object:
    """Copy one stored message through the mockable Telegram transfer seam."""
    return await bot.copy_message(
        chat_id=recipient_chat_id,
        from_chat_id=reference.chat_id,
        message_id=reference.message_id,
        protect_content=protect_content,
    )
