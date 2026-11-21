"""Persist only channel posts and identifiable user reactions observed by this bot."""
from __future__ import annotations

import logging
import time
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from media_bot.db.rules import ReactionRepository

logger = logging.getLogger(__name__)


class ReactionUpdateHandler:
    def __init__(self, database) -> None:
        self.repository = ReactionRepository(database)

    async def handle_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        post = getattr(update, "channel_post", None)
        if post is not None:
            chat = getattr(post, "chat", None)
            posted_at = getattr(post, "date", None)
            if chat is not None and getattr(chat, "id", None) is not None:
                timestamp = int(posted_at.timestamp() * 1000) if posted_at is not None else _now_ms()
                try:
                    await self.repository.observe_post(
                        int(chat.id), int(post.message_id), timestamp
                    )
                except Exception as exc:
                    logger.error("channel_post_record_failed (%s)", type(exc).__name__)
            return

        change = getattr(update, "message_reaction", None)
        if change is None:
            return
        user = getattr(change, "user", None)
        chat = getattr(change, "chat", None)
        if user is None or chat is None:
            # Anonymous reactions do not identify an individual user.
            return
        emojis = tuple(
            key for reaction in (getattr(change, "new_reaction", None) or ())
            if (key := _reaction_key(reaction)) is not None
        )
        change_date = getattr(change, "date", None)
        changed_at_ms = int(change_date.timestamp() * 1000) if change_date is not None else _now_ms()
        try:
            await self.repository.replace_user_message_reactions(
                user_id=int(user.id),
                channel_id=int(chat.id),
                message_id=int(change.message_id),
                emojis=emojis,
                updated_at_ms=changed_at_ms,
            )
        except Exception as exc:
            logger.error("reaction_update_record_failed (%s)", type(exc).__name__)


def _reaction_key(reaction: Any) -> str | None:
    kind = str(getattr(reaction, "type", ""))
    emoji = getattr(reaction, "emoji", None)
    custom_id = getattr(reaction, "custom_emoji_id", None)
    if emoji:
        return str(emoji)
    if custom_id:
        return f"custom:{custom_id}"
    # Accommodate PTB wrappers where the concrete reaction subtype exposes the
    # payload but the inherited type property is not present in a mock.
    if kind.lower().endswith("emoji"):
        return kind
    return None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
