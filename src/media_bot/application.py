"""Telegram application construction and safe placeholder commands."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from media_bot.auth import admin_only
from media_bot.config import Settings, configure_admin_ids
from media_bot.db.database import Database
from media_bot.handlers.admin import delete_command, list_command
from media_bot.handlers.diagnostics import configcheck_command, whoami_command
from media_bot.handlers.sharing import (
    adduser_command,
    redeem_command,
    revoke_command,
    share_command,
    start_command,
)
from media_bot.handlers.uploads import UploadHandler


logger = logging.getLogger(__name__)


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
    if settings.uploader_mode == "all" and settings.env == "prod":
        logger.warning(
            "UPLOADER_MODE=all allows any private user to upload while ENV=prod"
        )
    database = Database(settings.db_path)
    upload_handler = UploadHandler(settings, database)
    application = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(upload_handler.recover)
        .post_shutdown(upload_handler.shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["database"] = database
    application.bot_data["upload_handler"] = upload_handler

    application.add_handler(CommandHandler("start", start_command))
    private_commands = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("share", share_command, filters=private_commands))
    application.add_handler(CommandHandler("redeem", redeem_command, filters=private_commands))
    application.add_handler(
        CommandHandler("adduser", admin_only(adduser_command), filters=private_commands)
    )
    application.add_handler(
        CommandHandler("revoke", admin_only(revoke_command), filters=private_commands)
    )
    application.add_handler(
        CommandHandler("list", admin_only(list_command), filters=private_commands)
    )
    application.add_handler(
        CommandHandler("delete", admin_only(delete_command), filters=private_commands)
    )
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("configcheck", configcheck_command))
    upload_filter = filters.ChatType.PRIVATE & (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.ANIMATION
        | filters.VIDEO_NOTE
    )
    application.add_handler(MessageHandler(upload_filter, upload_handler.handle_message))
    application.add_error_handler(error_handler)
    return application
