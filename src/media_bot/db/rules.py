"""Repositories for M5 categories, requirements, settings, and reaction state."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from media_bot.db.database import Database
from media_bot.db.repositories import InvalidRecord, RecordNotFound, _validate_text

_REQUIREMENT_TYPES = {"JOIN_CHAT", "REACT", "START_BOT", "TIMED_WAIT"}
_MAX_WAIT_SECONDS = 86_400
_MAX_LAST_MESSAGES = 1_000
_MAX_LAST_DAYS = 36_500
_INT64_MAX = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class Category:
    id: int
    name: str
    storage_channel_id: int | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class Requirement:
    id: int
    type: str
    target: str | None
    settings: dict[str, object]
    created_at_ms: int


class CategoryRepository:
    """Persist categories, category inheritance, and deletion choices."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def list(self) -> tuple[Category, ...]:
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM categories ORDER BY id")
            rows = await cursor.fetchall()
        return tuple(_category_from_row(row) for row in rows)

    async def get(self, category_id: int) -> Category | None:
        _positive_id(category_id, "category_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
            row = await cursor.fetchone()
        return None if row is None else _category_from_row(row)

    async def get_by_name(self, name: str) -> Category | None:
        name = _category_name(name)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM categories WHERE name = ? COLLATE NOCASE", (name,))
            row = await cursor.fetchone()
        return None if row is None else _category_from_row(row)

    async def default(self) -> Category:
        result = await self.get_by_name("Default")
        if result is None:
            raise RecordNotFound("Default category does not exist")
        return result

    async def ensure_default_storage_channel(self, storage_channel_id: int) -> Category:
        """Fill the migration placeholder with the configured legacy storage channel."""
        _chat_id(storage_channel_id, "storage_channel_id")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """UPDATE categories SET storage_channel_id = ?
                   WHERE name = 'Default' COLLATE NOCASE AND storage_channel_id IS NULL""",
                (storage_channel_id,),
            )
            await connection.execute(
                "UPDATE items SET storage_channel_id = ? WHERE storage_channel_id IS NULL",
                (storage_channel_id,),
            )
            await connection.commit()
        return await self.default()

    async def create(self, name: str, storage_channel_id: int) -> Category:
        name = _category_name(name)
        _chat_id(storage_channel_id, "storage_channel_id")
        created_at_ms = _now_ms()
        try:
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    "INSERT INTO categories (name, storage_channel_id, created_at_ms) VALUES (?, ?, ?)",
                    (name, storage_channel_id, created_at_ms),
                )
                category_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Category name already exists or category data is invalid") from exc
        return Category(category_id, name, storage_channel_id, created_at_ms)

    async def link(self, parent_name: str, child_name: str) -> None:
        parent = await self.get_by_name(parent_name)
        child = await self.get_by_name(child_name)
        if parent is None or child is None:
            raise RecordNotFound("Parent or child category was not found")
        if parent.id == child.id:
            raise InvalidRecord("A category cannot link to itself")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """WITH RECURSIVE descendants(id) AS (
                       SELECT child_category_id FROM category_links WHERE parent_category_id = ?
                       UNION
                       SELECT links.child_category_id FROM category_links AS links
                       JOIN descendants ON links.parent_category_id = descendants.id
                   ) SELECT 1 FROM descendants WHERE id = ? LIMIT 1""",
                (child.id, parent.id),
            )
            if await cursor.fetchone() is not None:
                await connection.rollback()
                raise InvalidRecord("Category links cannot create a cycle")
            try:
                await connection.execute(
                    "INSERT INTO category_links (parent_category_id, child_category_id) VALUES (?, ?)",
                    (parent.id, child.id),
                )
            except sqlite3.IntegrityError as exc:
                await connection.rollback()
                raise InvalidRecord("Category link already exists") from exc
            await connection.commit()

    async def requirements_for(self, category_id: int) -> tuple[Requirement, ...]:
        _positive_id(category_id, "category_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """WITH RECURSIVE inherited(id) AS (
                       SELECT id FROM categories WHERE id = ?
                       UNION
                       SELECT links.child_category_id FROM category_links AS links
                       JOIN inherited ON links.parent_category_id = inherited.id
                   )
                   SELECT DISTINCT requirements.* FROM requirements
                   JOIN category_requirements AS associations
                     ON associations.requirement_id = requirements.id
                   WHERE associations.category_id IS NULL
                      OR associations.category_id IN (SELECT id FROM inherited)
                   ORDER BY requirements.id""",
                (category_id,),
            )
            rows = await cursor.fetchall()
        return tuple(_requirement_from_row(row) for row in rows)

    async def links_for(
        self, category_id: int
    ) -> tuple[tuple[Category, ...], tuple[Category, ...]]:
        """Return this category's parents and children for an admin detail view."""
        _positive_id(category_id, "category_id")
        async with self.database.connection() as connection:
            parents_cursor = await connection.execute(
                """SELECT categories.* FROM categories
                   JOIN category_links ON category_links.parent_category_id = categories.id
                   WHERE category_links.child_category_id = ? ORDER BY categories.name COLLATE NOCASE""",
                (category_id,),
            )
            children_cursor = await connection.execute(
                """SELECT categories.* FROM categories
                   JOIN category_links ON category_links.child_category_id = categories.id
                   WHERE category_links.parent_category_id = ? ORDER BY categories.name COLLATE NOCASE""",
                (category_id,),
            )
            parents = await parents_cursor.fetchall()
            children = await children_cursor.fetchall()
        return (
            tuple(_category_from_row(row) for row in parents),
            tuple(_category_from_row(row) for row in children),
        )

    async def delete(self, name: str, *, delete_orphaned_requirements: bool) -> Category:
        category = await self.get_by_name(name)
        if category is None:
            raise RecordNotFound("Category was not found")
        if category.name.casefold() == "default":
            raise InvalidRecord("The Default category cannot be deleted")
        default = await self.default()
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("SELECT id FROM categories WHERE id = ?", (category.id,))
            if await cursor.fetchone() is None:
                await connection.rollback()
                raise RecordNotFound("Category was not found")
            await connection.execute(
                """UPDATE share_links SET revoked_at_ms = MAX(?, created_at_ms)
                   WHERE revoked_at_ms IS NULL
                     AND item_id IN (SELECT id FROM items WHERE category_id = ?)""",
                (_now_ms(), category.id),
            )
            await connection.execute(
                "UPDATE items SET category_id = ? WHERE category_id = ?",
                (default.id, category.id),
            )
            await connection.execute(
                "UPDATE pending_uploads SET category_id = ?, category_selection_pending = 0 "
                "WHERE category_id = ?",
                (default.id, category.id),
            )
            await connection.execute(
                "DELETE FROM category_requirements WHERE category_id = ?", (category.id,)
            )
            await connection.execute(
                "DELETE FROM category_links WHERE parent_category_id = ? OR child_category_id = ?",
                (category.id, category.id),
            )
            await connection.execute("DELETE FROM categories WHERE id = ?", (category.id,))
            if delete_orphaned_requirements:
                await connection.execute(
                    """DELETE FROM requirements
                       WHERE NOT EXISTS (
                           SELECT 1 FROM category_requirements
                           WHERE category_requirements.requirement_id = requirements.id
                       )"""
                )
            await connection.commit()
        return category


class RequirementRepository:
    """Validate and persist typed, category-scoped or global rules."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        *,
        category_id: int | None,
        requirement_type: str,
        target: str | int | None,
        is_fake: bool,
        wait_seconds: int,
        last_x_messages: int | None = None,
        last_x_days: int | None = None,
        link_url: str | None = None,
        attach_scope: bool = True,
    ) -> Requirement:
        requirement_type = str(requirement_type).upper()
        if requirement_type not in _REQUIREMENT_TYPES:
            raise InvalidRecord("Type must be JOIN_CHAT, REACT, START_BOT, or TIMED_WAIT")
        if not isinstance(is_fake, bool):
            raise InvalidRecord("is_fake must be a boolean")
        if requirement_type == "START_BOT" and not is_fake:
            raise InvalidRecord(
                "Real START_BOT verification is not supported yet, please use is_fake=True"
            )
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= _MAX_WAIT_SECONDS:
            raise InvalidRecord(f"wait_seconds must be between 0 and {_MAX_WAIT_SECONDS}")
        if category_id is not None:
            _positive_id(category_id, "category_id")
        stored_target = _validate_target(requirement_type, target)
        stored_link = validate_telegram_link(link_url)
        if stored_link is not None and requirement_type == "TIMED_WAIT":
            raise InvalidRecord("Telegram links can only be used with channel or bot requirements")
        settings: dict[str, object] = {"is_fake": is_fake, "wait_seconds": wait_seconds}
        if stored_link is not None:
            settings["link_url"] = stored_link
        if requirement_type == "REACT":
            messages = 5 if last_x_messages is None else last_x_messages
            days = 0 if last_x_days is None else last_x_days
            _bounded_int(messages, 0, _MAX_LAST_MESSAGES, "last_x_messages")
            _bounded_int(days, 0, _MAX_LAST_DAYS, "last_x_days")
            settings.update({"last_x_messages": messages, "last_x_days": days})
        elif last_x_messages is not None or last_x_days is not None:
            raise InvalidRecord("REACT window settings can only be used with REACT")
        created_at_ms = _now_ms()
        settings_json = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            async with self.database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                if category_id is not None:
                    cursor = await connection.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,))
                    if await cursor.fetchone() is None:
                        await connection.rollback()
                        raise RecordNotFound("Category was not found")
                cursor = await connection.execute(
                    "INSERT INTO requirements (type, target, settings_json, created_at_ms) VALUES (?, ?, ?, ?)",
                    (requirement_type, stored_target, settings_json, created_at_ms),
                )
                requirement_id = int(cursor.lastrowid)
                if attach_scope:
                    await connection.execute(
                        "INSERT INTO category_requirements (category_id, requirement_id) VALUES (?, ?)",
                        (category_id, requirement_id),
                    )
                await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise InvalidRecord("Requirement violates database constraints") from exc
        return Requirement(requirement_id, requirement_type, stored_target, settings, created_at_ms)

    async def get(self, requirement_id: int) -> Requirement | None:
        _positive_id(requirement_id, "requirement_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM requirements WHERE id = ?", (requirement_id,))
            row = await cursor.fetchone()
        return None if row is None else _requirement_from_row(row)

    async def list_for_scope(self, category_id: int | None) -> tuple[Requirement, ...]:
        """List rules directly attached to one category or the global scope."""
        if category_id is not None:
            _positive_id(category_id, "category_id")
        async with self.database.connection() as connection:
            if category_id is None:
                cursor = await connection.execute(
                    """SELECT requirements.* FROM requirements
                       JOIN category_requirements ON category_requirements.requirement_id = requirements.id
                       WHERE category_requirements.category_id IS NULL
                       ORDER BY requirements.id"""
                )
            else:
                cursor = await connection.execute(
                    """SELECT requirements.* FROM requirements
                       JOIN category_requirements ON category_requirements.requirement_id = requirements.id
                       WHERE category_requirements.category_id = ?
                       ORDER BY requirements.id""",
                    (category_id,),
                )
            rows = await cursor.fetchall()
        return tuple(_requirement_from_row(row) for row in rows)

    async def list_all(self) -> tuple[Requirement, ...]:
        """List every registered requirement for offer-level assignment."""
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM requirements ORDER BY id")
            rows = await cursor.fetchall()
        return tuple(_requirement_from_row(row) for row in rows)

    async def scopes_for(self, requirement_id: int) -> tuple[str, ...]:
        """Return direct scopes using a requirement, including the Global scope."""
        _positive_id(requirement_id, "requirement_id")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT category_requirements.category_id, categories.name
                   FROM category_requirements
                   LEFT JOIN categories
                     ON categories.id = category_requirements.category_id
                   WHERE category_requirements.requirement_id = ?
                   ORDER BY category_requirements.category_id""",
                (requirement_id,),
            )
            rows = await cursor.fetchall()
        return tuple(
            "Global" if row["category_id"] is None else str(row["name"])
            for row in rows
        )

    async def remove_everywhere(self, requirement_id: int) -> bool:
        """Delete a requirement and all direct scope associations atomically."""
        _positive_id(requirement_id, "requirement_id")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT 1 FROM requirements WHERE id = ?", (requirement_id,)
            )
            if await cursor.fetchone() is None:
                await connection.rollback()
                return False
            await connection.execute(
                "DELETE FROM category_requirements WHERE requirement_id = ?",
                (requirement_id,),
            )
            cursor = await connection.execute(
                "DELETE FROM requirements WHERE id = ?", (requirement_id,)
            )
            await connection.commit()
        return cursor.rowcount == 1

    async def remove_from_scope(
        self, *, category_id: int | None, requirement_id: int
    ) -> bool:
        """Remove only this scope's association; delete the rule when unreferenced."""
        if category_id is not None:
            _positive_id(category_id, "category_id")
        _positive_id(requirement_id, "requirement_id")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            if category_id is None:
                cursor = await connection.execute(
                    "DELETE FROM category_requirements WHERE category_id IS NULL AND requirement_id = ?",
                    (requirement_id,),
                )
            else:
                cursor = await connection.execute(
                    "DELETE FROM category_requirements WHERE category_id = ? AND requirement_id = ?",
                    (category_id, requirement_id),
                )
            if cursor.rowcount != 1:
                await connection.rollback()
                return False
            await connection.execute(
                """DELETE FROM requirements WHERE id = ? AND NOT EXISTS (
                       SELECT 1 FROM category_requirements WHERE requirement_id = ?
                   )""",
                (requirement_id, requirement_id),
            )
            await connection.commit()
        return True


