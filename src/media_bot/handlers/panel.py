"""Single-message inline admin control panel for M6."""
from __future__ import annotations

import logging
import math
import re
import secrets
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from media_bot.auth import is_admin
from media_bot.config import Settings
from media_bot.db.admins import AdminRepository
from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database
from media_bot.db.repositories import InvalidRecord, RecordNotFound, UploaderRepository
from media_bot.db.rules import (
    BotSettingsRepository,
    CategoryRepository,
    RequirementRepository,
)

logger = logging.getLogger(__name__)
_PANEL_KEY = "m6_admin_panel"
_PAGE_SIZE = 6
_MAX_ID = (1 << 63) - 1
_NUMERIC_ID = re.compile(r"^[0-9]{1,19}$")


def _database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _panel(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    value = context.user_data.get(_PANEL_KEY)
    return value if isinstance(value, dict) else None


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
        [_button("📁 Categories", state, "categories", 0), _button("📜 Requirements", state, "requirements", 0)],
        [_button("👥 Staff", state, "staff"), _button("⚙️ Settings", state, "settings")],
        [_button("Close Menu", state, "close")],
    ])


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open or reuse this admin's one private panel message."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None or chat is None or getattr(chat, "type", None) != "private":
        return
    database = _database(context)
    if not await is_admin(int(user.id), database):
        await message.reply_text("Not authorized.")
        return

    state = _panel(context)
    if state is not None and int(state.get("chat_id", 0)) == int(chat.id):
        state.update({
            "nonce": secrets.token_hex(4),
            "owner_user_id": int(user.id),
            "view": "home",
            "closed": False,
            "input": None,
        })
        await _edit_panel(
            context,
            state,
            "Admin Control Panel",
            _home_markup(state),
        )
        return

    state = {
        "nonce": secrets.token_hex(4),
        "owner_user_id": int(user.id),
        "chat_id": int(chat.id),
        "message_id": 0,
        "view": "home",
        "closed": False,
        "input": None,
    }
    context.user_data[_PANEL_KEY] = state
    try:
        sent = await context.bot.send_message(
            chat_id=int(chat.id),
            text="Admin Control Panel",
            reply_markup=_home_markup(state),
        )
    except Exception as exc:
        context.user_data.pop(_PANEL_KEY, None)
        logger.warning("admin_panel_send_failed (%s)", type(exc).__name__)
        return
    state["message_id"] = int(sent.message_id)


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
    state["nonce"] = secrets.token_hex(4)
    action, *args = parts[2:]
    try:
        await _dispatch_callback(action, args, query, context, state)
    except (InvalidRecord, RecordNotFound, ValueError) as exc:
        await _edit_panel(context, state, f"Could not complete that action: {exc}", _back_home(state), query=query)
    except Exception as exc:
        logger.error("admin_panel_action_failed (%s)", type(exc).__name__)
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
    pending = state.get("input")
    if not isinstance(pending, dict):
        return
    if getattr(chat, "type", None) != "private" or int(chat.id) != int(state.get("chat_id", 0)):
        return
    if int(state.get("owner_user_id", 0)) != int(user.id):
        return
    if not await is_admin(int(user.id), _database(context)):
        state["input"] = None
        await _audit_denial(_database(context), int(user.id), "admin_panel_input")
        return
    if state.get("closed"):
        state["input"] = None
        return
    state["nonce"] = secrets.token_hex(4)

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
            changed = await AdminRepository(_database(context)).add(
                user_id=target_id, added_by=actor_id
            )
            result = "Administrator added successfully." if changed else "That user is already an administrator."
            await _show_staff(context, state, "admins", int(state.get("page", 0)), notice=result)
        else:
            changed = await UploaderRepository(_database(context)).add(
                user_id=target_id, added_by=actor_id
            )
            result = "Uploader added successfully." if changed else "That user is already an uploader."
            await _show_staff(context, state, "uploaders", int(state.get("page", 0)), notice=result)
        state["input"] = None
        raise ApplicationHandlerStop

    if pending.get("kind") == "add_requirement":
        rule = await _parse_rule_input(
            getattr(message, "text", None),
            category_id=pending.get("category_id"),
        )
        if rule is None:
            await _edit_panel(
                context,
                state,
                "Invalid rule. Send: TYPE TARGET IS_FAKE WAIT_SECONDS; for REACT you may append LAST_X_MESSAGES LAST_X_DAYS.",
                _input_markup(state),
            )
            raise ApplicationHandlerStop
        try:
            created = await RequirementRepository(_database(context)).create(**rule)
        except (InvalidRecord, RecordNotFound, ValueError) as exc:
            await _edit_panel(context, state, f"Rule was not added: {exc}", _input_markup(state))
            raise ApplicationHandlerStop
        category_id = pending.get("category_id")
        scope_name = "Global" if category_id is None else str(pending.get("scope_name", "Category"))
        await _audit(
            _database(context),
            int(user.id),
            "requirement_created",
            str(created.id),
            target_type="requirement",
        )
        state["input"] = None
        await _show_requirements(
            context,
            state,
            category_id,
            notice=f"Requirement {created.id} added to {scope_name}.",
        )
        raise ApplicationHandlerStop

    state["input"] = None
    raise ApplicationHandlerStop


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


