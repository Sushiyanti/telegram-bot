"""Temporary M5 category and requirement commands and their limited inline callbacks."""

from __future__ import annotations

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from media_bot.auth import is_admin
from media_bot.db.audit_repository import AuditLogRepository
from media_bot.db.database import Database
from media_bot.db.repositories import InvalidRecord, RecordNotFound
from media_bot.db.rules import CategoryRepository, RequirementRepository
from media_bot.handlers.uploads import UploadHandler

logger = logging.getLogger(__name__)
_INTEGER = re.compile(r"^-?[0-9]+$")
_POSITIVE = re.compile(r"^[0-9]+$")


def _database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


async def add_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if len(context.args) != 2 or _INTEGER.fullmatch(context.args[1]) is None:
        await message.reply_text("Usage: /addcategory <name> <channel_id>")
        return
    try:
        category = await CategoryRepository(_database(context)).create(
            context.args[0], int(context.args[1])
        )
    except (InvalidRecord, ValueError) as exc:
        await message.reply_text(str(exc))
        return
    await _audit(
        _database(context),
        update.effective_user.id,
        "category_created",
        str(category.id),
    )
    await message.reply_text(f"Category {category.name} created.")


async def link_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if len(context.args) != 2:
        await message.reply_text("Usage: /linkcategory <parent_name> <child_name>")
        return
    try:
        await CategoryRepository(_database(context)).link(context.args[0], context.args[1])
    except (InvalidRecord, RecordNotFound) as exc:
        await message.reply_text(str(exc))
        return
    await _audit(
        _database(context),
        update.effective_user.id,
        "category_linked",
        f"{context.args[0]}->{context.args[1]}",
    )
    await message.reply_text("Category link created.")


async def add_requirement_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if (
        len(context.args) not in {5, 7}
        or context.args[3].lower() not in {"true", "false"}
        or _POSITIVE.fullmatch(context.args[4]) is None
        or (len(context.args) == 7 and context.args[1].upper() != "REACT")
        or (
            len(context.args) == 7
            and any(_POSITIVE.fullmatch(value) is None for value in context.args[5:7])
        )
    ):
        await message.reply_text(
            "Usage: /addrequirement <category_name|global> <type> <target> <is_fake> <wait_seconds> [last_x_messages last_x_days for REACT]"
        )
        return
    scope, kind, target, fake_text, wait_text = context.args[:5]
    category_id = None
    if scope.casefold() != "global":
        category = await CategoryRepository(_database(context)).get_by_name(scope)
        if category is None:
            await message.reply_text("Category was not found.")
            return
        category_id = category.id
    try:
        requirement = await RequirementRepository(_database(context)).create(
            category_id=category_id,
            requirement_type=kind,
            target=target,
            is_fake=fake_text.lower() == "true",
            wait_seconds=int(wait_text),
            last_x_messages=int(context.args[5]) if len(context.args) == 7 else None,
            last_x_days=int(context.args[6]) if len(context.args) == 7 else None,
        )
    except (InvalidRecord, RecordNotFound, ValueError) as exc:
        await message.reply_text(str(exc))
        return
    await _audit(
        _database(context),
        update.effective_user.id,
        "requirement_created",
        str(requirement.id),
    )
    scope_label = "Global" if category_id is None else scope
    await message.reply_text(f"Requirement {requirement.id} added to {scope_label}.")


async def delete_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if len(context.args) != 1:
        await message.reply_text("Usage: /delcategory <name>")
        return
    category = await CategoryRepository(_database(context)).get_by_name(context.args[0])
    if category is None:
        await message.reply_text("Category was not found.")
        return
    if category.name.casefold() == "default":
        await message.reply_text("The Default category cannot be deleted.")
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "A: Delete category and orphaned requirements",
                    callback_data=f"delcat:a:{category.id}:{user.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "B: Delete category, keep requirements",
                    callback_data=f"delcat:b:{category.id}:{user.id}",
                )
            ],
            [InlineKeyboardButton("Cancel", callback_data=f"delcat:x:{category.id}:{user.id}")],
        ]
    )
    await message.reply_text(
        f"Confirm deletion of {category.name}. Items and stored media will be retained and reassigned to Default; all generated share links for those items will be revoked. Linked categories and their storage channels will remain.",
        reply_markup=keyboard,
    )


async def delete_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    data = str(query.data or "")
    match = re.fullmatch(r"delcat:([abx]):([1-9][0-9]{0,18}):([1-9][0-9]{0,18})", data)
    if match is None:
        await query.answer("Invalid category action.", show_alert=True)
        return
    choice, category_text, owner_text = match.groups()
    if not await is_admin(int(user.id), _database(context)) or int(owner_text) != int(user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    if choice == "x":
        await query.answer("Deletion cancelled.")
        if query.message is not None:
            await query.edit_message_text("Category deletion cancelled.")
        return
    try:
        category = await CategoryRepository(_database(context)).get(int(category_text))
        if category is None:
            raise RecordNotFound("Category was not found")
        await CategoryRepository(_database(context)).delete(
            category.name,
            delete_orphaned_requirements=(choice == "a"),
        )
    except (InvalidRecord, RecordNotFound) as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await _audit(
        _database(context),
        int(user.id),
        "category_deleted",
        f"{category.id}:orphaned={'deleted' if choice == 'a' else 'kept'}",
    )
    await query.answer("Category deleted.")
    if query.message is not None:
        await query.edit_message_text(
            f"Category {category.name} deleted. Items and stored media were retained and reassigned to Default; share links for those items were revoked. Linked categories were kept. Orphaned requirements "
            f"{'were deleted.' if choice == 'a' else 'were kept.'}"
        )


async def category_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    data = str(query.data or "")
    match = re.fullmatch(r"uselect:([0-9a-f]{32}):([1-9][0-9]{0,18})", data)
    if match is None:
        await query.answer("This selection is invalid or expired.", show_alert=True)
        return
    upload_id, category_text = match.groups()
    handler: UploadHandler = context.application.bot_data["upload_handler"]
    upload = await handler.pending.repository.get(upload_id)
    if (
        upload is None
        or upload.owner_user_id != int(user.id)
        or not upload.category_selection_pending
    ):
        await query.answer("This upload selection is not yours or has expired.", show_alert=True)
        return
    category_id = int(category_text)
    category = await CategoryRepository(_database(context)).get(category_id)
    if category is None:
        await query.answer("That category is no longer available.", show_alert=True)
        return
    assigned = await handler.pending.repository.assign_category(
        upload_id,
        owner_user_id=int(user.id),
        category_id=category_id,
        selection_pending=False,
    )
    if not assigned:
        await query.answer("This upload can no longer be stored.", show_alert=True)
        return
    await query.answer(f"Saving to {category.name}.")
    await handler.process_selected_upload(upload, category_id, context, query.message)


async def _audit(database: Database, actor_id: int, action: str, target_id: str) -> None:
    try:
        await AuditLogRepository(database).record_event(
            actor_id=actor_id,
            action=action,
            target_type="category_or_requirement",
            target_id=target_id,
        )
    except Exception as exc:
        logger.error("m5_admin_audit_failed (%s)", type(exc).__name__)