class BotSettingsRepository:
    """Read and change durable key/value behavior flags."""

    COMMANDS_KEY = "ENABLE_USER_COMMANDS"
    REAL_CHECK_FAILURE_POLICY_KEY = "REAL_CHECK_FAILURE_POLICY"
    REAL_CHECK_FAIL_CLOSED = "fail_closed"
    REAL_CHECK_WAIT_FALLBACK = "wait_fallback"

    def __init__(self, database: Database) -> None:
        self.database = database

    async def user_commands_enabled(self) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT value FROM bot_settings WHERE key = ?", (self.COMMANDS_KEY,))
            row = await cursor.fetchone()
        return True if row is None else str(row["value"]).lower() == "true"

    async def toggle_user_commands(self) -> bool:
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("SELECT value FROM bot_settings WHERE key = ?", (self.COMMANDS_KEY,))
            row = await cursor.fetchone()
            enabled = not (row is not None and str(row["value"]).lower() == "true")
            await connection.execute(
                """INSERT INTO bot_settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (self.COMMANDS_KEY, "true" if enabled else "false"),
            )
            await connection.commit()
        return enabled

    async def set_user_commands_enabled(self, enabled: bool) -> bool:
        """Set lockdown state explicitly; return whether it changed."""
        if not isinstance(enabled, bool):
            raise InvalidRecord("enabled must be a boolean")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (self.COMMANDS_KEY,)
            )
            row = await cursor.fetchone()
            current = True if row is None else str(row["value"]).lower() == "true"
            if current == enabled:
                await connection.commit()
                return False
            await connection.execute(
                """INSERT INTO bot_settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (self.COMMANDS_KEY, "true" if enabled else "false"),
            )
            await connection.commit()
        return True

    async def real_check_failure_policy(self) -> str:
        """Return the durable real-check failure policy; absent/invalid values fail closed."""
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT value FROM bot_settings WHERE key = ?",
                (self.REAL_CHECK_FAILURE_POLICY_KEY,),
            )
            row = await cursor.fetchone()
        value = self.REAL_CHECK_FAIL_CLOSED if row is None else str(row["value"])
        if value not in {self.REAL_CHECK_FAIL_CLOSED, self.REAL_CHECK_WAIT_FALLBACK}:
            return self.REAL_CHECK_FAIL_CLOSED
        return value

    async def set_real_check_failure_policy(self, policy: str) -> bool:
        """Persist a supported real-check failure policy; return whether it changed."""
        if policy not in {self.REAL_CHECK_FAIL_CLOSED, self.REAL_CHECK_WAIT_FALLBACK}:
            raise InvalidRecord("policy must be fail_closed or wait_fallback")
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT value FROM bot_settings WHERE key = ?",
                (self.REAL_CHECK_FAILURE_POLICY_KEY,),
            )
            row = await cursor.fetchone()
            current = self.REAL_CHECK_FAIL_CLOSED if row is None else str(row["value"])
            if current == policy:
                await connection.commit()
                return False
            await connection.execute(
                """INSERT INTO bot_settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (self.REAL_CHECK_FAILURE_POLICY_KEY, policy),
            )
            await connection.commit()
        return True


class ReactionRepository:
    """Store channel posts and the currently observed user reactions."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def observe_post(self, channel_id: int, message_id: int, posted_at_ms: int) -> None:
        _chat_id(channel_id, "channel_id")
        _positive_id(message_id, "message_id")
        _nonnegative_int(posted_at_ms, "posted_at_ms")
        async with self.database.connection() as connection:
            await connection.execute(
                """INSERT INTO observed_channel_posts (channel_id, message_id, posted_at_ms)
                   VALUES (?, ?, ?) ON CONFLICT(channel_id, message_id) DO UPDATE SET
                   posted_at_ms = excluded.posted_at_ms""",
                (channel_id, message_id, posted_at_ms),
            )

    async def replace_user_message_reactions(
        self,
        *,
        user_id: int,
        channel_id: int,
        message_id: int,
        emojis: tuple[str, ...],
        updated_at_ms: int,
    ) -> None:
        _positive_id(user_id, "user_id")
        _chat_id(channel_id, "channel_id")
        _positive_id(message_id, "message_id")
        _nonnegative_int(updated_at_ms, "updated_at_ms")
        clean_emojis = tuple(dict.fromkeys(_emoji_key(value) for value in emojis))
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT 1 FROM observed_channel_posts WHERE channel_id = ? AND message_id = ?",
                (channel_id, message_id),
            )
            if await cursor.fetchone() is None:
                await connection.rollback()
                return
            await connection.execute(
                "DELETE FROM user_reactions WHERE user_id = ? AND channel_id = ? AND message_id = ?",
                (user_id, channel_id, message_id),
            )
            for emoji in clean_emojis:
                await connection.execute(
                    """INSERT INTO user_reactions
                       (user_id, channel_id, message_id, emoji, updated_at_ms)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user_id, channel_id, message_id, emoji, updated_at_ms),
                )
            await connection.commit()

    async def has_recent_reaction(
        self,
        *,
        user_id: int,
        channel_id: int,
        last_x_messages: int,
        last_x_days: int,
        now_ms: int | None = None,
    ) -> bool:
        _positive_id(user_id, "user_id")
        _chat_id(channel_id, "channel_id")
        _bounded_int(last_x_messages, 0, _MAX_LAST_MESSAGES, "last_x_messages")
        _bounded_int(last_x_days, 0, _MAX_LAST_DAYS, "last_x_days")
        now = _now_ms() if now_ms is None else now_ms
        _nonnegative_int(now, "now_ms")
        since_ms = 0 if last_x_days == 0 else now - last_x_days * 86_400_000
        async with self.database.connection() as connection:
            if last_x_messages == 0:
                cursor = await connection.execute(
                    """SELECT 1 FROM user_reactions AS reactions
                       JOIN observed_channel_posts AS posts
                         ON posts.channel_id = reactions.channel_id
                        AND posts.message_id = reactions.message_id
                       WHERE posts.channel_id = ? AND posts.posted_at_ms >= ?
                         AND reactions.user_id = ? LIMIT 1""",
                    (channel_id, since_ms, user_id),
                )
            else:
                cursor = await connection.execute(
                    """SELECT 1 FROM user_reactions AS reactions
                       JOIN (
                           SELECT message_id FROM observed_channel_posts
                           WHERE channel_id = ? AND posted_at_ms >= ?
                           ORDER BY posted_at_ms DESC, message_id DESC LIMIT ?
                       ) AS recent ON recent.message_id = reactions.message_id
                       WHERE reactions.user_id = ? AND reactions.channel_id = ? LIMIT 1""",
                    (channel_id, since_ms, last_x_messages, user_id, channel_id),
                )
            return await cursor.fetchone() is not None


def _category_from_row(row) -> Category:
    return Category(
        id=int(row["id"]),
        name=str(row["name"]),
        storage_channel_id=None if row["storage_channel_id"] is None else int(row["storage_channel_id"]),
        created_at_ms=int(row["created_at_ms"]),
    )


def _requirement_from_row(row) -> Requirement:
    return Requirement(
        id=int(row["id"]),
        type=str(row["type"]),
        target=None if row["target"] is None else str(row["target"]),
        settings=json.loads(str(row["settings_json"])),
        created_at_ms=int(row["created_at_ms"]),
    )


def _category_name(value: str) -> str:
    _validate_text(value, "category name", max_length=80)
    name = value.strip()
    if name.casefold() == "global":
        raise InvalidRecord("Global is reserved for global requirements")
    return name


def _validate_target(requirement_type: str, target: str | int | None) -> str | None:
    if requirement_type == "TIMED_WAIT":
        if target is not None and str(target).strip().lower() not in {"", "-", "null", "none"}:
            raise InvalidRecord("TIMED_WAIT target must be '-' or null")
        return None
    if requirement_type in {"JOIN_CHAT", "REACT"}:
        raw = str(target).strip() if target is not None else ""
        try:
            parsed = int(raw, 10)
        except (ValueError, TypeError) as exc:
            raise InvalidRecord(f"{requirement_type} target must be a numeric Telegram chat ID") from exc
        _chat_id(parsed, "target")
        return str(parsed)
    raw = str(target).strip() if target is not None else ""
    if raw.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw) is not None:
        return raw
    try:
        numeric_target = int(raw, 10)
    except (ValueError, TypeError) as exc:
        raise InvalidRecord("START_BOT target must be an @username or numeric user ID") from exc
    _positive_id(numeric_target, "START_BOT target")
    return str(numeric_target)


def validate_telegram_link(value: object) -> str | None:
    """Accept only safe HTTPS links Telegram can open in an inline URL button."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRecord("link_url must be a Telegram HTTPS URL")
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > 512 or any(character.isspace() for character in raw):
        raise InvalidRecord("link_url must be a Telegram HTTPS URL no longer than 512 characters")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise InvalidRecord("link_url must be a Telegram HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in {"t.me", "telegram.me"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path.strip("/")
    ):
        raise InvalidRecord("link_url must use https://t.me/ or https://telegram.me/")
    return raw


def _emoji_key(value: str) -> str:
    _validate_text(value, "emoji", max_length=128)
    return value


def _positive_id(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > _INT64_MAX:
        raise InvalidRecord(f"{field} must be a positive signed 64-bit integer")


def _chat_id(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0 or not -(1 << 63) <= value <= _INT64_MAX:
        raise InvalidRecord(f"{field} must be a non-zero signed 64-bit integer")


def _nonnegative_int(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _INT64_MAX:
        raise InvalidRecord(f"{field} must be a non-negative signed 64-bit integer")


def _bounded_int(value: int, minimum: int, maximum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidRecord(f"{field} must be between {minimum} and {maximum}")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
