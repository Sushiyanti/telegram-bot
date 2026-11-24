"""Shared, post-evaluation delivery routine for reusable offers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from typing import Any

from media_bot.db.repositories import ItemRepository, OfferDeliveryRepository, OfferEventRepository
from media_bot.media_transfer import send_one


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    ok: bool
    messages_sent: int
    failure_reason: str | None


async def deliver_offer(context: Any, offer: Any, user_id: int) -> DeliveryOutcome:
    """Unlock, copy every stored message in order, then record delivery."""
    database = context.application.bot_data["database"]
    settings = context.application.bot_data["settings"]
    now_ms = time.time_ns() // 1_000_000
    deliveries = OfferDeliveryRepository(database)
    try:
        await deliveries.record_unlock(offer.id, user_id, at_ms=now_ms)
    except Exception:
        logger.exception("offer_unlock_record_failed")
        return DeliveryOutcome(False, 0, "db_unlock_error")

    item = await ItemRepository(database).get(offer.item_id)
    references = () if item is None else tuple(sorted(item.storage_messages, key=lambda ref: ref.sequence))
    if not references:
        await _record_failure(database, offer.id, user_id, "no_storage_messages", now_ms)
        return DeliveryOutcome(False, 0, "no_storage_messages")

    sent = 0
    for index, reference in enumerate(references):
        try:
            await send_one(
                context.bot,
                reference,
                int(user_id),
                protect_content=bool(settings.default_protect_content),
            )
        except Exception:
            logger.exception("offer_copy_failed", extra={"index": index})
            await _record_failure(database, offer.id, user_id, f"copy_error_at_index:{index}", now_ms)
            return DeliveryOutcome(False, sent, "copy_error")
        sent += 1

    try:
        await deliveries.record_delivery(offer.id, user_id, at_ms=time.time_ns() // 1_000_000)
    except Exception:
        logger.exception("offer_delivery_record_failed")
        return DeliveryOutcome(False, sent, "db_delivery_error")
    return DeliveryOutcome(True, sent, None)


async def _record_failure(
    database: Any, offer_id: int, user_id: int, reason: str, at_ms: int
) -> None:
    try:
        await OfferEventRepository(database).append(
            offer_id,
            user_id,
            "delivery_failure",
            at_ms=at_ms,
            metadata_json=json.dumps({"reason": reason}, separators=(",", ":")),
        )
    except Exception:
        logger.exception("offer_delivery_failure_event_failed")


__all__ = ["DeliveryOutcome", "deliver_offer"]
