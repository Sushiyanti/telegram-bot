"""Minimal Telegram application shell for Milestone 0."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from media_bot.config import Settings


logger = logging.getLogger(__name__)


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Explain that the bot is not active yet; sharing is added later."""
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
    application = Application.builder().token(settings.bot_token).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_error_handler(error_handler)
    return application
