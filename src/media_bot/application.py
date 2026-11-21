"""Telegram application construction and safe placeholder commands."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from media_bot.auth import admin_only
from media_bot.config import Settings, configure_admin_ids
from media_bot.db.admins import AdminRepository
from media_bot.db.database import Database
from media_bot.handlers.admin import delete_command, list_command
from media_bot.handlers.categories import (
    add_category_command,
    add_requirement_command,
    category_selection_callback,
    delete_category_callback,
    delete_category_command,
    link_category_command,
)
from media_bot.handlers.diagnostics import configcheck_command, whoami_command
from media_bot.handlers.policy import enforce_user_command_policy, toggle_commands_command
from media_bot.handlers.panel import (
    admin_panel_callback,
    admin_panel_command,
    admin_panel_input,
)
from media_bot.handlers.reactions import ReactionUpdateHandler
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

    async def post_init(application: Application) -> None:
        await AdminRepository(database).seed_configured(settings.admin_ids)
        await upload_handler.recover(application)

    application = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(upload_handler.shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["database"] = database
    application.bot_data["upload_handler"] = upload_handler
    application.bot_data["reaction_update_handler"] = ReactionUpdateHandler(database)

    application.add_handler(TypeHandler(Update, enforce_user_command_policy), group=-1)

    application.add_handler(CommandHandler("start", start_command))
    private_commands = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("admin", admin_only(admin_panel_command), filters=private_commands))
    application.add_handler(CommandHandler("panel", admin_only(admin_panel_command), filters=private_commands))
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
    application.add_handler(
        CommandHandler("addcategory", admin_only(add_category_command), filters=private_commands)
    )
    application.add_handler(
        CommandHandler("linkcategory", admin_only(link_category_command), filters=private_commands)
    )
    application.add_handler(
        CommandHandler("addrequirement", admin_only(add_requirement_command), filters=private_commands)
    )
    application.add_handler(
        CommandHandler("delcategory", admin_only(delete_category_command), filters=private_commands)
    )
    application.add_handler(
        CommandHandler("togglecommands", admin_only(toggle_commands_command), filters=private_commands)
    )
    application.add_handler(
        CallbackQueryHandler(admin_panel_callback, pattern=r"^m6:")
    )
    application.add_handler(
        CallbackQueryHandler(delete_category_callback, pattern=r"^delcat:")
    )
    application.add_handler(
        CallbackQueryHandler(category_selection_callback, pattern=r"^uselect:")
    )
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("configcheck", configcheck_command))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, admin_panel_input)
    )
    upload_filter = filters.ChatType.PRIVATE & (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.ANIMATION
        | filters.VIDEO_NOTE
    )
    application.add_handler(MessageHandler(upload_filter, upload_handler.handle_message), group=1)
    application.add_handler(
        TypeHandler(Update, application.bot_data["reaction_update_handler"].handle_update),
        group=2,
    )
    application.add_error_handler(error_handler)
    return application
