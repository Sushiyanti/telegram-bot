"""Environment-backed settings for the Telegram bot."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import find_dotenv, load_dotenv


class ConfigurationError(ValueError):
    """Raised when runtime settings are missing or invalid."""


# Populated from validated Settings during application construction. Authorization
# reads this configuration value directly and never queries the database.
ADMIN_IDS: frozenset[int] = frozenset()
_MIN_SIGNED_64 = -(1 << 63)
_MAX_SIGNED_64 = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated application settings."""

    bot_token: str
    storage_channel_id: int
    admin_ids: tuple[int, ...]
    db_path: Path
    log_level: str
    env: str
    max_relay_file_bytes: int
    default_protect_content: bool
    uploader_mode: str = "admin"
    uploader_ids: tuple[int, ...] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Parse settings from a mapping or the process environment and ``.env``."""
        if environ is None:
            load_dotenv(find_dotenv(usecwd=True), override=False)
            source: Mapping[str, str] = os.environ
        else:
            source = environ

        errors: list[str] = []
        bot_token = source.get("BOT_TOKEN", "").strip()
        if not bot_token:
            errors.append("BOT_TOKEN is required")
        elif re.fullmatch(r"[0-9]+:[^:\s]+", bot_token) is None:
            errors.append("BOT_TOKEN must have the BotFather token format")

        storage_channel_id = _parse_storage_channel_id(
            source.get("STORAGE_CHANNEL_ID", ""), errors
        )
        admin_ids = _parse_admin_ids(source.get("ADMIN_IDS", ""), errors)

        uploader_mode = source.get("UPLOADER_MODE", "admin").strip().lower()
        if uploader_mode not in {"admin", "allowlist", "all"}:
            errors.append("UPLOADER_MODE must be admin, allowlist, or all")
            uploader_mode = "admin"
        uploader_ids = (
            _parse_uploader_ids(source.get("UPLOADER_IDS", ""), errors)
            if uploader_mode == "allowlist"
            else ()
        )

        raw_db_path = source.get("DB_PATH", "./data/bot.db").strip()
        if not raw_db_path:
            errors.append("DB_PATH cannot be empty")
            db_path = Path("./data/bot.db")
        else:
            db_path = Path(raw_db_path).expanduser()

        log_level = source.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            errors.append("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

        env = source.get("ENV", "dev").strip().lower()
        if env not in {"dev", "prod"}:
            errors.append("ENV must be dev or prod")

        max_relay_file_bytes = _parse_positive_int(
            source.get("MAX_RELAY_FILE_BYTES", "2147483648"),
            "MAX_RELAY_FILE_BYTES",
            errors,
        )

        raw_protect_content = source.get("DEFAULT_PROTECT_CONTENT", "false").strip().lower()
        if raw_protect_content not in {"true", "false"}:
            errors.append("DEFAULT_PROTECT_CONTENT must be true or false")
        default_protect_content = raw_protect_content == "true"

        if errors:
            raise ConfigurationError("Invalid configuration: " + "; ".join(errors))

        return cls(
            bot_token=bot_token,
            storage_channel_id=storage_channel_id,
            admin_ids=admin_ids,
            db_path=db_path,
            log_level=log_level,
            env=env,
            max_relay_file_bytes=max_relay_file_bytes,
            default_protect_content=default_protect_content,
            uploader_mode=uploader_mode,
            uploader_ids=uploader_ids,
        )


def configure_admin_ids(admin_ids: tuple[int, ...] | frozenset[int]) -> None:
    """Install validated admin IDs for fast, database-independent authorization."""
    global ADMIN_IDS
    ADMIN_IDS = frozenset(admin_ids)


def _parse_admin_ids(raw_value: str, errors: list[str]) -> tuple[int, ...]:
    if not raw_value.strip():
        errors.append("ADMIN_IDS must contain at least one numeric Telegram user ID")
        return ()

    parts = raw_value.split(",")
    parsed: list[int] = []
    invalid = False
    for raw_part in parts:
        part = raw_part.strip()
        if (
            not part
            or not part.isascii()
            or not part.isdecimal()
            or len(part) > 19
        ):
            invalid = True
            continue
        user_id = int(part)
        if user_id <= 0 or user_id > _MAX_SIGNED_64:
            invalid = True
            continue
        parsed.append(user_id)
    if invalid:
        errors.append("ADMIN_IDS entries must be positive numeric Telegram user IDs")
        return ()
    if len(set(parsed)) != len(parsed):
        errors.append("ADMIN_IDS must not contain duplicate IDs")
        return ()
    return tuple(parsed)


def _parse_uploader_ids(raw_value: str, errors: list[str]) -> tuple[int, ...]:
    if not raw_value.strip():
        errors.append("UPLOADER_IDS must contain at least one numeric Telegram user ID when UPLOADER_MODE=allowlist")
        return ()

    parts = raw_value.split(",")
    parsed: list[int] = []
    invalid = False
    for raw_part in parts:
        part = raw_part.strip()
        if (
            not part
            or not part.isascii()
            or not part.isdecimal()
            or len(part) > 19
        ):
            invalid = True
            continue
        user_id = int(part)
        if user_id <= 0 or user_id > _MAX_SIGNED_64:
            invalid = True
            continue
        parsed.append(user_id)
    if invalid:
        errors.append("UPLOADER_IDS entries must be positive numeric Telegram user IDs")
        return ()
    if len(set(parsed)) != len(parsed):
        errors.append("UPLOADER_IDS must not contain duplicate IDs")
        return ()
    return tuple(parsed)


def _parse_storage_channel_id(raw_value: str, errors: list[str]) -> int:
    value = raw_value.strip()
    if not value:
        errors.append("STORAGE_CHANNEL_ID is required")
        return 0
    digits = value.lstrip("+-")
    if re.fullmatch(r"[+-]?[0-9]+", value) is None or len(digits) > 19:
        errors.append("STORAGE_CHANNEL_ID must be an integer Telegram channel ID")
        return 0
    channel_id = int(value)
    if channel_id == 0 or not (_MIN_SIGNED_64 <= channel_id <= _MAX_SIGNED_64):
        errors.append("STORAGE_CHANNEL_ID must fit a non-zero signed 64-bit integer")
        return 0
    return channel_id


def _parse_positive_int(raw_value: str, field: str, errors: list[str]) -> int:
    value = raw_value.strip()
    if re.fullmatch(r"[0-9]+", value) is None or len(value) > 19:
        errors.append(f"{field} must be a positive integer")
        return 0
    parsed = int(value)
    if parsed <= 0 or parsed > _MAX_SIGNED_64:
        errors.append(f"{field} must be a positive integer")
        return 0
    return parsed
