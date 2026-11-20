"""SQLite persistence for bot metadata and references."""

from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database, DatabaseError, MigrationError
from media_bot.db.models import (
    CreatedShareLink,
    MediaItem,
    PendingUpload,
    PendingUploadMessage,
    RequiredChat,
    ShareLink,
    StorageMessageRef,
)
from media_bot.media_transfer import TransferCapabilities
from media_bot.db.repositories import (
    ItemRepository,
    PendingUploadRepository,
    RequiredChatRepository,
    ShareLinkRepository,
)

__all__ = [
    "AuditLogRepository",
    "CreatedShareLink",
    "Database",
    "DatabaseError",
    "ItemRepository",
    "MediaItem",
    "MigrationError",
    "PendingUpload",
    "PendingUploadMessage",
    "PendingUploadRepository",
    "RequiredChat",
    "RequiredChatRepository",
    "ShareLink",
    "ShareLinkRepository",
    "StorageMessageRef",
    "TransferCapabilities",
]
