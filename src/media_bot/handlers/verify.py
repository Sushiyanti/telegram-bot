"""Live Verify callback handling for offer gates.

The originating private chat's in-memory ``chat_data`` stores the short nonce
produced by the Phase 2 renderer. This reuses the existing panel-style
per-chat nonce approach without adding persistent state.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from media_bot.db.repositories import OfferRepository, OfferRequirementRepository, RequiredChatRepository
from media_bot.db.rules import ReactionRepository
from media_bot.services.offer_delivery import deliver_offer
from media_bot.services.offer_evaluator import OfferEvaluator
from media_bot.services.offer_messaging import render_gate_message
from media_bot.services.verify_callback import VerifyCallbackPayload


logger = logging.getLogger(__name__)


def _nonce_store(context: ContextTypes.DEFAULT_TYPE) -> dict[int, str]:
    store = getattr(context, "chat_data", None)
    if isinstance(store, dict):
        return store
    return context.application.bot_data.setdefault("verify_nonces", {})


def remember_verify_nonce(context: ContextTypes.DEFAULT_TYPE, markup: Any) -> None:
    for row in getattr(markup, "inline_keyboard", ()):
        for button in row:
            payload = VerifyCallbackPayload.decode(getattr(button, "callback_data", ""))
            if payload is not None:
                _nonce_store(context)[payload.offer_id] = payload.nonce
                return


def _evaluator(context: ContextTypes.DEFAULT_TYPE) -> OfferEvaluator:
    database = context.application.bot_data["database"]
    return OfferEvaluator(
        bot=context.bot,
        offer_repo=OfferRepository(database),
        requirement_repo=OfferRequirementRepository(database),
        reaction_repo=ReactionRepository(database),
        chat_repo=RequiredChatRepository(database),
    )


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    payload = VerifyCallbackPayload.decode(getattr(query, "data", None))
    if payload is None:
        await query.answer("Invalid or expired button")
        return
    user = getattr(query, "from_user", None)
    if user is None or payload.user_id != int(user.id):
        await query.answer("This button is not for you")
        return
    if _nonce_store(context).get(payload.offer_id) != payload.nonce:
        await query.answer("Expired, please resend /start")
        return

    database = context.application.bot_data["database"]
    offer = await OfferRepository(database).get_by_id(payload.offer_id, active_only=False)
    if offer is None or not offer.is_enabled:
        await query.answer("Offer unavailable")
        await _edit_or_reply(query, "This offer is unavailable.")
        return
    try:
        result = await _evaluator(context).evaluate(offer.id, payload.user_id)
    except Exception as exc:
        logger.exception("verify_offer_evaluation_failed (%s)", type(exc).__name__)
        await query.answer("Offer unavailable")
        await _edit_or_reply(query, "This offer is temporarily unavailable.")
        return
    if not result.all_passed:
        text, markup = render_gate_message(offer, result)
        remember_verify_nonce(context, markup)
        await _edit_or_reply(query, text, reply_markup=markup)
        return

    outcome = await deliver_offer(context, offer, payload.user_id)
    if outcome.ok:
        await _edit_or_reply(query, "Unlocked. Your files were delivered.")
    else:
        await _edit_or_reply(query, "Delivery failed. Please try again later.")


async def _edit_or_reply(query: Any, text: str, *, reply_markup: Any = None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        message = getattr(query, "message", None)
        if message is not None:
            await message.reply_text(text, reply_markup=reply_markup)


__all__ = ["remember_verify_nonce", "verify_callback"]
