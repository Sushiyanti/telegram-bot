"""Single-message inline admin control panel for M6."""
from __future__ import annotations

import logging
import math
import re
import secrets
import time
import hashlib
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from media_bot.auth import is_admin
from media_bot.config import Settings
from media_bot.db.admins import AdminRepository
from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database
from media_bot.db.repositories import (
    InvalidRecord,
    ItemRepository,
    OfferEventRepository,
    OfferRepository,
    OfferRequirementRepository,
    RequiredChatRepository,
    RecordNotFound,
    UploaderRepository,
)
from media_bot.db.rules import (
    BotSettingsRepository,
    CategoryRepository,
    RequirementRepository,
    validate_telegram_link,
)
from media_bot.db.models import Offer, RequiredChat
from media_bot.services import hash_share_token

logger = logging.getLogger(__name__)
_PANEL_KEY = "m6_admin_panel"
_PAGE_SIZE = 6
_PENDING_TIMEOUT_SECONDS = 300
_MAX_ID = (1 << 63) - 1
_NUMERIC_ID = re.compile(r"^[0-9]{1,19}$")
_SIGNED_NUMERIC_ID = re.compile(r"^-?[0-9]{1,19}$")


def _database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _panel_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    value = context.application.bot_data.get("bot_username")
    if not isinstance(value, str):
        return None
    username = value.strip().lstrip("@")
    if not username or username.casefold() in {"yourbot", "your_bot", "bot_username"}:
        return None
    return username


