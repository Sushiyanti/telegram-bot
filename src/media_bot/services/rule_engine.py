"""Dynamic category requirement evaluation for share-link delivery."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from media_bot.db.rules import CategoryRepository, ReactionRepository, Requirement
from media_bot.db.repositories import InvalidRecord
from media_bot.db.rules import validate_telegram_link

logger = logging.getLogger(__name__)


class RequirementEvaluator:
    """Evaluate global, category, and recursively inherited rules before delivery."""

    def __init__(self, database) -> None:
        self.categories = CategoryRepository(database)
        self.reactions = ReactionRepository(database)

    async def verify(self, *, category_id: int, user_id: int, bot: Any, message: Any) -> bool:
        requirements = await self.categories.requirements_for(category_id)
        for requirement in requirements:
            if not await self._verify_one(requirement, user_id=user_id, bot=bot, message=message):
                return False
        return True

    async def _verify_one(self, requirement: Requirement, *, user_id: int, bot: Any, message: Any) -> bool:
        settings = requirement.settings
        wait_seconds = int(settings.get("wait_seconds", 0))
        if requirement.type == "TIMED_WAIT":
            await _wait_with_message(message, wait_seconds)
            return True
        if bool(settings.get("is_fake", False)):
            await _prompt_then_wait(message, requirement, wait_seconds)
            return True

        if requirement.type == "START_BOT":
            # The repository rejects real START_BOT rules; this guard protects
            # against manually inserted or pre-release database rows as well.
            logger.error("unsupported_real_start_bot_requirement")
            await message.reply_text(
                "This access requirement cannot currently be verified. Please contact the administrator."
            )
            return False

        if requirement.type == "JOIN_CHAT":
            try:
                membership = await bot.get_chat_member(
                    chat_id=int(requirement.target), user_id=user_id
                )
            except Exception as exc:
                logger.warning("join_chat_check_failed (%s); using wait fallback", type(exc).__name__)
                await _wait_with_message(message, wait_seconds)
                return True
            status = str(getattr(membership, "status", "")).lower()
            is_member = status in {"creator", "administrator", "member"}
            if status == "restricted":
                is_member = bool(getattr(membership, "is_member", False))
            if is_member:
                return True
            channel_label = "the required chat"
            try:
                chat = await bot.get_chat(int(requirement.target))
                username = getattr(chat, "username", None)
                if isinstance(username, str) and username:
                    channel_label = f"@{username.lstrip('@')}"
            except Exception as exc:
                logger.debug("join_chat_label_lookup_failed (%s)", type(exc).__name__)
            await message.reply_text(
                f"Join {channel_label} to access this file.",
                reply_markup=_link_markup(requirement, "Open required channel or group"),
            )
            return False

        if requirement.type == "REACT":
            last_x_messages = int(settings.get("last_x_messages", 5))
            channel_id = int(requirement.target)
            try:
                bot_id = getattr(bot, "id", None)
                if not isinstance(bot_id, int) or bot_id <= 0:
                    identity = await bot.get_me()
                    bot_id = int(identity.id)
                membership = await bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
                if str(getattr(membership, "status", "")).lower() not in {"administrator", "creator"}:
                    await _wait_with_message(message, wait_seconds)
                    return True
            except Exception as exc:
                logger.warning("reaction_admin_check_failed (%s); using wait fallback", type(exc).__name__)
                await _wait_with_message(message, wait_seconds)
                return True
            has_reaction = await self.reactions.has_recent_reaction(
                user_id=user_id,
                channel_id=channel_id,
                last_x_messages=last_x_messages,
                last_x_days=int(settings.get("last_x_days", 0)),
            )
            if has_reaction:
                return True
            await message.reply_text(
                "React to at least one recent post in the required channel to access this file.",
                reply_markup=_link_markup(requirement, "Open required channel"),
            )
            return False

        logger.error("unknown_requirement_type")
        await message.reply_text("An access requirement could not be evaluated.")
        return False


async def _wait_with_message(message: Any, wait_seconds: int) -> None:
    await message.reply_text("Please wait...")
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)


async def _prompt_then_wait(
    message: Any, requirement: Requirement, wait_seconds: int
) -> None:
    prompts = {
        "JOIN_CHAT": "Please join the required channel or group to continue.",
        "REACT": "Please react to a post in the required channel to continue.",
        "START_BOT": "Please open the required bot to continue.",
    }
    text = prompts.get(requirement.type, "Please continue to access this file.")
    if wait_seconds > 0:
        text += "\nPlease wait..."
    await message.reply_text(
        text,
        reply_markup=_link_markup(requirement, _link_button_label(requirement.type)),
    )
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)


def _link_button_label(requirement_type: str) -> str:
    return {
        "JOIN_CHAT": "Open required channel or group",
        "REACT": "Open required channel",
        "START_BOT": "Open required bot",
    }.get(requirement_type, "Open Telegram link")


def _link_markup(
    requirement: Requirement, label: str
) -> InlineKeyboardMarkup | None:
    try:
        link_url = validate_telegram_link(requirement.settings.get("link_url"))
    except InvalidRecord:
        logger.warning("invalid_requirement_prompt_link (requirement_id=%s)", requirement.id)
        return None
    if link_url is None:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=link_url)]])
