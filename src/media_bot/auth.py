"""Configuration-based administrator authorization for Telegram handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from media_bot import config
from media_bot.config import Settings
from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.repositories import UploaderRepository


logger = logging.getLogger(__name__)
CommandHandlerFunction = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def is_admin(user_id: int) -> bool:
    """Return whether a numeric Telegram user ID is in validated config.ADMIN_IDS."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False
    return user_id in config.ADMIN_IDS


def can_upload(user_id: int, settings: Settings) -> bool:
    """Return authorization from the static portion of the uploader allowlist."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False
    if settings.uploader_mode == "all":
        return True
    return user_id in settings.admin_ids or user_id in settings.uploader_ids


async def can_upload_with_registry(
    user_id: int, settings: Settings, uploaders: UploaderRepository
) -> bool:
    """Apply the M3 effective allowlist, including active database grants."""
    return can_upload(user_id, settings) or await uploaders.is_active(user_id)


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
