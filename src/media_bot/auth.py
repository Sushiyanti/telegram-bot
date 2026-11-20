"""Configuration-based administrator authorization for Telegram handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from media_bot import config
from media_bot.db.audit_repository import AuditLogRepository


logger = logging.getLogger(__name__)
CommandHandlerFunction = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def is_admin(user_id: int) -> bool:
    """Return whether a numeric Telegram user ID is in validated config.ADMIN_IDS."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False
    return user_id in config.ADMIN_IDS


def admin_only(handler: CommandHandlerFunction) -> CommandHandlerFunction:
    """Refuse non-admin calls, appending a denial event to the audit log."""
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is not None and is_admin(user.id):
            await handler(update, context)
            return

        if user is not None:
            database = context.application.bot_data.get("database")
            if database is None:
                logger.error("admin_denied audit unavailable (database not configured)")
            else:
                try:
                    await AuditLogRepository(database).record_admin_denied(
                        actor_id=user.id,
                        target_id=handler.__name__,
                    )
                except Exception as exc:
                    logger.error(
                        "admin_denied audit write failed (%s)", type(exc).__name__
                    )

        message = update.effective_message
        if message is not None:
            await message.reply_text("Not authorized.")

    return wrapped
