"""Typed records persisted by the SQLite layer."""

from __future__ import annotations

from dataclasses import dataclass

from media_bot.media_transfer import StorageMessageRef


@dataclass(frozen=True, slots=True)
class ItemFile:
    """Source-file metadata retained for one member of a stored item."""

    sequence: int
    media_type: str
    file_id: str
    file_unique_id: str
    file_size_bytes: int
    file_name: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class MediaItem:
    """Metadata for one logical stored item; bytes remain in Telegram."""

    owner_user_id: int
    media_type: str
    file_size_bytes: int | None
    storage_messages: tuple[StorageMessageRef, ...]
    file_name: str | None = None
    mime_type: str | None = None
    caption: str | None = None
    protect_content: bool = False
    id: int | None = None
    created_at_ms: int | None = None
    sha256: bytes | None = None
    files: tuple[ItemFile, ...] = ()
    category_id: int | None = None
    storage_channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class ItemSummary:
    """Non-sensitive fields used by the admin recent-items command."""

    id: int
    owner_user_id: int
    media_type: str
    file_size_bytes: int | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ShareLink:
    """Persisted share-link metadata. It never contains the bearer token."""

    id: int
    item_id: int
    token_hash: bytes
    created_by_user_id: int
    created_at_ms: int
    expires_at_ms: int | None
    revoked_at_ms: int | None
    use_count: int = 0
    max_uses: int | None = None


@dataclass(frozen=True, slots=True)
class UploaderGrant:
    """Current uploader grant and the actor/timestamps that established it."""

    user_id: int
    added_by: int | None
    added_at: str
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class CreatedShareLink:
    """The raw bearer token is returned once alongside its persisted record."""

    link: ShareLink
    token: str


@dataclass(frozen=True, slots=True)
class RequiredChat:
    """A channel or group in the permanent required-chat registry."""

    chat_id: int
    chat_type: str
    title: str
    username: str | None = None
    is_enabled: bool = True
    created_at_ms: int | None = None
    updated_at_ms: int | None = None
    invite_url: str | None = None


@dataclass(frozen=True, slots=True)
class PendingUploadMessage:
    """Durable Telegram message metadata needed to resume album processing."""

    source_chat_id: int
    source_message_id: int
    sequence: int
    media_type: str
    file_id: str
    file_unique_id: str
    file_size_bytes: int | None
    received_at_ms: int
    file_name: str | None = None
    mime_type: str | None = None
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class PendingUpload:
    """Persistent work record for a Telegram media group or single upload."""

    upload_id: str
    owner_user_id: int
    media_group_id: str
    status: str
    created_at_ms: int
    last_seen_at_ms: int
    settle_at_ms: int
    messages: tuple[PendingUploadMessage, ...] = ()
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    error_code: str | None = None
    pipeline: str = "legacy"
    category_id: int | None = None
    category_selection_pending: bool = False


@dataclass(frozen=True, slots=True)
class Offer:
    """A reusable public offer addressed by an opaque slug hash."""

    id: int
    item_id: int
    slug_hash: bytes
    slug_hint: str | None
    title: str
    description: str | None
    is_enabled: bool
    created_by_user_id: int
    created_at_ms: int
    updated_at_ms: int
    slug_plain: str | None = None
    share_link_id: int | None = None


@dataclass(frozen=True, slots=True)
class OfferRequiredChat:
    """An ordered channel/group membership gate assigned to an offer."""

    offer_id: int
    chat_id: int
    sort_order: int
    is_enabled: bool


@dataclass(frozen=True, slots=True)
class OfferRequirement:
    """An ordered reusable requirement definition assigned to an offer."""

    offer_id: int
    requirement_id: int
    sort_order: int


@dataclass(frozen=True, slots=True)
class OfferDelivery:
    """Per-user unlock and repeat-delivery ledger for an offer."""

    offer_id: int
    user_id: int
    first_unlocked_at_ms: int
    last_delivered_at_ms: int
    delivery_count: int


@dataclass(frozen=True, slots=True)
class OfferEvent:
    """Append-only offer conversion event."""

    id: int
    offer_id: int
    user_id: int
    event_type: str
    verification_result: str | None
    created_at_ms: int
    metadata_json: str


@dataclass(frozen=True, slots=True)
class OfferStats:
    """Aggregated per-offer conversion metrics."""

    offer_id: int
    starts: int
    distinct_start_users: int
    unlocks: int
    distinct_unlock_users: int
    deliveries: int
    conversion_rate_percent: float
