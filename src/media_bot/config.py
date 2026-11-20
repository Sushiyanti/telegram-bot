"""Environment-backed settings for the Telegram bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigurationError(ValueError):
    """Raised when required runtime settings are missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated application settings."""

    bot_token: str
    admin_ids: tuple[int, ...]
    storage_chat_id: int
    database_path: Path
    log_level: str
    default_protect_content: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Parse and validate settings from a mapping or the process environment."""
        source = os.environ if environ is None else environ
        errors: list[str] = []

        bot_token = source.get("BOT_TOKEN", "").strip()
        token_prefix, separator, token_secret = bot_token.partition(":")
        if not bot_token:
            errors.append("BOT_TOKEN is required")
        elif (
            not separator
            or not token_prefix.isdecimal()
            or not token_secret
            or ":" in token_secret
        ):
            errors.append("BOT_TOKEN must have the BotFather token format")

        admin_ids = _parse_admin_ids(source.get("ADMIN_IDS", ""), errors)
        storage_chat_id = _parse_storage_chat_id(
            source.get("STORAGE_CHAT_ID", ""), errors
        )

        raw_path = source.get(
            "DATABASE_PATH", "/var/lib/telegram-storage-bot/bot.sqlite3"
        ).strip()
        if not raw_path:
            errors.append("DATABASE_PATH cannot be empty")
            database_path = Path("/var/lib/telegram-storage-bot/bot.sqlite3")
        else:
            database_path = Path(raw_path).expanduser()

        log_level = source.get("LOG_LEVEL", "INFO").strip().upper()
        allowed_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if log_level not in allowed_log_levels:
            errors.append("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

        raw_protect_content = source.get("DEFAULT_PROTECT_CONTENT", "false").strip().lower()
        if raw_protect_content not in {"true", "false"}:
            errors.append("DEFAULT_PROTECT_CONTENT must be true or false")
        default_protect_content = raw_protect_content == "true"

        if errors:
            raise ConfigurationError("Invalid configuration: " + "; ".join(errors))

        return cls(
            bot_token=bot_token,
            admin_ids=admin_ids,
            storage_chat_id=storage_chat_id,
            database_path=database_path,
            log_level=log_level,
            default_protect_content=default_protect_content,
        )


def _parse_admin_ids(raw_value: str, errors: list[str]) -> tuple[int, ...]:
    parts = [part.strip() for part in raw_value.split(",") if part.strip()]
    if not parts:
        errors.append("ADMIN_IDS must contain at least one numeric Telegram user ID")
        return ()

    parsed: list[int] = []
    for part in parts:
        if not part.isdecimal() or int(part) <= 0:
            errors.append("ADMIN_IDS must contain only positive numeric Telegram user IDs")
            return ()
        parsed.append(int(part))

    if len(set(parsed)) != len(parsed):
        errors.append("ADMIN_IDS must not contain duplicate IDs")
        return ()
    return tuple(parsed)


def _parse_storage_chat_id(raw_value: str, errors: list[str]) -> int:
    value = raw_value.strip()
    if not value:
        errors.append("STORAGE_CHAT_ID is required")
        return 0
    try:
        chat_id = int(value)
    except ValueError:
        errors.append("STORAGE_CHAT_ID must be a numeric Telegram chat ID")
        return 0
    if chat_id == 0:
        errors.append("STORAGE_CHAT_ID cannot be zero")
        return 0
    return chat_id
