"""SQLite persistence for bot metadata and references."""

from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database, DatabaseError, MigrationError
from media_bot.db.models import (
    CreatedShareLink,
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
)
from media_bot.db.repositories import (
    ItemRepository,
    OfferDeliveryRepository,
    OfferEventRepository,
    OfferRepository,
    OfferRequirementRepository,
    PendingUploadRepository,
    RequiredChatRepository,
    ShareLinkRepository,
)
from media_bot.media_transfer import TransferCapabilities

__all__ = [
    "AuditLogRepository",
    "CreatedShareLink",
    "Database",
    "DatabaseError",
    "ItemRepository",
    "MediaItem",
    "MigrationError",
    "Offer",
    "OfferDelivery",
    "OfferDeliveryRepository",
    "OfferEvent",
    "OfferEventRepository",
    "OfferRepository",
    "OfferRequiredChat",
    "OfferRequirement",
    "OfferRequirementRepository",
    "OfferStats",
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