async def _parse_rule_input(value: object, *, category_id: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    parts = value.split()
    if len(parts) not in {4, 6}:
        return None
    kind, target, fake, wait = parts[:4]
    kind = kind.upper()
    if kind not in {"JOIN_CHAT", "REACT", "START_BOT", "TIMED_WAIT"}:
        return None
    if fake.lower() not in {"true", "false"} or not wait.isascii() or not wait.isdecimal():
        return None
    if len(parts) == 6 and (kind != "REACT" or any(not p.isascii() or not p.isdecimal() for p in parts[4:])):
        return None
    return {
        "category_id": category_id,
        "requirement_type": kind,
        "target": target,
        "is_fake": fake.lower() == "true",
        "wait_seconds": int(wait),
        "last_x_messages": int(parts[4]) if len(parts) == 6 else None,
        "last_x_days": int(parts[5]) if len(parts) == 6 else None,
    }


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
        state["input"] = None
        await _edit_panel(context, state, "Admin panel closed.", None, query=query)
    elif action == "home":
        state.update({"view": "home", "input": None})
        await _edit_panel(context, state, "Admin Control Panel", _home_markup(state), query=query)
    elif action == "categories":
        await _show_categories(context, state, _int_arg(args, 0, 0), query=query)
    elif action == "catpage":
        await _show_categories(context, state, _int_arg(args, 0), query=query)
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
        await _show_requirement_delete(
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
    elif action == "inputcancel":
        state["input"] = None
        if state.get("view") == "input_staff_admin":
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
    rows.append([_button("Main Menu", state, "home")])
    text = f"Categories — {page + 1}/{pages}"
    if notice:
        text += f"\n\n{notice}"
    if not visible:
        text += "\nNo categories found."
    await _edit_panel(context, state, text, _markup(rows), query=query)


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
        f"Delete {category.name}? Its items will move to Default. Choose what to do with orphaned requirements.",
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
    await _show_categories(context, state, int(state.get("page", 0)), query=query, notice=f"{category.name} deleted; items reassigned to Default.")


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
        lines.append(f"#{requirement.id} {requirement.type} | {target} | wait {requirement.settings.get('wait_seconds', 0)}s")
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
        "view": "input_requirement",
        "scope_id": category_id,
        "scope_name": category_name,
        "input": {"kind": "add_requirement", "category_id": category_id, "scope_name": category_name},
    })
    await _edit_panel(
        context,
        state,
        f"Add a rule to {category_name}. Send: TYPE TARGET IS_FAKE WAIT_SECONDS. For REACT, optionally append LAST_X_MESSAGES LAST_X_DAYS. TIMED_WAIT uses target -.",
        _input_markup(state),
        query=query,
    )


def _input_markup(state: dict[str, Any]) -> InlineKeyboardMarkup:
    return _markup([[_button("Cancel", state, "inputcancel")]])


async def _show_requirement_delete(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    category_id: int | None,
    requirement_id: int,
    *,
    query: Any = None,
) -> None:
    requirement = await RequirementRepository(_database(context)).get(requirement_id)
    if requirement is None:
        await _show_requirements(context, state, category_id, query=query, notice="Requirement no longer exists.")
        return
    state.update({"view": "requirement_delete", "scope_id": category_id})
    scope_token = "g" if category_id is None else category_id
    rows = [
        [_button("Confirm Delete", state, "reqdeletedo", scope_token, requirement_id, "yes")],
        [_button("Cancel", state, "reqdeletedo", scope_token, requirement_id, "no")],
    ]
    await _edit_panel(
        context,
        state,
        f"Remove requirement #{requirement_id} from this scope? Other scope associations will remain.",
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
    if choice == "yes":
        removed = await RequirementRepository(_database(context)).remove_from_scope(
            category_id=category_id,
            requirement_id=requirement_id,
        )
        if removed:
            await _audit(
                _database(context),
                int(state["owner_user_id"]),
                "requirement_removed",
                str(requirement_id),
                target_type="requirement",
            )
    await _show_requirements(
        context,
        state,
        category_id,
        query=query,
        notice="Requirement removed from this scope." if choice == "yes" else "Deletion cancelled.",
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
    rows.append([_button("Add", state, "staffadd", "admins" if role == "admins" else "uploaders")])
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
            await _show_staff(context, state, "admins", int(state.get("page", 0)), query=query, notice="Environment super-admins cannot be removed.")
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
    state.update({"view": "settings", "input": None})
    target_action = "unlock" if locked else "lock"
    markup = _markup([
        [_button(f"Lock Bot ({'ON' if locked else 'OFF'})", state, target_action)],
        [_button("Main Menu", state, "home"), _button("Close Menu", state, "close")],
    ])
    await _edit_panel(
        context,
        state,
        f"Settings\nRegular-user command/upload lock: {'ON' if locked else 'OFF'}",
        markup,
        query=query,
    )


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
    if _NUMERIC_ID.fullmatch(raw) is None:
        raise ValueError("Invalid numeric callback argument")
    parsed = int(raw)
    if parsed < 0 or parsed > _MAX_ID:
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
