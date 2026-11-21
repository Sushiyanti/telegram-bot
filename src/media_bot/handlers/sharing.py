"""M3 uploader self-verification, share-link, and delivery commands."""

from __future__ import annotations

import logging
import re
from typing import Any

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from media_bot.auth import can_upload_with_registry, is_admin
from media_bot.config import Settings
from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database
from media_bot.db.repositories import ItemRepository, ShareLinkRepository, UploaderRepository
from media_bot.db.rules import CategoryRepository
from media_bot.media_transfer import TransferCapabilities
from media_bot.services import ShareLinkService
from media_bot.services.rule_engine import RequirementEvaluator


logger = logging.getLogger(__name__)
_ITEM_ID = re.compile(r"^[0-9]+$")


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def _private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and getattr(chat, "type", None) == "private"


def _is_missing_storage_message_error(error: BadRequest) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "message to copy not found",
            "message_id_invalid",
            "message identifier is not specified",
        )
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliver the stored item addressed by a valid private share-link token."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not context.args:
        await message.reply_text("Welcome. Open a valid share link to receive a file.")
        return
    if not _private_chat(update):
        await message.reply_text("Open this share link in a private chat with the bot.")
        return

    token = context.args[0]
    settings = _settings(context)
    database = _database(context)
    items = ItemRepository(database)
    links = ShareLinkService(
        ShareLinkRepository(database),
        items,
        TransferCapabilities(settings.verified_transfer_max_file_bytes),
    )
    link = await links.find_active(token)
    if link is None:
        await message.reply_text("This share link is invalid, expired, revoked, or exhausted.")
        return
    item = await items.get(link.item_id)
    if item is None or not item.storage_messages:
        await message.reply_text("This shared item is unavailable.")
        return
    expected_storage_id = item.storage_channel_id
    if expected_storage_id is None:
        category = await CategoryRepository(database).get(item.category_id) if item.category_id else None
        expected_storage_id = (
            category.storage_channel_id
            if category is not None and category.storage_channel_id is not None
            else settings.storage_channel_id
        )
    if any(reference.chat_id != expected_storage_id for reference in item.storage_messages):
        await message.reply_text("This shared item is unavailable.")
        return
    if not TransferCapabilities(settings.verified_transfer_max_file_bytes).supports(
        item.file_size_bytes
    ):
        await message.reply_text("Delivery is not verified for this file size.")
        return

    if settings.required_channel_id is not None:
        try:
            membership = await context.bot.get_chat_member(
                chat_id=settings.required_channel_id,
                user_id=int(user.id),
            )
        except Exception as exc:
            logger.warning("required_channel_check_failed (%s)", type(exc).__name__)
            await message.reply_text(
                "Membership could not be verified. Please contact the bot administrator."
            )
            return
        status = str(getattr(membership, "status", "")).lower()
        member = status in {"creator", "administrator", "member"}
        if status == "restricted":
            member = bool(getattr(membership, "is_member", False))
        if not member:
            channel = "@channel"
            try:
                chat = await context.bot.get_chat(settings.required_channel_id)
                username = getattr(chat, "username", None)
                if isinstance(username, str) and username:
                    channel = f"@{username.lstrip('@')}"
            except Exception as exc:
                logger.warning("required_channel_name_lookup_failed (%s)", type(exc).__name__)
            await message.reply_text(f"Join {channel} to access.")
            return

    category_id = item.category_id
    if category_id is None:
        category_id = (await CategoryRepository(database).default()).id
    if not await RequirementEvaluator(database).verify(
        category_id=category_id,
        user_id=int(user.id),
        bot=context.bot,
        message=message,
    ):
        return

    consumed = await links.consume(token)
    if consumed is None:
        await message.reply_text("This share link is invalid, expired, revoked, or exhausted.")
        return
    try:
        for reference in sorted(item.storage_messages, key=lambda value: value.sequence):
            await context.bot.copy_message(
                chat_id=int(user.id),
                from_chat_id=reference.chat_id,
                message_id=reference.message_id,
                protect_content=settings.default_protect_content,
            )
    except BadRequest as exc:
        if _is_missing_storage_message_error(exc):
            logger.warning("share_item_unavailable (storage message missing)")
            try:
                await items.delete(item.id if item.id is not None else link.item_id)
            except Exception as cleanup_exc:
                logger.error("unavailable_item_cleanup_failed (%s)", type(cleanup_exc).__name__)
            await message.reply_text("This file is no longer available")
            return
        logger.error("share_delivery_failed (%s)", type(exc).__name__)
        await message.reply_text("Delivery failed. Please contact the bot administrator.")
        return
    except Exception as exc:
        logger.error("share_delivery_failed (%s)", type(exc).__name__)
        await message.reply_text("Delivery failed. Please contact the bot administrator.")
        return

    await message.reply_text("Delivered.")
    await _audit(database, actor_id=int(user.id), action="share_delivery", target_id=str(consumed.id))


