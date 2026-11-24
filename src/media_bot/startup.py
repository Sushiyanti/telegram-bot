"""Local startup prerequisite checks."""

from __future__ import annotations

import tempfile
import logging
from pathlib import Path

from media_bot.db.database import Database
from media_bot.db.models import RequiredChat
from media_bot.db.repositories import OfferRequirementRepository, OfferRepository, RequiredChatRepository


logger = logging.getLogger(__name__)


class StartupPrerequisiteError(RuntimeError):
    """Raised when local filesystem prerequisites are not met."""


async def run_legacy_required_channel_import(
    database: Database,
    required_channel_id: int | None,
    *,
    enabled: bool = False,
) -> int:
    """Apply the opt-in compatibility migration, or explicitly ignore it."""
    if required_channel_id is None:
        return 0
    if not enabled:
        logger.warning(
            "REQUIRED_CHANNEL_ID is set but ignored; offer-level requirements are authoritative. See README."
        )
        return 0
    return await import_legacy_required_channel(database, required_channel_id)


async def import_legacy_required_channel(
    database: Database, required_channel_id: int | None
) -> int:
    """Import the legacy global channel into active offers at most once.

    The marker is durable in ``bot_settings``.  A configured legacy value still
    produces a deprecation warning on every boot, but successful import is not
    repeated after the marker becomes ``done``.
    """
    if required_channel_id is None:
        return 0
    logger.warning(
        "REQUIRED_CHANNEL_ID is deprecated; migrate requirements to per-offer chat assignments"
    )
    if isinstance(required_channel_id, bool) or not isinstance(required_channel_id, int) or required_channel_id == 0:
        raise ValueError("required_channel_id must be a non-zero integer")

    async with database.connection() as connection:
        await connection.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('REQUIRED_CHANNEL_IMPORT_V1', 'pending')"
        )
        row = await (await connection.execute(
            "SELECT value FROM bot_settings WHERE key = 'REQUIRED_CHANNEL_IMPORT_V1'"
        )).fetchone()
    if row is not None and str(row["value"]) == "done":
        return 0

    now_ms = __import__("time").time_ns() // 1_000_000
    await RequiredChatRepository(database).upsert(
        RequiredChat(
            chat_id=required_channel_id,
            chat_type="channel",
            title="Legacy required channel",
            is_enabled=True,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
    )
    offers = await OfferRepository(database).list_active()
    assignments = OfferRequirementRepository(database)
    imported = 0
    for offer in offers:
        existing = await assignments.list_required_chats(offer.id)
        if any(entry.chat_id == required_channel_id for entry in existing):
            continue
        await assignments.add_required_chat(offer.id, required_channel_id)
        imported += 1

    async with database.connection() as connection:
        await connection.execute(
            "UPDATE bot_settings SET value = 'done' WHERE key = 'REQUIRED_CHANNEL_IMPORT_V1'"
        )
    return imported


def prepare_state_directory(database_path: Path) -> None:
    """Create the database parent directory and verify it is writable."""
    if database_path.exists() and not database_path.is_file():
        raise StartupPrerequisiteError("DB_PATH exists but is not a file")

    state_directory = database_path.parent
    try:
        state_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".telegram-storage-bot-write-test-",
            dir=state_directory,
            delete=True,
        ) as test_file:
            test_file.write(b"ok")
            test_file.flush()
    except OSError as exc:
        raise StartupPrerequisiteError(
            "The database directory could not be created or is not writable"
        ) from exc
