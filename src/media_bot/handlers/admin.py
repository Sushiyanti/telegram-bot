"""Admin commands for listing and deleting stored media items."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from media_bot.config import Settings
from media_bot.db.database import Database
from media_bot.db.repositories import ItemRepository


logger = logging.getLogger(__name__)
_INTEGER = re.compile(r"^[0-9]+$")
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 100


def _database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List recent item metadata; never include Telegram file IDs."""
    message = update.effective_message
    if message is None:
        return
    if len(context.args) > 1 or (
        context.args
        and (
            len(context.args[0]) > 3
            or _INTEGER.fullmatch(context.args[0]) is None
        )
    ):
        await message.reply_text("Usage: /list [limit] (1-100)")
        return
    limit = _DEFAULT_LIMIT if not context.args else int(context.args[0])
    if not 1 <= limit <= _MAX_LIMIT:
        await message.reply_text("Usage: /list [limit] (1-100)")
        return

    items = await ItemRepository(_database(context)).list_recent(limit)
    if not items:
        await message.reply_text("No items found.")
        return
    pages = ["Recent items:"]
    for item in items:
        size = "unknown" if item.file_size_bytes is None else f"{item.file_size_bytes} B"
        created = datetime.fromtimestamp(
            item.created_at_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        line = (
            f"{item.id} | uploader {item.owner_user_id} | {item.media_type} | "
            f"{size} | {created}"
        )
        if len(pages[-1]) + len(line) + 1 > 3_500:
            pages.append("Recent items (continued):")
        pages[-1] += f"\n{line}"
    for page in pages:
        await message.reply_text(page)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete an item and its DB references, then best-effort delete Telegram posts."""
    message = update.effective_message
    if message is None:
        return
    if (
        len(context.args) != 1
        or len(context.args[0]) > 19
        or _INTEGER.fullmatch(context.args[0]) is None
    ):
        await message.reply_text("Usage: /delete <item_id>")
        return
    item_id = int(context.args[0])
    if item_id <= 0 or item_id > 9_223_372_036_854_775_807:
        await message.reply_text("Item ID must be a positive numeric ID.")
        return

    references = await ItemRepository(_database(context)).delete(item_id)
    if references is None:
        await message.reply_text("Item not found.")
        return

    settings = _settings(context)
    failed = 0
    deleted = 0
    for reference in references:
        if reference.chat_id != settings.storage_channel_id:
            failed += 1
            logger.error("item_storage_delete_skipped (unexpected chat ID)")
            continue
        try:
            await context.bot.delete_message(
                chat_id=reference.chat_id,
                message_id=reference.message_id,
            )
            deleted += 1
        except Exception as exc:
            failed += 1
            logger.warning("storage_message_delete_failed (%s)", type(exc).__name__)

    if failed:
        await message.reply_text(
            f"Item {item_id} deleted from the database. "
            f"Could not delete {failed} storage message(s) from Telegram."
        )
    else:
        await message.reply_text(
            f"Item {item_id} deleted; removed {deleted} storage message(s)."
        )