async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a hashed bearer link for a stored item and return its Telegram URL."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not _private_chat(update):
        await message.reply_text("Use /share in a private chat with the bot.")
        return
    settings = _settings(context)
    database = _database(context)
    uploaders = UploaderRepository(database)
    admin = await is_admin(int(user.id), database)
    allowed = admin or int(user.id) in settings.uploader_ids
    if not allowed:
        allowed = await uploaders.is_active(int(user.id))
    if not allowed:
        await message.reply_text("Not authorized.")
        return
    if len(context.args) != 1 or _ITEM_ID.fullmatch(context.args[0]) is None:
        await message.reply_text("Usage: /share <item_id>")
        return
    item_id = int(context.args[0])
    items = ItemRepository(database)
    item = await items.get(item_id)
    if item is None:
        await message.reply_text("Could not create a share link for that item.")
        return
    if not admin and item.owner_user_id != int(user.id):
        await message.reply_text("You can only share your own files.")
        return
    try:
        me = await context.bot.get_me()
    except Exception as exc:
        logger.error("share_bot_identity_lookup_failed (%s)", type(exc).__name__)
        await message.reply_text("Share links are temporarily unavailable.")
        return
    username = getattr(me, "username", None)
    if not isinstance(username, str) or not username:
        await message.reply_text("Share links are temporarily unavailable.")
        return

    links = ShareLinkService(
        ShareLinkRepository(database),
        items,
        TransferCapabilities(settings.verified_transfer_max_file_bytes),
    )
    try:
        created = await links.create(item_id=item_id, created_by_user_id=int(user.id))
    except Exception as exc:
        logger.warning("share_link_creation_failed (%s)", type(exc).__name__)
        await message.reply_text("Could not create a share link for that item.")
        return
    await message.reply_text(f"https://t.me/{username.lstrip('@')}?start={created.token}")
    await _audit(database, actor_id=int(user.id), action="share_created", target_id=str(item_id))


async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Redeem the configured uploader code with an atomic 30-second cooldown."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not _private_chat(update):
        await message.reply_text("Redeem uploader codes in a private chat with the bot.")
        return
    if len(context.args) != 1:
        await message.reply_text("Usage: /redeem <code>")
        return
    result = await UploaderRepository(_database(context)).redeem(
        user_id=int(user.id),
        supplied_code=context.args[0],
        configured_code=_settings(context).uploader_redeem_code,
    )
    if result == "success":
        await message.reply_text("Uploader access verified.")
    elif result == "rate_limited":
        await message.reply_text("Please wait 30 seconds before trying again.")
    else:
        await message.reply_text("Invalid verification code.")


async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grant uploader access; this handler is registered behind admin_only."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if len(context.args) != 1 or _ITEM_ID.fullmatch(context.args[0]) is None:
        await message.reply_text("Usage: /adduser <user_id>")
        return
    target_id = int(context.args[0])
    if target_id <= 0:
        await message.reply_text("User ID must be a positive numeric ID.")
        return
    changed = await UploaderRepository(_database(context)).add(
        user_id=target_id,
        added_by=int(user.id),
    )
    await message.reply_text("Uploader access granted." if changed else "User is already an uploader.")


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke a database-backed uploader grant; this handler is admin-only."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if len(context.args) != 1 or _ITEM_ID.fullmatch(context.args[0]) is None:
        await message.reply_text("Usage: /revoke <user_id>")
        return
    target_id = int(context.args[0])
    if target_id <= 0:
        await message.reply_text("User ID must be a positive numeric ID.")
        return
    if await is_admin(target_id, _database(context)):
        await message.reply_text("Administrators cannot be revoked.")
        return
    changed = await UploaderRepository(_database(context)).revoke(
        user_id=target_id,
        revoked_by=int(user.id),
    )
    await message.reply_text("Uploader access revoked." if changed else "No active uploader grant found.")


async def _audit(database: Database, *, actor_id: int, action: str, target_id: str) -> None:
    try:
        await AuditLogRepository(database).record_event(
            actor_id=actor_id,
            action=action,
            target_type="share_link",
            target_id=target_id,
        )
    except Exception as exc:
        logger.error("share_audit_write_failed (%s)", type(exc).__name__)


def admin_managed_handlers() -> tuple[Any, Any]:
    """Return admin-protected handlers without creating circular auth imports."""
    from media_bot.auth import admin_only

    return admin_only(adduser_command), admin_only(revoke_command)
