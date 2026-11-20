"""Telegram application construction and safe placeholder commands."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from media_bot.config import Settings, configure_admin_ids
from media_bot.db.database import Database
from media_bot.handlers.diagnostics import configcheck_command, whoami_command


logger = logging.getLogger(__name__)


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Explain that upload and sharing workflows remain out of scope."""
    del context
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "This bot is being configured. File sharing is not available yet."
        )


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log only the error type, not message contents or credential-bearing URLs."""
    del update
    error_type = type(context.error).__name__ if context.error is not None else "Unknown"
    logger.error("Unhandled Telegram update failure (%s)", error_type)


def build_application(settings: Settings) -> Application:
    """Construct the PTB application without making network requests."""
    configure_admin_ids(settings.admin_ids)
    application = Application.builder().token(settings.bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["database"] = Database(settings.db_path)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("configcheck", configcheck_command))
    application.add_error_handler(error_handler)
    return application
