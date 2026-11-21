"""Database-backed non-admin command and upload lockdown."""
from __future__ import annotations

import logging
from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from media_bot.auth import can_upload_with_registry
from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database
from media_bot.db.repositories import UploaderRepository
from media_bot.db.rules import BotSettingsRepository

logger = logging.getLogger(__name__)
_ALLOWED_COMMANDS = {"start"}
_MEDIA_FIELDS = (
    "document", "photo", "video", "audio", "voice", "animation", "video_note"
)


def _database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


async def toggle_commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if context.args:
        await message.reply_text("Usage: /togglecommands")
        return
    enabled = await BotSettingsRepository(_database(context)).toggle_user_commands()
    action = "user_commands_enabled" if enabled else "user_commands_disabled"
    try:
        await AuditLogRepository(_database(context)).record_event(
            actor_id=int(user.id), action=action, target_type="bot_setting",
            target_id=BotSettingsRepository.COMMANDS_KEY,
        )
    except Exception as exc:
        logger.error("command_toggle_audit_failed (%s)", type(exc).__name__)
    await message.reply_text(
        "Regular-user commands and uploads are enabled."
        if enabled else "Regular-user commands and uploads are locked."
    )


async def enforce_user_command_policy(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Stop disallowed commands/uploads only for users outside the effective allowlist."""
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    bot_data = context.application.bot_data
    database = bot_data["database"]
    settings = bot_data["settings"]
    uploaders = UploaderRepository(database)
    if await can_upload_with_registry(int(user.id), settings, uploaders):
        return
    try:
        enabled = await BotSettingsRepository(database).user_commands_enabled()
    except Exception as exc:
        logger.error("command_policy_read_failed (%s)", type(exc).__name__)
        enabled = False
    if enabled:
        return

    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    command = ""
    if isinstance(text, str) and text.startswith("/"):
        token = text.split(maxsplit=1)[0].split("@", 1)[0]
        command = token[1:].lower()
    is_disallowed_command = bool(command) and command not in _ALLOWED_COMMANDS
    has_media = any(getattr(message, field, None) for field in _MEDIA_FIELDS)
    if is_disallowed_command or has_media:
        await message.reply_text("Commands are currently locked.")
        raise ApplicationHandlerStop