def _panel(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    value = context.user_data.get(_PANEL_KEY)
    return value if isinstance(value, dict) else None


def _has_pending_flow(state: dict[str, Any]) -> bool:
    return isinstance(state.get("input"), dict) or isinstance(state.get("wizard"), dict)


def _touch_pending_flow(state: dict[str, Any]) -> None:
    if _has_pending_flow(state):
        state["pending_last_activity"] = time.monotonic()
    else:
        state.pop("pending_last_activity", None)


def _expire_pending_flow(state: dict[str, Any]) -> bool:
    """Clear an idle user-data wizard and reset the panel to its home view."""
    if not _has_pending_flow(state):
        return False
    last_activity = state.get("pending_last_activity")
    now = time.monotonic()
    if (
        not isinstance(last_activity, (int, float))
        or now - float(last_activity) >= _PENDING_TIMEOUT_SECONDS
    ):
        state.update({"input": None, "wizard": None, "view": "home", "closed": False})
        state["nonce"] = secrets.token_hex(4)
        state.pop("category_draft_name", None)
        state.pop("pending_last_activity", None)
        return True
    state["pending_last_activity"] = now
    return False


async def expire_panel_flow_before_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Expire an idle private-panel flow before a command is routed normally."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    state = _panel(context)
    text = getattr(message, "text", None)
    if (
        user is None
        or message is None
        or chat is None
        or getattr(chat, "type", None) != "private"
        or not isinstance(text, str)
        or not text.startswith("/")
        or state is None
        or int(state.get("owner_user_id", 0)) != int(user.id)
        or int(state.get("chat_id", 0)) != int(chat.id)
    ):
        return
    if _expire_pending_flow(state):
        await _edit_panel(
            context, state, "Admin Control Panel", _home_markup(state)
        )


def _callback(state: dict[str, Any], action: str, *values: object) -> str:
    payload = ":".join(("m6", str(state["nonce"]), action, *(str(value) for value in values)))
    if len(payload.encode("utf-8")) > 64:
        raise ValueError("M6 callback payload exceeded Telegram's callback-data limit")
    return payload


def _button(text: str, state: dict[str, Any], action: str, *values: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=_callback(state, action, *values))


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _home_markup(state: dict[str, Any]) -> InlineKeyboardMarkup:
    return _markup([
        [_button("📣 Campaigns", state, "campaigns", 0), _button("📜 Requirements", state, "requirements", 0)],
        [_button("👥 Staff", state, "staff"), _button("⚙️ Settings", state, "settings")],
        [_button("📊 Stats", state, "stats")],
        [_button("Close Menu", state, "close")],
    ])


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open a fresh private panel and deactivate any earlier panel message."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None or chat is None or getattr(chat, "type", None) != "private":
        return
    database = _database(context)
    if not await is_admin(int(user.id), database):
        await message.reply_text("Not authorized.")
        return

    previous = _panel(context)
    if previous is not None and int(previous.get("chat_id", 0)) == int(chat.id):
        previous.update({
            "nonce": secrets.token_hex(4),
            "view": "closed",
            "closed": True,
        })
        _clear_pending_flow(previous)
        try:
            await context.bot.edit_message_text(
                chat_id=int(previous["chat_id"]),
                message_id=int(previous["message_id"]),
                text="This panel was replaced by a newer /admin command.",
                reply_markup=None,
            )
        except Exception as exc:
            logger.info("previous_admin_panel_deactivate_failed (%s)", type(exc).__name__)

    state = {
        "nonce": secrets.token_hex(4),
        "owner_user_id": int(user.id),
        "chat_id": int(chat.id),
        "message_id": 0,
        "view": "home",
        "closed": False,
        "input": None,
        "wizard": None,
    }
    try:
        sent = await context.bot.send_message(
            chat_id=int(chat.id),
            text="Admin Control Panel",
            reply_markup=_home_markup(state),
        )
    except Exception as exc:
        logger.warning("admin_panel_send_failed (%s)", type(exc).__name__)
        return
    state["message_id"] = int(sent.message_id)
    context.user_data[_PANEL_KEY] = state


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Validate a panel callback and edit only its bound panel message."""
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    data = str(query.data or "")
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "m6":
        await query.answer("Invalid panel action.", show_alert=True)
        return

    database = _database(context)
    if not await is_admin(int(user.id), database):
        await _audit_denial(database, int(user.id), "admin_panel_callback")
        await query.answer("Not authorized.", show_alert=True)
        return

    state = _panel(context)
    query_message = getattr(query, "message", None)
    query_chat = getattr(query_message, "chat", None)
    if (
        state is None
        or state.get("closed")
        or int(state.get("owner_user_id", 0)) != int(user.id)
        or str(state.get("nonce")) != parts[1]
        or query_message is None
        or query_chat is None
        or int(getattr(query_chat, "id", 0)) != int(state.get("chat_id", -1))
        or int(getattr(query_message, "message_id", 0)) != int(state.get("message_id", -1))
        or int(getattr(query_chat, "id", 0)) != int(user.id)
    ):
        await query.answer("This panel is expired or unavailable.", show_alert=True)
        return

    await query.answer()
    if _expire_pending_flow(state):
        await _edit_panel(context, state, "Admin Control Panel", _home_markup(state), query=query)
        return
    state["nonce"] = secrets.token_hex(4)
    _touch_pending_flow(state)
    action, *args = parts[2:]
    try:
        await _dispatch_callback(action, args, query, context, state)
    except ValueError:
        logger.debug("malformed_admin_panel_callback data=%r", data)
        await _edit_panel(context, state, "That button is out of date; reopen the menu", _back_home(state), query=query)
    except (InvalidRecord, RecordNotFound) as exc:
        await _edit_panel(context, state, f"Could not complete that action: {exc}", _back_home(state), query=query)
    except Exception:
        logger.exception("admin_panel_action_failed")
        await _edit_panel(
            context,
            state,
            "That action could not be completed. Return to the menu and try again.",
            _back_home(state),
            query=query,
        )


async def admin_panel_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consume only pending private-panel input and edit the stored message in place."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    state = _panel(context)
    if user is None or message is None or chat is None or state is None:
        return
    if getattr(chat, "type", None) != "private" or int(chat.id) != int(state.get("chat_id", 0)):
        return
    if int(state.get("owner_user_id", 0)) != int(user.id):
        return
    if not await is_admin(int(user.id), _database(context)):
        _clear_pending_flow(state)
        await _audit_denial(_database(context), int(user.id), "admin_panel_input")
        return
    if state.get("closed"):
        _clear_pending_flow(state)
        return
    if _expire_pending_flow(state):
        await _edit_panel(context, state, "Admin Control Panel", _home_markup(state))
        return
    pending = state.get("input")
    if not isinstance(pending, dict):
        return
    state["nonce"] = secrets.token_hex(4)
    _touch_pending_flow(state)

    if pending.get("kind") in {"add_admin", "add_uploader"}:
        target_id = _forwarded_user_id(message)
        if target_id is None:
            target_id = _parse_user_id(getattr(message, "text", None))
        if target_id is None:
            await _edit_panel(
                context,
                state,
                "Please send a positive numeric Telegram user ID or forward a message from that user.",
                _input_markup(state),
            )
            raise ApplicationHandlerStop
        actor_id = int(user.id)
        if pending["kind"] == "add_admin":
            if target_id in _settings(context).admin_ids:
                result = "That user is already an environment super-admin."
            else:
                changed = await AdminRepository(_database(context)).add(
                    user_id=target_id, added_by=actor_id
                )
                result = "Administrator added successfully." if changed else "User is already an admin."
            await _show_staff(context, state, "admins", int(state.get("page", 0)), notice=result)
        else:
            changed = await UploaderRepository(_database(context)).add(
                user_id=target_id, added_by=actor_id
            )
            result = "Uploader added successfully." if changed else "That user is already an uploader."
            await _show_staff(context, state, "uploaders", int(state.get("page", 0)), notice=result)
        state["input"] = None
        raise ApplicationHandlerStop

    if pending.get("kind") == "category_name":
        name = getattr(message, "text", None)
        name = name.strip() if isinstance(name, str) else ""
        if not name or len(name) > 80 or name.casefold() == "global":
            await _edit_panel(
                context,
                state,
                "Send a non-empty category name of at most 80 characters. The name Global is reserved.",
                _input_markup(state),
            )
            raise ApplicationHandlerStop
        existing = await CategoryRepository(_database(context)).get_by_name(name)
        if existing is not None:
            await _edit_panel(
                context,
                state,
                "That category name already exists. Send a different category name.",
                _input_markup(state),
            )
            raise ApplicationHandlerStop
        state["category_draft_name"] = name
        state["input"] = {"kind": "category_storage"}
        state["view"] = "input_category_storage"
        _touch_pending_flow(state)
        await _edit_panel(
            context,
            state,
            f"Forward a message from the storage channel for {name}, or send its numeric chat ID. "
            "The bot needs administrator permission to post there.",
            _input_markup(state),
        )
        raise ApplicationHandlerStop

    if pending.get("kind") == "campaign_chat":
        wizard = state.get("wizard")
        value = getattr(message, "text", None)
        value = value.strip() if isinstance(value, str) else ""
        if not isinstance(wizard, dict) or not value:
            await _edit_panel(context, state, "Please send a non-empty value.", _input_markup(state))
            raise ApplicationHandlerStop
        step = wizard.get("chat_step")
        if step == "identifier":
            wizard["identifier"] = _chat_identifier(value)
            wizard["chat_step"] = "display_name"
            await _show_campaign_chat_name(context, state)
        elif step == "display_name":
            wizard["display_name"] = value
            wizard["chat_step"] = "invite_url"
            await _show_campaign_chat_invite(context, state)
        elif step == "invite_url":
            try:
                wizard["invite_url"] = validate_telegram_link(value)
            except InvalidRecord:
                await _edit_panel(context, state, "Send a valid Telegram invite URL, or press Skip.", _markup([[_button("Skip", state, "cmpchat_skipurl")], [_button("Cancel", state, "cmpchatcancel")]]))
                raise ApplicationHandlerStop
            try:
                await _finish_campaign_chat(context, state)
            except (InvalidRecord, RecordNotFound) as exc:
                logger.exception("campaign_chat_wizard_failed")
                _clear_pending_flow(state)
                await _edit_panel(context, state, f"That action could not be completed. {exc}. Reply /panel to return.", _back_home(state))
        else:
            raise InvalidRecord("channel setup step is invalid")
        raise ApplicationHandlerStop

    if pending.get("kind") == "campaign_title":
        title = getattr(message, "text", None)
        title = title.strip() if isinstance(title, str) else ""
        if not title or len(title) > 256:
            await _edit_panel(context, state, "Send a non-empty offer title (maximum 256 characters).", _input_markup(state))
            raise ApplicationHandlerStop
        wizard = state.get("wizard")
        if not isinstance(wizard, dict):
            _clear_pending_flow(state)
            await _edit_panel(context, state, "Admin Control Panel", _home_markup(state))
            raise ApplicationHandlerStop
        wizard["title"] = title
        state["input"] = None
        await _edit_panel(
            context, state,
            f"Create campaign for item {wizard['item_id']} titled “{title}”?\n\nYou can add channels and requirements after creation.",
            _markup([[_button("Create offer", state, "cmpconfirm")], [_button("Cancel", state, "campaigns", 0)]]),
        )
        raise ApplicationHandlerStop

    if pending.get("kind") == "campaign_edit":
        wizard = state.get("wizard")
        value = getattr(message, "text", None)
        value = value.strip() if isinstance(value, str) else ""
        max_length = 256 if isinstance(wizard, dict) and wizard.get("field") == "title" else 4096
        if not isinstance(wizard, dict) or not value or len(value) > max_length:
            await _edit_panel(context, state, "Please send valid non-empty text within the allowed length.", _input_markup(state))
            raise ApplicationHandlerStop
        column = "title" if wizard.get("field") == "title" else "description"
        async with _database(context).connection() as connection:
            await connection.execute(f"UPDATE offers SET {column} = ?, updated_at_ms = ? WHERE id = ?", (value, int(time.time() * 1000), int(wizard["offer_id"])))
        offer_id = int(wizard["offer_id"])
        _clear_pending_flow(state)
        await _show_campaign(context, state, offer_id, notice="Campaign updated.")
        raise ApplicationHandlerStop

    if pending.get("kind") == "category_storage":
        target = _forwarded_storage_chat_id(message)
        if target is None and not _is_forwarded_message(message):
            target = _parse_rule_target(getattr(message, "text", None), "JOIN_CHAT")
        if target is None:
            await _edit_panel(
                context,
                state,
                "I couldn't find a storage-channel ID. Forward a message from that channel or send a numeric chat ID.",
                _input_markup(state),
            )
            raise ApplicationHandlerStop
        name = state.get("category_draft_name")
        try:
            category = await CategoryRepository(_database(context)).create(str(name), target)
        except (InvalidRecord, RecordNotFound, ValueError) as exc:
            await _edit_panel(
                context,
                state,
                f"Could not create the category: {exc}. Send a different storage-channel ID or forward its message.",
                _input_markup(state),
            )
            raise ApplicationHandlerStop
        actor_id = int(user.id)
        await _audit(
            _database(context),
            actor_id,
            "category_created",
            str(category.id),
            target_type="category",
        )
        state.pop("category_draft_name", None)
        _clear_pending_flow(state)
        await _show_categories(
            context,
            state,
            int(state.get("page", 0)),
            notice=f"Category {category.name} created. Ensure the bot can post in its storage channel.",
        )
        raise ApplicationHandlerStop

    if pending.get("kind") == "rule_link":
        wizard = state.get("wizard")
        if not isinstance(wizard, dict):
            _clear_pending_flow(state)
            await _edit_panel(context, state, "Admin Control Panel", _home_markup(state))
            return
        try:
            link_url = validate_telegram_link(getattr(message, "text", None))
        except InvalidRecord:
            link_url = None
        if link_url is None:
            await _edit_panel(
                context,
                state,
                "Send a Telegram link such as https://t.me/channelname, or continue without a link.",
                _rule_link_input_markup(state),
            )
            raise ApplicationHandlerStop
        wizard["link_url"] = link_url
        state["input"] = None
        await _show_rule_link(context, state, notice="Telegram link saved.")
        raise ApplicationHandlerStop

    if pending.get("kind") == "rule_target":
        wizard = state.get("wizard")
        if not isinstance(wizard, dict):
            _clear_pending_flow(state)
            await _edit_panel(context, state, "Admin Control Panel", _home_markup(state))
            return
        rule_type = str(wizard.get("type", ""))
        target = _forwarded_target_id(message, rule_type)
        if target is None and not _is_forwarded_message(message):
            target = _parse_rule_target(getattr(message, "text", None), rule_type)
        if target is None:
            if _is_forwarded_message(message):
                error = (
                    "That forwarded message doesn't match this rule type or expose an ID. "
                    "Forward a channel for JOIN_CHAT/REACT, a user for START_BOT, "
                    "or paste a numeric ID."
                )
            else:
                error = "I couldn't find a target ID. Forward a message from the channel/user or paste a numeric ID."
            await _edit_panel(
                context,
                state,
                error,
                _input_markup(state),
            )
            raise ApplicationHandlerStop
        wizard["target"] = target
        wizard["link_url"] = _forwarded_target_url(message, rule_type)
        state["input"] = None
        await _show_rule_link(context, state)
        raise ApplicationHandlerStop

    state["input"] = None
    raise ApplicationHandlerStop


def _clear_pending_flow(state: dict[str, Any]) -> None:
    state["input"] = None
    state["wizard"] = None
    state.pop("category_draft_name", None)
    state.pop("pending_last_activity", None)


def _forwarded_user_id(message: Any) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        if getattr(origin, "type", None) != "user":
            return None
        sender = getattr(origin, "sender_user", None)
        candidate = getattr(sender, "id", None)
        return candidate if _valid_user_id(candidate) else None
    sender = getattr(message, "forward_from", None)
    if sender is not None:
        candidate = getattr(sender, "id", None)
        return candidate if _valid_user_id(candidate) else None
    return None


def _is_forwarded_message(message: Any) -> bool:
    return any(
        getattr(message, field, None) is not None
        for field in ("forward_origin", "forward_from_chat", "forward_from", "forward_sender_name")
    )


def _forwarded_target_id(message: Any, requirement_type: str) -> int | None:
    expected_origins = {"user"} if requirement_type == "START_BOT" else {"channel", "chat"}
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        if getattr(origin, "type", None) not in expected_origins:
            return None
        if requirement_type != "START_BOT":
            candidate = getattr(getattr(origin, "chat", None), "id", None)
            return candidate if _valid_chat_id(candidate) else None
        candidate = getattr(getattr(origin, "sender_user", None), "id", None)
        return candidate if _valid_user_id(candidate) else None
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if forwarded_chat is not None:
        if requirement_type == "START_BOT":
            return None
        candidate = getattr(forwarded_chat, "id", None)
        return candidate if _valid_chat_id(candidate) else None
    forwarded_user = getattr(message, "forward_from", None)
    if forwarded_user is not None:
        if requirement_type != "START_BOT":
            return None
        candidate = getattr(forwarded_user, "id", None)
        return candidate if _valid_user_id(candidate) else None
    return None


def _forwarded_target_url(message: Any, requirement_type: str) -> str | None:
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        origin_type = getattr(origin, "type", None)
        expected = {"user"} if requirement_type == "START_BOT" else {"channel", "chat"}
        if origin_type not in expected:
            return None
        source = (
            getattr(origin, "sender_user", None)
            if requirement_type == "START_BOT"
            else getattr(origin, "chat", None)
        )
        candidate = getattr(source, "username", None)
    elif requirement_type == "START_BOT":
        candidate = getattr(getattr(message, "forward_from", None), "username", None)
    else:
        candidate = getattr(getattr(message, "forward_from_chat", None), "username", None)
    if not isinstance(candidate, str):
        return None
    username = candidate.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) is None:
        return None
    return f"https://t.me/{username}"


def _forwarded_storage_chat_id(message: Any) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        if getattr(origin, "type", None) not in {"channel", "chat"}:
            return None
        candidate = getattr(getattr(origin, "chat", None), "id", None)
        return candidate if _valid_chat_id(candidate) else None
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if forwarded_chat is not None:
        candidate = getattr(forwarded_chat, "id", None)
        return candidate if _valid_chat_id(candidate) else None
    return None


def _parse_user_id(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if _NUMERIC_ID.fullmatch(raw) is None:
        return None
    result = int(raw)
    return result if _valid_user_id(result) else None


def _valid_user_id(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= _MAX_ID
    )


def _valid_chat_id(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value != 0
        and abs(value) <= _MAX_ID
    )


def _parse_rule_target(value: object, requirement_type: str) -> int | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if _SIGNED_NUMERIC_ID.fullmatch(raw) is None:
        return None
    parsed = int(raw)
    if not _valid_chat_id(parsed):
        return None
    if requirement_type == "START_BOT" and parsed < 0:
        return None
    return parsed


async def _dispatch_callback(
    action: str,
    args: list[str],
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
) -> None:
    if action == "close":
        state["closed"] = True
        state["view"] = "closed"
        _clear_pending_flow(state)
        await _edit_panel(context, state, "Admin panel closed.", None, query=query)
    elif action == "home":
        _clear_pending_flow(state)
        state.update({"view": "home"})
        await _edit_panel(context, state, "Admin Control Panel", _home_markup(state), query=query)
    elif action == "categories":
        await _show_categories(context, state, _int_arg(args, 0, 0), query=query)
    elif action == "campaigns":
        await _show_campaigns(context, state, _int_arg(args, 0, 0), query=query)
    elif action == "cmppage":
        await _show_campaigns(context, state, _int_arg(args, 0), query=query)
    elif action == "cmpcreate":
        await _start_campaign_create(context, state, query=query)
    elif action == "cmpitem":
        await _select_campaign_item(context, state, _int_arg(args, 0), query=query)
    elif action == "cmpconfirm":
        await _create_campaign(context, state, query=query)
    elif action == "cmpedit_title":
        await _start_campaign_edit(context, state, _int_arg(args, 0), "title", query=query)
    elif action == "cmpedit_desc":
        await _start_campaign_edit(context, state, _int_arg(args, 0), "description", query=query)
    elif action == "cmp":
        await _show_campaign(context, state, _int_arg(args, 0), query=query)
    elif action == "cmpenable":
        await _toggle_campaign(context, state, _int_arg(args, 0), True, query=query)
    elif action == "cmpdisable":
        await _toggle_campaign(context, state, _int_arg(args, 0), False, query=query)
    elif action == "cmpdelete":
        await _toggle_campaign(context, state, _int_arg(args, 0), False, query=query)
    elif action == "cmpaddchat":
        await _show_campaign_chat_picker(context, state, _int_arg(args, 0), query=query)
    elif action == "cmpnewchat":
        await _start_campaign_chat_input(context, state, _int_arg(args, 0), query=query)
    elif action == "cmpchat_skipname":
        await _campaign_chat_skip_name(context, state, query=query)
    elif action == "cmpchat_skipurl":
        await _campaign_chat_skip_url(context, state, query=query)
    elif action == "cmpchatcancel":
        wizard = state.get("wizard")
        if not isinstance(wizard, dict):
            raise InvalidRecord("channel setup is incomplete")
        await _show_campaign(context, state, int(wizard["offer_id"]), query=query)
    elif action == "cmpchat":
        await _add_campaign_chat(context, state, _int_arg(args, 0), _int_arg(args, 1), query=query)
    elif action == "cmrc":
        await _remove_campaign_chat(context, state, _int_arg(args, 0), _int_arg(args, 1), query=query)
    elif action == "cmpaddreq":
        await _show_campaign_requirement_picker(context, state, _int_arg(args, 0), query=query)
    elif action == "cmpreq":
        await _add_campaign_requirement(context, state, _int_arg(args, 0), _int_arg(args, 1), query=query)
    elif action == "cmrr":
        await _remove_campaign_requirement(context, state, _int_arg(args, 0), _int_arg(args, 1), query=query)
    elif action == "cmpstats":
        await _show_campaign_stats(context, state, _int_arg(args, 0), query=query)
    elif action == "stats":
        await _show_panel_stats(context, state, query=query)
    elif action == "catpage":
        await _show_categories(context, state, _int_arg(args, 0), query=query)
    elif action == "catadd":
        await _start_category_input(context, state, query=query)
    elif action == "cat":
        await _show_category(context, state, _int_arg(args, 0), query=query)
    elif action == "catlink":
        await _show_link_choices(context, state, _int_arg(args, 0), query=query)
    elif action == "catlinkdo":
        await _link_category(
            context, state, _int_arg(args, 0), _int_arg(args, 1), query=query
        )
    elif action == "catdelete":
        await _show_category_delete(context, state, _int_arg(args, 0), query=query)
    elif action == "catdeletedo":
        await _delete_category(
            context, state, _int_arg(args, 0), _str_arg(args, 1), query=query
        )
    elif action == "requirements":
        await _show_requirement_scopes(context, state, _int_arg(args, 0, 0), query=query)
    elif action == "reqpage":
        await _show_requirement_scopes(context, state, _int_arg(args, 0), query=query)
    elif action == "reqscope":
        await _show_requirements(context, state, _scope_from_arg(args, 0), query=query)
    elif action == "reqadd":
        await _start_requirement_input(context, state, _scope_from_arg(args, 0), query=query)
    elif action == "reqdelconfirm":
        await _begin_requirement_delete(
            context,
            state,
            _scope_from_arg(args, 0),
            _int_arg(args, 1),
            query=query,
        )
    elif action == "reqdeletedo":
        await _delete_requirement(
            context,
            state,
            _scope_from_arg(args, 0),
            _int_arg(args, 1),
            _str_arg(args, 2),
            query=query,
        )
    elif action == "rule_type":
        await _select_rule_type(context, state, _str_arg(args, 0), query=query)
    elif action == "rule_link_edit":
        await _start_rule_link_input(context, state, query=query)
    elif action == "rule_link_continue":
        state["input"] = None
        await _show_rule_verification(context, state, query=query)
    elif action == "rule_link_skip":
        _require_rule_wizard(state)["link_url"] = None
        state["input"] = None
        await _show_rule_verification(context, state, query=query)
    elif action == "rule_link_remove":
        _require_rule_wizard(state)["link_url"] = None
        await _show_rule_link(context, state, query=query, notice="Telegram link removed.")
    elif action == "rule_verify":
        await _select_rule_verification(context, state, _str_arg(args, 0), query=query)
    elif action == "rule_continue":
        await _show_rule_wait(context, state, query=query)
    elif action == "rule_fake":
        wizard = _require_rule_wizard(state)
        wizard["is_fake"] = True
        await _show_rule_wait(context, state, query=query)
    elif action == "rule_wait":
        await _set_rule_wait(context, state, _int_arg(args, 0), query=query)
    elif action == "rule_wait_done":
        wizard = _require_rule_wizard(state)
        if wizard.get("type") == "REACT":
            await _show_react_windows(context, state, query=query)
        else:
            await _show_rule_confirmation(context, state, query=query)
    elif action == "rule_message_window":
        await _set_react_window(context, state, "last_x_messages", _int_arg(args, 0), query=query)
    elif action == "rule_day_window":
        await _set_react_window(context, state, "last_x_days", _int_arg(args, 0), query=query)
    elif action == "rule_windows_done":
        await _show_rule_confirmation(context, state, query=query)
    elif action == "rule_save":
        await _save_rule(context, state, query=query)
    elif action == "rule_cancel":
        scope_id = state.get("scope_id")
        _clear_pending_flow(state)
        await _show_requirements(context, state, scope_id, query=query, notice="Rule creation cancelled.")
    elif action == "staff":
        await _show_staff_menu(context, state, query=query)
    elif action == "staffrole":
        await _show_staff(context, state, _str_arg(args, 0), _int_arg(args, 1, 0), query=query)
    elif action == "staffpage":
        await _show_staff(context, state, _str_arg(args, 0), _int_arg(args, 1), query=query)
    elif action == "staffadd":
        await _start_staff_input(context, state, _str_arg(args, 0), query=query)
    elif action == "staffremove":
        await _remove_staff(
            context, state, _str_arg(args, 0), _int_arg(args, 1), query=query
        )
    elif action == "settings":
        await _show_settings(context, state, query=query)
    elif action == "lock":
        await _set_lockdown(context, state, True, query=query)
    elif action == "unlock":
        await _set_lockdown(context, state, False, query=query)
    elif action == "realpolicy_set":
        await _set_real_check_policy(
            context, state, _str_arg(args, 0), query=query
        )
    elif action == "inputcancel":
        was_category_input = str(state.get("view", "")).startswith("input_category_")
        state.pop("category_draft_name", None)
        _clear_pending_flow(state)
        if was_category_input:
            await _show_categories(context, state, int(state.get("page", 0)), query=query, notice="Category creation cancelled.")
        elif state.get("view") == "input_staff_admin":
            await _show_staff(context, state, "admins", int(state.get("page", 0)), query=query)
        elif state.get("view") == "input_staff_uploader":
            await _show_staff(context, state, "uploaders", int(state.get("page", 0)), query=query)
        else:
            await _show_requirements(context, state, state.get("scope_id"), query=query)
    else:
        await _edit_panel(context, state, "Unknown panel action.", _back_home(state), query=query)


def _back_home(state: dict[str, Any]) -> InlineKeyboardMarkup:
    return _markup([[_button("Main Menu", state, "home")]])


def _page_row(state: dict[str, Any], action: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    return [
        _button("‹", state, action, max(0, page - 1)),
        _button(f"{page + 1}/{pages}", state, action, page),
        _button("›", state, action, min(pages - 1, page + 1)),
    ]


async def _show_categories(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    page: int,
    *,
    query: Any = None,
    notice: str = "",
) -> None:
    categories = await CategoryRepository(_database(context)).list()
    pages = max(1, math.ceil(len(categories) / _PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    state.update({"view": "categories", "page": page, "input": None})
    visible = categories[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
    rows = [[_button(category.name[:64], state, "cat", category.id)] for category in visible]
    if pages > 1:
        rows.append(_page_row(state, "catpage", page, pages))
    rows.append([_button("Add Category", state, "catadd")])
    rows.append([_button("Main Menu", state, "home")])
    text = f"Categories — {page + 1}/{pages}"
    if notice:
        text += f"\n\n{notice}"
    if not visible:
        text += "\nNo categories found."
    await _edit_panel(context, state, text, _markup(rows), query=query)


async def _start_category_input(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    state.pop("category_draft_name", None)
    state.update({
        "view": "input_category_name",
        "input": {"kind": "category_name"},
        "wizard": None,
    })
    _touch_pending_flow(state)
    await _edit_panel(
        context,
        state,
        "Send a category name (up to 80 characters). You will then choose its storage channel.",
        _input_markup(state),
        query=query,
    )


async def _show_category(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    category_id: int,
    *,
    query: Any = None,
    notice: str = "",
) -> None:
    repository = CategoryRepository(_database(context))
    category = await repository.get(category_id)
    if category is None:
        await _show_categories(context, state, 0, query=query, notice="Category no longer exists.")
        return
    parents, children = await repository.links_for(category_id)
    state.update({"view": "category_detail", "category_id": category_id, "input": None})
    text = (
        f"Category: {category.name}\nID: {category.id}\n"
        f"Storage channel: {category.storage_channel_id}\n"
        f"Parents: {', '.join(item.name for item in parents) or 'None'}\n"
        f"Children: {', '.join(item.name for item in children) or 'None'}"
    )
    if notice:
        text += f"\n\n{notice}"
    rows = [[_button("Link to Child", state, "catlink", category.id)]]
    if category.name.casefold() != "default":
        rows.append([_button("Delete", state, "catdelete", category.id)])
    rows.append([_button("Back to Categories", state, "categories", int(state.get("page", 0)))])
    await _edit_panel(context, state, text, _markup(rows), query=query)


async def _show_link_choices(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    parent_id: int,
    *,
    query: Any = None,
) -> None:
    repository = CategoryRepository(_database(context))
    parent = await repository.get(parent_id)
    if parent is None:
        await _show_categories(context, state, 0, query=query, notice="Category no longer exists.")
        return
    _, children = await repository.links_for(parent_id)
    existing = {child.id for child in children}
    candidates = [category for category in await repository.list() if category.id != parent_id and category.id not in existing]
    state.update({"view": "category_link", "category_id": parent_id, "input": None})
    rows = [[_button(f"Link {child.name}"[:64], state, "catlinkdo", parent_id, child.id)] for child in candidates]
    rows.append([_button("Back", state, "cat", parent_id)])
    await _edit_panel(
        context,
        state,
        f"Choose a child category to link under {parent.name}. Cycles are rejected.",
        _markup(rows),
        query=query,
    )


async def _link_category(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    parent_id: int,
    child_id: int,
    *,
    query: Any = None,
) -> None:
    repository = CategoryRepository(_database(context))
    parent = await repository.get(parent_id)
    child = await repository.get(child_id)
    if parent is None or child is None:
        await _show_category(context, state, parent_id, query=query, notice="A category no longer exists.")
        return
    await repository.link(parent.name, child.name)
    await _audit(
        _database(context),
        int(state["owner_user_id"]),
        "category_linked",
        f"{parent_id}->{child_id}",
        target_type="category",
    )
    await _show_category(context, state, parent_id, query=query, notice=f"Linked {child.name} as a child.")



async def _show_category_delete(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    category_id: int,
    *,
    query: Any = None,
) -> None:
    category = await CategoryRepository(_database(context)).get(category_id)
    if category is None or category.name.casefold() == "default":
        await _show_category(context, state, category_id, query=query, notice="Default cannot be deleted.")
        return
    state.update({"view": "category_delete", "category_id": category_id})
    rows = [
        [_button("Delete orphaned requirements", state, "catdeletedo", category_id, "delete")],
        [_button("Keep orphaned requirements", state, "catdeletedo", category_id, "keep")],
        [_button("Cancel", state, "cat", category_id)],
    ]
    await _edit_panel(
        context,
        state,
        f"Delete {category.name}? Its items will move to Default; the items and stored media will remain. All generated share links for those items will be revoked. Parent/child links to this category will be removed, but linked categories and their storage channels will remain. Choose whether to delete or keep requirements that become orphaned.",
        _markup(rows),
        query=query,
    )


async def _delete_category(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    category_id: int,
    choice: str,
    *,
    query: Any = None,
) -> None:
    if choice not in {"delete", "keep"}:
        await _show_category(context, state, category_id, query=query)
        return
    category = await CategoryRepository(_database(context)).get(category_id)
    if category is None:
        await _show_categories(context, state, 0, query=query, notice="Category no longer exists.")
        return
    await CategoryRepository(_database(context)).delete(
        category.name,
        delete_orphaned_requirements=(choice == "delete"),
    )
    actor_id = int(state["owner_user_id"])
    await _audit(
        _database(context),
        actor_id,
        "category_deleted",
        f"{category_id}:orphaned={choice}",
        target_type="category",
    )
    await _show_categories(
        context,
        state,
        int(state.get("page", 0)),
        query=query,
        notice=(
            f"{category.name} deleted; items and stored media were retained and "
            "reassigned to Default. Share links for those items were revoked; "
            "linked categories were kept."
        ),
    )


async def _show_requirement_scopes(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    page: int,
    *,
    query: Any = None,
) -> None:
    categories = await CategoryRepository(_database(context)).list()
    pages = max(1, math.ceil((len(categories) + 1) / _PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    scopes: list[tuple[str, int | None]] = [("Global", None)] + [(item.name, item.id) for item in categories]
    visible = scopes[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
    state.update({"view": "requirement_scopes", "page": page, "input": None})
    rows = [[_button(name[:64], state, "reqscope", "g" if category_id is None else category_id)] for name, category_id in visible]
    if pages > 1:
        rows.append(_page_row(state, "reqpage", page, pages))
    rows.append([_button("Main Menu", state, "home")])
    await _edit_panel(context, state, f"Requirements — select a scope ({page + 1}/{pages})", _markup(rows), query=query)


async def _show_requirements(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    category_id: int | None,
    *,
    query: Any = None,
    notice: str = "",
) -> None:
    scope_name = "Global"
    if category_id is not None:
        category = await CategoryRepository(_database(context)).get(category_id)
        if category is None:
            await _show_requirement_scopes(context, state, 0, query=query)
            return
        scope_name = category.name
    requirements = await RequirementRepository(_database(context)).list_for_scope(category_id)
    state.update({"view": "requirements_list", "scope_id": category_id, "scope_name": scope_name, "input": None})
    rows = []
    lines = [f"Requirements attached directly to {scope_name}:"]
    if not requirements:
        lines.append("None. Global rules and inherited child rules still apply where configured.")
    for requirement in requirements:
        target = "—" if requirement.target is None else requirement.target
        link_note = " | prompt link" if requirement.settings.get("link_url") else ""
        lines.append(f"#{requirement.id} {requirement.type} | {target} | wait {requirement.settings.get('wait_seconds', 0)}s{link_note}")
        rows.append([_button(f"Delete rule #{requirement.id}", state, "reqdelconfirm", "g" if category_id is None else category_id, requirement.id)])
    if notice:
        lines.append(f"\n{notice}")
    rows.append([_button("Add Rule", state, "reqadd", "g" if category_id is None else category_id)])
    rows.append([_button("Back to Scopes", state, "requirements", 0)])
    await _edit_panel(context, state, "\n".join(lines), _markup(rows), query=query)


async def _start_requirement_input(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    category_id: int | None,
    *,
    query: Any = None,
) -> None:
    category_name = "Global"
    if category_id is not None:
        category = await CategoryRepository(_database(context)).get(category_id)
        if category is None:
            await _show_requirement_scopes(context, state, 0, query=query)
            return
        category_name = category.name
    state.update({
        "view": "rule_wizard_type",
        "scope_id": category_id,
        "scope_name": category_name,
        "input": None,
        "wizard": {"category_id": category_id, "scope_name": category_name, "step": "type"},
    })
    _touch_pending_flow(state)
    rows = [
        [_button("Join a Channel", state, "rule_type", "JOIN_CHAT")],
        [_button("React to Posts", state, "rule_type", "REACT")],
        [_button("Start a Bot", state, "rule_type", "START_BOT")],
        [_button("Timed Wait", state, "rule_type", "TIMED_WAIT")],
        [_button("Cancel", state, "rule_cancel")],
    ]
    await _edit_panel(
        context,
        state,
        f"Add a rule to {category_name}. Choose its type:",
        _markup(rows),
        query=query,
    )


def _input_markup(state: dict[str, Any]) -> InlineKeyboardMarkup:
    wizard = state.get("wizard")
    action = "cmpchatcancel" if isinstance(wizard, dict) and wizard.get("chat_step") else "rule_cancel" if isinstance(wizard, dict) else "inputcancel"
    return _markup([[_button("Cancel", state, action)]])


def _rule_link_input_markup(state: dict[str, Any]) -> InlineKeyboardMarkup:
    return _markup([
        [_button("Continue without link", state, "rule_link_skip")],
        [_button("Cancel", state, "rule_cancel")],
    ])


async def _show_rule_link(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
    notice: str = "",
) -> None:
    wizard = _require_rule_wizard(state)
    link_url = wizard.get("link_url")
    wizard["step"] = "link"
    state.update({"view": "rule_wizard_link", "input": None})
    _touch_pending_flow(state)
    rows: list[list[InlineKeyboardButton]] = []
    if isinstance(link_url, str):
        rows.extend([
            [_button("Continue", state, "rule_link_continue")],
            [_button("Change Link", state, "rule_link_edit")],
            [_button("Remove Link", state, "rule_link_remove")],
        ])
        link_text = f"Prompt link: {link_url}"
    else:
        rows.extend([
            [_button("Add Telegram Link", state, "rule_link_edit")],
            [_button("Continue without link", state, "rule_link_continue")],
        ])
        link_text = "Prompt link: none"
    rows.append([_button("Cancel", state, "rule_cancel")])
    lines = [
        "Optional Telegram link for the user-facing prompt.",
        "Opening a link does not prove membership or a reaction.",
        link_text,
    ]
    if notice:
        lines.append(notice)
    await _edit_panel(context, state, "\n".join(lines), _markup(rows), query=query)


async def _start_rule_link_input(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    wizard = _require_rule_wizard(state)
    wizard["step"] = "link_input"
    state.update({"view": "rule_wizard_link_input", "input": {"kind": "rule_link"}})
    _touch_pending_flow(state)
    await _edit_panel(
        context,
        state,
        "Send a Telegram HTTPS link such as https://t.me/channelname.",
        _rule_link_input_markup(state),
        query=query,
    )


def _require_rule_wizard(state: dict[str, Any]) -> dict[str, Any]:
    wizard = state.get("wizard")
    if not isinstance(wizard, dict):
        raise ValueError("The rule wizard has expired")
    return wizard


async def _select_rule_type(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    rule_type: str,
    *,
    query: Any = None,
) -> None:
    wizard = _require_rule_wizard(state)
    if rule_type not in {"JOIN_CHAT", "REACT", "START_BOT", "TIMED_WAIT"}:
        raise ValueError("Unknown rule type")
    wizard.update({"type": rule_type, "step": "target"})
    if rule_type == "TIMED_WAIT":
        wizard["target"] = None
        wizard["is_fake"] = True
        state["input"] = None
        await _show_rule_wait(context, state, query=query)
        return
    state.update({"view": "rule_wizard_target", "input": {"kind": "rule_target"}})
    _touch_pending_flow(state)
    if rule_type in {"JOIN_CHAT", "REACT"}:
        prompt = "Forward a message from the target channel or group, or paste its numeric chat ID."
    else:
        prompt = "Forward a message from the target user, or paste the user's numeric ID."
    await _edit_panel(context, state, prompt, _input_markup(state), query=query)


async def _show_rule_verification(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    wizard = _require_rule_wizard(state)
    rule_type = str(wizard.get("type"))
    wizard["step"] = "verification"
    state.update({"view": "rule_wizard_verification", "input": None})
    rows: list[list[InlineKeyboardButton]] = []
    if rule_type == "START_BOT":
        rows.append([_button("Fake (Prompt + Wait)", state, "rule_verify", "fake")])
        text = "Real START_BOT verification is not supported. Choose the prompt-and-wait pass."
    else:
        rows.append([
            _button("Real", state, "rule_verify", "real"),
            _button("Fake", state, "rule_verify", "fake"),
        ])
        text = "Choose how this rule is handled: Real checks eligibility; Fake shows a prompt, then passes after the selected wait."
    rows.append([_button("Cancel", state, "rule_cancel")])
    await _edit_panel(context, state, text, _markup(rows), query=query)


async def _select_rule_verification(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    mode: str,
    *,
    query: Any = None,
) -> None:
    wizard = _require_rule_wizard(state)
    if mode not in {"real", "fake"}:
        raise ValueError("Unknown verification mode")
    rule_type = str(wizard.get("type"))
    if rule_type == "START_BOT" and mode == "real":
        raise InvalidRecord("Real START_BOT verification is not supported")
    wizard["is_fake"] = mode == "fake"
    if rule_type == "JOIN_CHAT" and mode == "real":
        target = wizard.get("target")
        if not isinstance(target, int):
            raise InvalidRecord("JOIN_CHAT target is missing")
        bot_admin_status = await _bot_is_chat_admin(context, target)
        if bot_admin_status is False or bot_admin_status is None:
            wizard["step"] = "join_warning"
            state["view"] = "rule_wizard_admin_warning"
            rows = [
                [_button("Continue Anyway", state, "rule_continue")],
                [_button("Use Fake", state, "rule_fake")],
                [_button("Cancel", state, "rule_cancel")],
            ]
            await _edit_panel(
                context,
                state,
                (
                    "⚠️ Bot is not an admin in this channel. Real checks may fail."
                    if bot_admin_status is False
                    else "⚠️ Could not verify whether the bot is an admin. Telegram lookup failed; check permissions and retry."
                ),
                _markup(rows),
                query=query,
            )
            return
    await _show_rule_wait(context, state, query=query)


async def _bot_is_chat_admin(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> bool | None:
    bot = context.bot
    bot_id = getattr(bot, "id", None)
    try:
        if not isinstance(bot_id, int) or bot_id <= 0:
            bot_id = int((await bot.get_me()).id)
        member = await bot.get_chat_member(chat_id=chat_id, user_id=bot_id)
    except Exception:
        return None
    return getattr(member, "status", None) in {"administrator", "creator"}


async def _show_rule_wait(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    wizard = _require_rule_wizard(state)
    wait_seconds = int(wizard.get("wait_seconds", 0))
    wizard.update({"step": "wait", "wait_seconds": wait_seconds})
    state.update({"view": "rule_wizard_wait", "input": None})
    options = (0, 3, 5, 10, 30, 60)
    rows = [
        [_button(f"{'✓ ' if value == wait_seconds else ''}{value}s", state, "rule_wait", value)
         for value in options[:4]],
        [_button(f"{'✓ ' if value == wait_seconds else ''}{value}s", state, "rule_wait", value)
         for value in options[4:]],
        [_button("Continue", state, "rule_wait_done")],
        [_button("Cancel", state, "rule_cancel")],
    ]
    await _edit_panel(context, state, "Choose the wait time before delivery:", _markup(rows), query=query)


async def _set_rule_wait(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    wait_seconds: int,
    *,
    query: Any = None,
) -> None:
    if wait_seconds not in {0, 3, 5, 10, 30, 60}:
        raise ValueError("Unsupported wait-time option")
    _require_rule_wizard(state)["wait_seconds"] = wait_seconds
    await _show_rule_wait(context, state, query=query)


async def _show_react_windows(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    wizard = _require_rule_wizard(state)
    wizard.setdefault("last_x_messages", 5)
    wizard.setdefault("last_x_days", 0)
    wizard["step"] = "react_windows"
    state.update({"view": "rule_wizard_react_windows", "input": None})
    messages = (0, 3, 5, 10)
    days = (0, 7, 30)
    rows = [
        [_button(f"{'✓ ' if wizard['last_x_messages'] == value else ''}{'No message limit' if value == 0 else f'Last {value} posts'}",
                 state, "rule_message_window", value) for value in messages],
        [_button(f"{'✓ ' if wizard['last_x_days'] == value else ''}Days {value}",
                 state, "rule_day_window", value) for value in days],
        [_button("Continue", state, "rule_windows_done")],
        [_button("Cancel", state, "rule_cancel")],
    ]
    await _edit_panel(
        context,
        state,
        "Require at least one observed reaction. Set No message limit and/or Days 0 to disable that bound.",
        _markup(rows),
        query=query,
    )


async def _set_react_window(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    setting: str,
    value: int,
    *,
    query: Any = None,
) -> None:
    allowed = {
        "last_x_messages": {0, 3, 5, 10},
        "last_x_days": {0, 7, 30},
    }
    if setting not in allowed or value not in allowed[setting]:
        raise ValueError("Unsupported REACT window")
    _require_rule_wizard(state)[setting] = value
    await _show_react_windows(context, state, query=query)


async def _show_rule_confirmation(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    wizard = _require_rule_wizard(state)
    rule_type = str(wizard.get("type"))
    target = wizard.get("target")
    if rule_type == "TIMED_WAIT":
        verification = "Wait only (no eligibility check)"
    else:
        verification = "Fake (prompt + wait)" if wizard.get("is_fake") else "Real"
    lines = [
        "Confirm rule",
        f"Type: {rule_type}",
        f"Target: {target if target is not None else 'Not applicable'}",
        f"Verification: {verification}",
        f"Wait: {wizard.get('wait_seconds', 0)} seconds",
        f"Prompt link: {wizard.get('link_url') or 'None'}",
    ]
    if rule_type == "REACT":
        message_window = (
            "any observed post" if int(wizard.get("last_x_messages", 5)) == 0
            else f"last {wizard.get('last_x_messages', 5)} posts"
        )
        day_window = (
            "any age" if int(wizard.get("last_x_days", 0)) == 0
            else f"within {wizard.get('last_x_days', 0)} days"
        )
        lines.append(
            f"Window: {message_window} / {day_window}"
        )
    wizard["step"] = "confirm"
    state.update({"view": "rule_wizard_confirm", "input": None})
    rows = [
        [_button("Save Rule", state, "rule_save")],
        [_button("Cancel", state, "rule_cancel")],
    ]
    await _edit_panel(context, state, "\n".join(lines), _markup(rows), query=query)


async def _save_rule(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    wizard = _require_rule_wizard(state)
    rule_type = str(wizard["type"])
    created = await RequirementRepository(_database(context)).create(
        category_id=wizard.get("category_id"),
        requirement_type=rule_type,
        target=wizard.get("target"),
        is_fake=bool(wizard.get("is_fake")),
        wait_seconds=int(wizard.get("wait_seconds", 0)),
        last_x_messages=int(wizard.get("last_x_messages", 5)) if rule_type == "REACT" else None,
        last_x_days=int(wizard.get("last_x_days", 0)) if rule_type == "REACT" else None,
        link_url=wizard.get("link_url") if isinstance(wizard.get("link_url"), str) else None,
    )
    category_id = wizard.get("category_id")
    scope_name = str(wizard.get("scope_name", "Global"))
    await _audit(
        _database(context),
        int(state["owner_user_id"]),
        "requirement_created",
        str(created.id),
        target_type="requirement",
    )
    _clear_pending_flow(state)
    await _show_requirements(
        context,
        state,
        category_id,
        query=query,
        notice=f"Requirement {created.id} added to {scope_name}.",
    )


async def _begin_requirement_delete(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    category_id: int | None,
    requirement_id: int,
    *,
    query: Any = None,
) -> None:
    repository = RequirementRepository(_database(context))
    requirement = await repository.get(requirement_id)
    if requirement is None:
        await _show_requirements(context, state, category_id, query=query, notice="Requirement no longer exists.")
        return
    scopes = await repository.scopes_for(requirement_id)
    current_scope = "Global"
    if category_id is not None:
        category = await CategoryRepository(_database(context)).get(category_id)
        current_scope = category.name if category is not None else f"Category {category_id}"
    if current_scope not in scopes:
        await _show_requirements(
            context,
            state,
            category_id,
            query=query,
            notice="This rule is no longer attached to the selected scope.",
        )
        return
    scope_token = "g" if category_id is None else category_id
    if len(scopes) == 1:
        await _delete_requirement(context, state, category_id, requirement_id, "unlink", query=query)
        return
    other_scopes = [scope for scope in scopes if scope != current_scope]
    state.update({"view": "requirement_shared_delete", "scope_id": category_id})
    rows = [
        [_button("Unlink Here", state, "reqdeletedo", scope_token, requirement_id, "unlink")],
        [_button("Delete Everywhere", state, "reqdeletedo", scope_token, requirement_id, "all")],
        [_button("Cancel", state, "reqdeletedo", scope_token, requirement_id, "cancel")],
    ]
    await _edit_panel(
        context,
        state,
        f"Rule #{requirement_id} is also used in {', '.join(other_scopes)}.\n"
        "Unlink it only here, or delete the rule from every scope?",
        _markup(rows),
        query=query,
    )


async def _delete_requirement(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    category_id: int | None,
    requirement_id: int,
    choice: str,
    *,
    query: Any = None,
) -> None:
    if choice == "unlink":
        removed = await RequirementRepository(_database(context)).remove_from_scope(
            category_id=category_id,
            requirement_id=requirement_id,
        )
        if removed:
            await _audit(
                _database(context),
                int(state["owner_user_id"]),
                "requirement_unlinked",
                str(requirement_id),
                target_type="requirement",
            )
        notice = "Requirement unlinked from this scope." if removed else "Rule was not attached to this scope."
    elif choice == "all":
        removed = await RequirementRepository(_database(context)).remove_everywhere(requirement_id)
        if removed:
            await _audit(
                _database(context),
                int(state["owner_user_id"]),
                "requirement_deleted_everywhere",
                str(requirement_id),
                target_type="requirement",
            )
        notice = "Requirement deleted everywhere." if removed else "Requirement no longer exists."
    elif choice == "cancel":
        notice = "Deletion cancelled."
    else:
        raise ValueError("Unknown requirement deletion choice")
    await _show_requirements(
        context,
        state,
        category_id,
        query=query,
        notice=notice,
    )


async def _show_staff_menu(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    state.update({"view": "staff_menu", "input": None})
    rows = [
        [_button("Admins", state, "staffrole", "admins", 0), _button("Uploaders", state, "staffrole", "uploaders", 0)],
        [_button("Main Menu", state, "home")],
    ]
    await _edit_panel(context, state, "Staff", _markup(rows), query=query)


async def _show_staff(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    role: str,
    page: int,
    *,
    query: Any = None,
    notice: str = "",
) -> None:
    if role not in {"admins", "uploaders"}:
        raise ValueError("Unknown staff role")
    state.update({"view": f"staff_{role}", "role": role, "page": page, "input": None})
    settings = _settings(context)
    if role == "admins":
        grants = await AdminRepository(_database(context)).list_all()
        by_id = {grant.user_id: grant for grant in grants}
        for user_id in settings.admin_ids:
            by_id.setdefault(user_id, None)
        values: list[tuple[int, bool]] = [
            (user_id, user_id in settings.admin_ids) for user_id in sorted(by_id)
        ]
        total = len(values)
        pages = max(1, math.ceil(total / _PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        visible = values[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
        lines = [f"Admins — {page + 1}/{pages}"]
        rows: list[list[InlineKeyboardButton]] = []
        for user_id, super_admin in visible:
            lines.append(f"{user_id} — {'super-admin (.env)' if super_admin else 'database admin'}")
            if not super_admin:
                rows.append([_button(f"Remove {user_id}", state, "staffremove", "admins", user_id)])
    else:
        uploader_repo = UploaderRepository(_database(context))
        total, _ = await uploader_repo.list_active_page(offset=0, limit=_PAGE_SIZE)
        pages = max(1, math.ceil(total / _PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        total, grants = await uploader_repo.list_active_page(
            offset=page * _PAGE_SIZE, limit=_PAGE_SIZE
        )
        visible = [(grant.user_id, False) for grant in grants]
        lines = [f"Uploaders — {page + 1}/{pages}"]
        rows = []
        for user_id, _ in visible:
            lines.append(str(user_id))
            rows.append([_button(f"Remove {user_id}", state, "staffremove", "uploaders", user_id)])
    state["page"] = page
    if not visible:
        lines.append("No entries.")
    if notice:
        lines.append(f"\n{notice}")
    if pages > 1:
        rows.append(_page_row(state, "staffpage", page, pages))
    add_label = "Add Admin" if role == "admins" else "Add Uploader"
    rows.append([_button(add_label, state, "staffadd", "admins" if role == "admins" else "uploaders")])
    rows.append([_button("Back to Staff", state, "staff")])
    await _edit_panel(context, state, "\n".join(lines), _markup(rows), query=query)


async def _start_staff_input(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    role: str,
    *,
    query: Any = None,
) -> None:
    if role not in {"admins", "uploaders"}:
        raise ValueError("Unknown staff role")
    view = "input_staff_admin" if role == "admins" else "input_staff_uploader"
    label = "administrator" if role == "admins" else "uploader"
    state.update({"view": view, "role": role, "input": {"kind": "add_admin" if role == "admins" else "add_uploader"}})
    _touch_pending_flow(state)
    await _edit_panel(
        context,
        state,
        f"Please send the numeric Telegram ID of the {label}, or forward a message from that user.",
        _input_markup(state),
        query=query,
    )


async def _remove_staff(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    role: str,
    user_id: int,
    *,
    query: Any = None,
) -> None:
    actor_id = int(state["owner_user_id"])
    settings = _settings(context)
    if role == "admins":
        if user_id in settings.admin_ids:
            await _show_staff(context, state, "admins", int(state.get("page", 0)), query=query, notice="Cannot revoke Environment Super-Admins.")
            return
        changed = await AdminRepository(_database(context)).remove(user_id=user_id, removed_by=actor_id)
        notice = "Administrator removed." if changed else "No database admin role found."
        await _show_staff(context, state, "admins", int(state.get("page", 0)), query=query, notice=notice)
    elif role == "uploaders":
        if await is_admin(user_id, _database(context)):
            await _show_staff(context, state, "uploaders", int(state.get("page", 0)), query=query, notice="An administrator's uploader access cannot be revoked.")
            return
        changed = await UploaderRepository(_database(context)).revoke(
            user_id=user_id,
            revoked_by=actor_id,
        )
        notice = "Uploader removed." if changed else "No active uploader grant found."
        await _show_staff(context, state, "uploaders", int(state.get("page", 0)), query=query, notice=notice)
    else:
        raise ValueError("Unknown staff role")


async def _show_settings(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    *,
    query: Any = None,
) -> None:
    settings_repo = BotSettingsRepository(_database(context))
    enabled = await settings_repo.user_commands_enabled()
    locked = not enabled
    real_policy = await settings_repo.real_check_failure_policy()
    state.update({"view": "settings", "input": None})
    target_action = "unlock" if locked else "lock"
    markup = _markup([
        [_button(f"Lock Bot ({'ON' if locked else 'OFF'})", state, target_action)],
        [_button(
            f"Real-check errors: {'Deny' if real_policy == BotSettingsRepository.REAL_CHECK_FAIL_CLOSED else 'Wait fallback'}",
            state,
            "realpolicy_set",
            BotSettingsRepository.REAL_CHECK_WAIT_FALLBACK
            if real_policy == BotSettingsRepository.REAL_CHECK_FAIL_CLOSED
            else BotSettingsRepository.REAL_CHECK_FAIL_CLOSED,
        )],
        [_button("Main Menu", state, "home"), _button("Close Menu", state, "close")],
    ])
    await _edit_panel(
        context,
        state,
        f"Settings — Access Control\n"
        f"Regular-user command/upload lock: {'ON' if locked else 'OFF'}\n\n"
        "When ON, regular users can use only /start and cannot upload media. "
        "Admins and approved Uploaders are unaffected.\n\n"
        f"Real JOIN_CHAT/REACT check unavailable: "
        f"{'deny without using a share link' if real_policy == BotSettingsRepository.REAL_CHECK_FAIL_CLOSED else 'use the rule’s configured wait'}.",
        markup,
        query=query,
    )


async def _set_real_check_policy(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    policy: str,
    *,
    query: Any = None,
) -> None:
    changed = await BotSettingsRepository(_database(context)).set_real_check_failure_policy(policy)
    if changed:
        await _audit(
            _database(context),
            int(state["owner_user_id"]),
            "real_check_failure_policy_changed",
            policy,
            target_type="bot_setting",
        )
    await _show_settings(context, state, query=query)


async def _set_lockdown(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    locked: bool,
    *,
    query: Any = None,
) -> None:
    changed = await BotSettingsRepository(_database(context)).set_user_commands_enabled(not locked)
    if changed:
        actor_id = int(state["owner_user_id"])
        await _audit(
            _database(context),
            actor_id,
            "user_commands_disabled" if locked else "user_commands_enabled",
            BotSettingsRepository.COMMANDS_KEY,
            target_type="bot_setting",
        )
    await _show_settings(context, state, query=query)


async def _edit_panel(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    query: Any = None,
) -> None:
    try:
        if query is not None:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
        else:
            await context.bot.edit_message_text(
                chat_id=int(state["chat_id"]),
                message_id=int(state["message_id"]),
                text=text,
                reply_markup=reply_markup,
            )
    except Exception as exc:
        logger.warning("admin_panel_edit_failed (%s)", type(exc).__name__)
        state["nonce"] = secrets.token_hex(4)
        state.update({"view": "home", "closed": False})
        _clear_pending_flow(state)
        try:
            sent = await context.bot.send_message(
                chat_id=int(state["chat_id"]),
                text=(
                    "The previous panel could not be updated, so this is a fresh panel. "
                    "The last action may already have been applied; review before repeating it."
                ),
                reply_markup=_home_markup(state),
            )
            state["message_id"] = int(sent.message_id)
        except Exception as recovery_exc:
            state["closed"] = True
            logger.error(
                "admin_panel_recovery_failed (%s)", type(recovery_exc).__name__
            )


async def _audit(
    database: Database,
    actor_id: int,
    action: str,
    target_id: str,
    *,
    target_type: str,
) -> None:
    try:
        await AuditLogRepository(database).record_event(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
        )
    except Exception as exc:
        logger.error("admin_panel_audit_failed (%s)", type(exc).__name__)


async def _audit_denial(database: Database, actor_id: int, target: str) -> None:
    try:
        await AuditLogRepository(database).record_admin_denied(
            actor_id=actor_id,
            target_id=target,
        )
    except Exception as exc:
        logger.error("admin_panel_denial_audit_failed (%s)", type(exc).__name__)


def _int_arg(args: list[str], index: int, default: int | None = None) -> int:
    if index >= len(args):
        if default is not None:
            return default
        raise ValueError("Missing numeric callback argument")
    raw = args[index]
    if _SIGNED_NUMERIC_ID.fullmatch(raw) is None:
        raise ValueError("Invalid numeric callback argument")
    parsed = int(raw)
    if parsed < -_MAX_ID or parsed > _MAX_ID:
        raise ValueError("Numeric callback argument is out of range")
    return parsed


def _str_arg(args: list[str], index: int) -> str:
    if index >= len(args):
        raise ValueError("Missing callback argument")
    return args[index]


def _scope_from_arg(args: list[str], index: int) -> int | None:
    value = _str_arg(args, index)
    if value == "g":
        return None
    if _NUMERIC_ID.fullmatch(value) is None:
        raise ValueError("Invalid requirement scope")
    parsed = int(value)
    if parsed <= 0 or parsed > _MAX_ID:
        raise ValueError("Invalid requirement scope")
    return parsed


async def _all_offers(database: Database) -> tuple[Offer, ...]:
    async with database.connection() as connection:
        rows = await (await connection.execute("SELECT * FROM offers ORDER BY updated_at_ms DESC, id DESC")).fetchall()
    return tuple(__import__("media_bot.db.repositories", fromlist=["_offer_from_row"])._offer_from_row(row) for row in rows)


async def _show_campaigns(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], page: int = 0, *, query: Any = None, notice: str | None = None) -> None:
    offers = await _all_offers(_database(context)); page = max(0, page); start = page * 5
    repo = OfferRequirementRepository(_database(context)); rows = []
    for offer in offers[start:start + 5]:
        chats = await repo.list_required_chats(offer.id); reqs = await repo.list_requirements(offer.id)
        rows.append([_button(f"{offer.title[:24]} · item {offer.item_id} · {'ON' if offer.is_enabled else 'OFF'} · C{len(chats)} R{len(reqs)}", state, "cmp", offer.id)])
    if not rows: rows.append([_button("No campaigns yet", state, "cmpcreate")])
    nav = []
    if page: nav.append(_button("‹", state, "cmppage", page - 1))
    if start + 5 < len(offers): nav.append(_button("›", state, "cmppage", page + 1))
    if nav: rows.append(nav)
    rows.extend([[_button("＋ Create campaign", state, "cmpcreate")], [_button("‹ Home", state, "home")]])
    state.update(view="campaigns", page=page)
    await _edit_panel(context, state, (notice + "\n\n" if notice else "") + "Campaigns\nSelect an offer to manage it.", _markup(rows), query=query)


async def _start_campaign_create(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], *, query: Any = None) -> None:
    items = await ItemRepository(_database(context)).list_recent(10)
    rows = [[_button(f"Item {item.id} · {item.media_type}", state, "cmpitem", item.id)] for item in items]
    rows.append([_button("‹ Campaigns", state, "campaigns", 0)])
    state["view"] = "campaign_create_item"
    await _edit_panel(context, state, "Create campaign\nChoose the media item to unlock.", _markup(rows), query=query)


async def _select_campaign_item(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], item_id: int, *, query: Any = None) -> None:
    state["wizard"] = {"item_id": item_id}; state["input"] = {"kind": "campaign_title"}; state["view"] = "campaign_title"; _touch_pending_flow(state)
    await _edit_panel(context, state, f"Item {item_id} selected. Send the campaign title as your next message.", _input_markup(state), query=query)


async def _create_campaign(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], *, query: Any = None) -> None:
    wizard = state.get("wizard"); actor = int(state["owner_user_id"])
    if not isinstance(wizard, dict) or "item_id" not in wizard or not wizard.get("title"): raise InvalidRecord("campaign creation is incomplete")
    slug = secrets.token_urlsafe(24); now = int(time.time() * 1000)
    offer = await OfferRepository(_database(context)).create(Offer(0, int(wizard["item_id"]), hash_share_token(slug), None, str(wizard["title"]), None, True, actor, now, now, slug))
    await _audit(_database(context), actor, "offer_created", str(offer.id), target_type="offer"); _clear_pending_flow(state)
    await _show_campaign(context, state, offer.id, query=query, notice="Campaign created. Share this deep link:")


async def _show_campaign(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, *, query: Any = None, notice: str | None = None) -> None:
    offer = await OfferRepository(_database(context)).get_by_id(offer_id)
    if offer is None: raise RecordNotFound("campaign does not exist")
    repo = OfferRequirementRepository(_database(context)); chats = await repo.list_required_chats(offer_id); reqs = await repo.list_requirements(offer_id)
    username = _panel_bot_username(context)
    link = (f"https://t.me/{username}?start={offer.slug_plain}" if username and offer.slug_plain
            else "(bot username not configured)" if offer.slug_plain
            else "Link not retrievable — backfilled from legacy token; existing circulating link continues to work")
    warning = "\n⚠ This offer has no requirements — files will be delivered freely to anyone who opens the link." if offer.is_enabled and not chats and not reqs else ""
    text = (notice + "\n" if notice else "") + f"{offer.title}\nItem: {offer.item_id}\nStatus: {'enabled' if offer.is_enabled else 'disabled'}\nChannels: {len(chats)} · Requirements: {len(reqs)}\n{link}{warning}"
    rows = [[_button("Disable" if offer.is_enabled else "Enable", state, "cmpdisable" if offer.is_enabled else "cmpenable", offer_id)], [_button("Edit title", state, "cmpedit_title", offer_id), _button("Edit description", state, "cmpedit_desc", offer_id)], [_button("Add channel", state, "cmpaddchat", offer_id)], [_button("Add requirement", state, "cmpaddreq", offer_id)]]
    rows.extend([[_button(f"Remove channel {chat.chat_id}", state, "cmrc", offer_id, chat.chat_id)] for chat in chats])
    rows.extend([[_button(f"Remove requirement {req.requirement_id}", state, "cmrr", offer_id, req.requirement_id)] for req in reqs])
    rows.extend([[_button("View stats", state, "cmpstats", offer_id)], [_button("‹ Campaigns", state, "campaigns", 0)]])
    state["view"] = "campaign"; await _edit_panel(context, state, text, _markup(rows), query=query)


async def _toggle_campaign(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, enabled: bool, *, query: Any = None) -> None:
    if await OfferRepository(_database(context)).get_by_id(offer_id) is None: raise RecordNotFound("campaign does not exist")
    async with _database(context).connection() as connection:
        await connection.execute("UPDATE offers SET is_enabled = ?, updated_at_ms = ? WHERE id = ?", (int(enabled), int(time.time() * 1000), offer_id))
    await _show_campaign(context, state, offer_id, query=query, notice="Campaign updated.")


async def _show_campaign_chat_picker(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, *, query: Any = None) -> None:
    chats = await RequiredChatRepository(_database(context)).list_enabled(); rows = [[_button((chat.username or chat.title)[:30], state, "cmpchat", offer_id, chat.chat_id)] for chat in chats]
    rows.append([_button("＋ Add new channel", state, "cmpnewchat", offer_id)])
    rows.append([_button("‹ Back", state, "cmp", offer_id)]); await _edit_panel(context, state, "Choose a required channel or group.", _markup(rows), query=query)


async def _start_campaign_chat_input(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, *, query: Any = None) -> None:
    state["wizard"] = {"offer_id": offer_id, "chat_step": "identifier"}
    state["input"] = {"kind": "campaign_chat"}
    state["view"] = "campaign_chat_identifier"
    _touch_pending_flow(state)
    await _edit_panel(context, state, "Send the channel @username, t.me link, or -100... id.", _input_markup(state), query=query)


async def _show_campaign_chat_name(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    await _edit_panel(context, state, "Send a display name (or press Skip to use the username).", _markup([[_button("Skip", state, "cmpchat_skipname")], [_button("Cancel", state, "cmpchatcancel")]]))


async def _show_campaign_chat_invite(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    await _edit_panel(context, state, "Send the invite URL (t.me/+... or t.me/... ), or Skip to derive from username.", _markup([[_button("Skip", state, "cmpchat_skipurl")], [_button("Cancel", state, "cmpchatcancel")]]))


async def _campaign_chat_skip_name(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], *, query: Any = None) -> None:
    wizard = state.get("wizard")
    if not isinstance(wizard, dict):
        raise InvalidRecord("channel setup is incomplete")
    wizard["chat_step"] = "invite_url"
    await _show_campaign_chat_invite(context, state)


async def _campaign_chat_skip_url(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], *, query: Any = None) -> None:
    await _finish_campaign_chat(context, state, query=query)


def _chat_identifier(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:/.*)?", value, re.IGNORECASE)
    if match:
        return "@" + match.group(1)
    return value if value.startswith("@") or value.startswith("-") else "@" + value


async def _finish_campaign_chat(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], *, query: Any = None) -> None:
    wizard = state.get("wizard")
    if not isinstance(wizard, dict):
        raise InvalidRecord("channel setup is incomplete")
    try:
        chat = await context.bot.get_chat(str(wizard["identifier"]))
    except Exception:
        await _edit_panel(context, state, "Bot cannot access that channel — is it public or is the bot an admin there?", _input_markup(state), query=query)
        return
    chat_type = str(getattr(chat, "type", "channel"))
    if chat_type not in {"channel", "group", "supergroup"}:
        raise InvalidRecord("The identifier must resolve to a channel or group")
    username = getattr(chat, "username", None)
    username = str(username).lstrip("@").strip() if username else None
    invite_url = wizard.get("invite_url") or (f"https://t.me/{username}" if username else None)
    if not invite_url:
        await _edit_panel(context, state, "An invite URL is required when Telegram does not provide a public username.", _input_markup(state), query=query)
        return
    title = str(wizard.get("display_name") or username or getattr(chat, "title", "Required channel"))
    await RequiredChatRepository(_database(context)).upsert(RequiredChat(chat_id=int(chat.id), chat_type=chat_type, title=title, username=username, invite_url=invite_url))
    await OfferRequirementRepository(_database(context)).add_required_chat(int(wizard["offer_id"]), int(chat.id))
    offer_id = int(wizard["offer_id"])
    _clear_pending_flow(state)
    await _show_campaign(context, state, offer_id, query=query, notice="Channel requirement added.")


async def _add_campaign_chat(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, chat_id: int, *, query: Any = None) -> None:
    await OfferRequirementRepository(_database(context)).add_required_chat(offer_id, chat_id); await _show_campaign(context, state, offer_id, query=query, notice="Channel requirement added.")


async def _remove_campaign_chat(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, chat_id: int, *, query: Any = None) -> None:
    await OfferRequirementRepository(_database(context)).remove_required_chat(offer_id, chat_id); await _show_campaign(context, state, offer_id, query=query, notice="Channel requirement removed.")


async def _show_campaign_requirement_picker(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, *, query: Any = None) -> None:
    requirements = tuple(item for item in await RequirementRepository(_database(context)).list_all() if item.type == "REACT")
    rows = [[_button(f"{item.type} · {item.id}", state, "cmpreq", offer_id, item.id)] for item in requirements]
    text = "Choose an existing requirement (including REACT)." if requirements else "No REACT requirements defined yet. Use /addreq on the admin channel to create one, then return here."
    rows.append([_button("‹ Back", state, "cmp", offer_id)]); await _edit_panel(context, state, text, _markup(rows), query=query)


async def _add_campaign_requirement(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, requirement_id: int, *, query: Any = None) -> None:
    await OfferRequirementRepository(_database(context)).add_requirement(offer_id, requirement_id); await _show_campaign(context, state, offer_id, query=query, notice="Requirement added.")


async def _remove_campaign_requirement(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, requirement_id: int, *, query: Any = None) -> None:
    await OfferRequirementRepository(_database(context)).remove_requirement(offer_id, requirement_id); await _show_campaign(context, state, offer_id, query=query, notice="Requirement removed.")


async def _show_campaign_stats(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, *, query: Any = None) -> None:
    values = await OfferEventRepository(_database(context)).stats(offer_id); item = values[0] if values else None
    text = "Campaign stats\nNo events yet." if item is None else f"Campaign stats\nStarts: {item.starts} ({item.distinct_start_users} users)\nUnlocks: {item.unlocks} ({item.distinct_unlock_users} users)\nDeliveries: {item.deliveries}\nConversion: {item.conversion_rate_percent:.1f}%"
    await _edit_panel(context, state, text, _markup([[_button("‹ Back", state, "cmp", offer_id)]]), query=query)


async def _show_panel_stats(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], *, query: Any = None) -> None:
    values = await OfferEventRepository(_database(context)).stats(); starts = sum(x.starts for x in values); unlocks = sum(x.unlocks for x in values); deliveries = sum(x.deliveries for x in values)
    await _edit_panel(context, state, f"Stats\nStarts: {starts}\nSuccessful unlocks: {unlocks}\nDeliveries: {deliveries}\nConversion: {(unlocks / starts * 100 if starts else 0):.1f}%", _markup([[_button("‹ Home", state, "home")]]), query=query)


async def _start_campaign_edit(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], offer_id: int, field: str, *, query: Any = None) -> None:
    if field not in {"title", "description"}:
        raise InvalidRecord("unsupported campaign field")
    state["wizard"] = {"offer_id": offer_id, "field": field}
    state["input"] = {"kind": "campaign_edit"}
    _touch_pending_flow(state)
    await _edit_panel(context, state, f"Send the new campaign {field}.", _input_markup(state), query=query)
