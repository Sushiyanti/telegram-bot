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
from media_bot.db.repositories import OfferEventRepository, OfferRepository
from media_bot.db.repositories import RequiredChatRepository


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

    items = ItemRepository(_database(context))
    item = await items.get(item_id)
    if item is None:
        await message.reply_text("Item not found.")
        return
    expected_storage_id = item.storage_channel_id or _settings(context).storage_channel_id
    references = await items.delete(item_id)
    if references is None:
        await message.reply_text("Item not found.")
        return

    failed = 0
    deleted = 0
    for reference in references:
        if reference.chat_id != expected_storage_id:
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


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bounded marketing-funnel conversion statistics without secret identifiers."""
    message = update.effective_message
    if message is None:
        return
    offer_id: int | None = None
    if context.args:
        if len(context.args) != 1 or _INTEGER.fullmatch(context.args[0]) is None:
            await message.reply_text("Usage: /stats [offer_id]")
            return
        offer_id = int(context.args[0])
        if offer_id <= 0:
            await message.reply_text("Usage: /stats [offer_id]")
            return
    days = 30
    cutoff = int(datetime.now(tz=timezone.utc).timestamp() * 1000) - days * 86_400_000
    database = _database(context)
    async with database.connection() as connection:
        offer_filter = " AND offers.id = ?" if offer_id is not None else ""
        parameters: tuple[object, ...] = (cutoff,) if offer_id is None else (cutoff, offer_id)
        rows = await (await connection.execute(
            """SELECT offers.id, offers.title,
                SUM(CASE WHEN e.event_type='start' THEN 1 ELSE 0 END) starts,
                COUNT(DISTINCT CASE WHEN e.event_type='start' THEN e.user_id END) start_users,
                SUM(CASE WHEN e.event_type='unlock' THEN 1 ELSE 0 END) unlocks,
                COUNT(DISTINCT CASE WHEN e.event_type='unlock' THEN e.user_id END) unlock_users,
                SUM(CASE WHEN e.event_type='delivery' THEN 1 ELSE 0 END) deliveries,
                SUM(CASE WHEN e.event_type='delivery_failure' THEN 1 ELSE 0 END) failures
              FROM offers LEFT JOIN offer_events e ON e.offer_id=offers.id AND e.created_at_ms >= ?
              WHERE 1 = 1""" + offer_filter + """
              GROUP BY offers.id ORDER BY unlocks DESC, starts DESC, offers.id DESC""", parameters
        )).fetchall()
    total = [sum(int(row[key] or 0) for row in rows) for key in ("starts", "unlocks", "deliveries", "failures")]
    scope = "aggregate" if offer_id is None else f"offer {offer_id}"
    lines = [f"Marketing stats ({scope}, last {days} days)", f"Starts: {total[0]} · Unlocks: {total[1]} · Deliveries: {total[2]} · Failures: {total[3]}", f"Conversion: {(total[1] / total[0] * 100 if total[0] else 0):.1f}%"]
    if rows:
        lines.append("Top offers:")
        for row in rows[:5]:
            starts = int(row["starts"] or 0); unlocks = int(row["unlocks"] or 0)
            lines.append(f"• {str(row['title'])[:40]} — {starts} starts, {unlocks} unlocks, {(unlocks / starts * 100 if starts else 0):.1f}%")
    await message.reply_text("\n".join(lines))


async def fixinvites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repair missing required-chat URLs using Telegram's invite_link or username."""
    message = update.effective_message
    if message is None:
        return
    database = _database(context)
    chats = await RequiredChatRepository(database).list_enabled()
    filled: list[int] = []
    failed: list[int] = []
    async with database.connection() as connection:
        for row in chats:
            if row.invite_url:
                continue
            try:
                chat = await context.bot.get_chat(row.chat_id)
                invite_link = getattr(chat, "invite_link", None)
                username = getattr(chat, "username", None)
                url = invite_link if isinstance(invite_link, str) and invite_link.startswith("https://") else (
                    f"https://t.me/{str(username).lstrip('@')}" if username else None
                )
                if not url:
                    failed.append(row.chat_id)
                    continue
                await connection.execute(
                    "UPDATE required_chats SET invite_url = ?, updated_at_ms = ? WHERE chat_id = ?",
                    (url, int(datetime.now(tz=timezone.utc).timestamp() * 1000), row.chat_id),
                )
                filled.append(row.chat_id)
            except Exception:
                logger.warning("fixinvites_chat_failed chat_id=%s", row.chat_id, exc_info=True)
                failed.append(row.chat_id)
        await connection.commit()
    await message.reply_text(
        f"Invite repair complete. Filled: {len(filled)} ({', '.join(map(str, filled)) or 'none'}). "
        f"Failed: {len(failed)} ({', '.join(map(str, failed)) or 'none'})."
    )


async def reactiondiag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report safe reaction-pipeline and configured REACT-gate diagnostics."""
    message = update.effective_message
    if message is None:
        return
    database = _database(context)
    offer_filter = ""
    params: tuple[object, ...] = ()
    if context.args:
        if len(context.args) != 1 or _INTEGER.fullmatch(context.args[0]) is None or int(context.args[0]) <= 0:
            await message.reply_text("Usage: /reactiondiag [offer_id]")
            return
        offer_filter = " AND offers.id = ?"
        params = (int(context.args[0]),)
    async with database.connection() as connection:
        rows = await (await connection.execute(
            """SELECT offers.id offer_id, requirements.id requirement_id,
                      requirements.target, requirements.settings_json
               FROM offers
               JOIN rule_assignments ON rule_assignments.offer_id = offers.id
                                      AND rule_assignments.scope_type = 'OFFER'
               JOIN requirements ON requirements.id = rule_assignments.requirement_id
               WHERE requirements.type = 'REACT'""" + offer_filter + " ORDER BY offers.id, requirements.id",
            params,
        )).fetchall()
        recent = await (await connection.execute(
            """SELECT user_id, channel_id, message_id, emoji, updated_at_ms
               FROM user_reactions ORDER BY updated_at_ms DESC LIMIT 20"""
        )).fetchall()
    lines = ["Reaction diagnostics", f"Configured REACT requirements: {len(rows)}"]
    for row in rows:
        lines.append(
            f"offer={row['offer_id']} requirement={row['requirement_id']} "
            f"channel={row['target']} settings={str(row['settings_json'])[:300]}"
        )
    lines.append(f"Persisted reaction rows (latest 20): {len(recent)}")
    for row in recent:
        lines.append(
            f"user={row['user_id']} channel={row['channel_id']} "
            f"message={row['message_id']} emoji={row['emoji']}"
        )
    await message.reply_text("\n".join(lines)[:4_000])
