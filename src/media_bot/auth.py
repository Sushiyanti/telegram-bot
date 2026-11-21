"""Environment- and database-backed authorization for Telegram handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from media_bot import config
from media_bot.config import Settings
from media_bot.db.admins import AdminRepository
from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database
from media_bot.db.repositories import UploaderRepository


logger = logging.getLogger(__name__)
CommandHandlerFunction = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _valid_user_id(user_id: int) -> bool:
    return (
        not isinstance(user_id, bool)
        and isinstance(user_id, int)
        and 0 < user_id <= (1 << 63) - 1
    )


async def is_admin(user_id: int, database: Database | None = None) -> bool:
    """Check the immutable environment fallback, then the durable admin registry."""
    if not _valid_user_id(user_id):
        return False
    if user_id in config.ADMIN_IDS:
        return True
    if database is None:
        return False
    try:
        return await AdminRepository(database).is_admin(user_id)
    except Exception as exc:
        logger.error("admin_role_lookup_failed (%s)", type(exc).__name__)
        return False


def can_upload(user_id: int, settings: Settings) -> bool:
    """Return authorization from the static portion of the uploader allowlist."""
    if not _valid_user_id(user_id):
        return False
    if settings.uploader_mode == "all":
        return True
    return user_id in settings.admin_ids or user_id in settings.uploader_ids


async def can_upload_with_registry(
    user_id: int, settings: Settings, uploaders: UploaderRepository
) -> bool:
    """Apply admins, static settings, and active DB uploader grants."""
    if not _valid_user_id(user_id):
        return False
    if can_upload(user_id, settings) or await is_admin(user_id, uploaders.database):
        return True
    return await uploaders.is_active(user_id)


def admin_only(handler: CommandHandlerFunction) -> CommandHandlerFunction:
    """Refuse non-admin calls, appending a denial event to the audit log."""
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        database = context.application.bot_data.get("database")
        if user is not None and await is_admin(user.id, database):
            await handler(update, context)
            return

        if user is not None:
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
