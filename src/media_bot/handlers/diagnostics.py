"""Safe identity and configuration diagnostic commands."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from media_bot.auth import admin_only, is_admin
from media_bot.config import Settings
from media_bot.db.database import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One sanitized PASS/FAIL diagnostic result."""

    name: str
    passed: bool
    detail: str


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


async def collect_config_checks(
    bot: Any, settings: Settings, database: Database
) -> list[DiagnosticCheck]:
    """Run configuration checks while never returning credentials or identifiers."""
    checks: list[DiagnosticCheck] = []

    bot_identity = None
    try:
        bot_identity = await bot.get_me()
        checks.append(DiagnosticCheck("Bot token valid (getMe)", True, "valid"))
    except Exception as exc:
        logger.warning("configcheck getMe failed (%s)", type(exc).__name__)
        checks.append(DiagnosticCheck("Bot token valid (getMe)", False, "unavailable"))

    storage_chat = None
    storage_is_channel = False
    try:
        storage_chat = await bot.get_chat(settings.storage_channel_id)
        storage_is_channel = _enum_value(getattr(storage_chat, "type", None)) == "channel"
        checks.append(
            DiagnosticCheck(
                "Storage channel reachable (getChat)",
                storage_is_channel,
                "reachable" if storage_is_channel else "chat is not a channel",
            )
        )
    except Exception as exc:
        logger.warning("configcheck getChat failed (%s)", type(exc).__name__)
        checks.append(DiagnosticCheck("Storage channel reachable (getChat)", False, "unavailable"))

    bot_is_admin = False
    if storage_is_channel and bot_identity is not None:
        try:
            member = await bot.get_chat_member(
                chat_id=settings.storage_channel_id,
                user_id=bot_identity.id,
            )
            status = _enum_value(getattr(member, "status", None))
            bot_is_admin = status in {"administrator", "creator"}
            detail = "administrator" if bot_is_admin else "not an administrator"
        except Exception as exc:
            logger.warning("configcheck getChatMember failed (%s)", type(exc).__name__)
            detail = "unavailable"
    else:
        detail = "identity or private channel unavailable"
    checks.append(DiagnosticCheck("Bot is a storage-channel administrator", bot_is_admin, detail))

    private_channel = storage_is_channel and getattr(storage_chat, "username", None) is None
    checks.append(
        DiagnosticCheck(
            "Storage channel is private",
            private_channel,
            "no public username; invite-link access only"
            if private_channel
            else "public username present or channel unavailable",
        )
    )

    admin_ids_valid = bool(settings.admin_ids) and all(
        type(user_id) is int and user_id > 0 for user_id in settings.admin_ids
    )
    admin_detail = (
        f"{len(settings.admin_ids)} configured; IDs redacted"
        if admin_ids_valid
        else "invalid or empty; IDs redacted"
    )
    checks.append(DiagnosticCheck("ADMIN_IDS parsed", admin_ids_valid, admin_detail))

    database_open = database.path.is_file()
    version: int | None = None
    writable = False
    if database_open:
        try:
            version, writable = await database.health_check()
        except Exception as exc:
            logger.warning("configcheck database inspection failed (%s)", type(exc).__name__)
            database_open = False
    checks.append(
        DiagnosticCheck(
            "Database open",
            database_open,
            "open" if database_open else "unavailable",
        )
    )

    expected_version = Database.latest_schema_version()
    schema_matches = database_open and version == expected_version
    if not database_open or version is None:
        schema_detail = "unavailable"
    elif schema_matches:
        schema_detail = f"version {expected_version}"
    else:
        schema_detail = f"expected {expected_version}; found {version}"
    checks.append(
        DiagnosticCheck(
            "Database schema version",
            schema_matches,
            schema_detail,
        )
    )
    checks.append(
        DiagnosticCheck(
            "Database writable",
            database_open and writable,
            "writable" if database_open and writable else "not writable or unavailable",
        )
    )

    max_size_valid = settings.max_relay_file_bytes > 0
    checks.append(
        DiagnosticCheck(
            "MAX_RELAY_FILE_BYTES",
            max_size_valid,
            f"{settings.max_relay_file_bytes} bytes",
        )
    )
    return checks


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the caller's numeric ID and effective admin status."""
    user = update.effective_user
    message = update.effective_message
    if user is not None and message is not None:
        database = context.application.bot_data.get("database")
        admin_status = "yes" if await is_admin(user.id, database) else "no"
        await message.reply_text(f"Your user_id: {user.id}\nAdmin: {admin_status}")


@admin_only
async def configcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report sanitized PASS/FAIL results for startup configuration checks."""
    message = update.effective_message
    if message is None:
        return

    bot_data = context.application.bot_data
    settings: Settings = bot_data["settings"]
    database: Database = bot_data["database"]
    checks = await collect_config_checks(context.bot, settings, database)
    failed = any(not check.passed for check in checks)
    exit_code = 1 if failed else 0
    marker = f"CONFIGCHECK_EXIT_CODE={exit_code}"
    if failed:
        logger.error(marker)
    else:
        logger.info(marker)

    lines = ["Config check:"]
    lines.extend(
        f"[{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.detail}" for check in checks
    )
    await message.reply_text("\n".join(lines))
