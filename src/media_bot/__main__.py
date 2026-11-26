"""Command-line entry point for the Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys

from media_bot.application import build_application
from media_bot.config import ConfigurationError, Settings
from media_bot.db.database import Database, DatabaseError
from media_bot.logging_config import configure_logging
from media_bot.startup import (
    StartupPrerequisiteError,
    prepare_state_directory,
    run_legacy_required_channel_import,
)

logger = logging.getLogger(__name__)


def main() -> None:
    """Validate local prerequisites and start the bot using long polling."""
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    configure_logging(settings.log_level)
    logger.info(
        "bot_runtime_instance hostname=%s pid=%s db_path=%s",
        socket.gethostname(),
        os.getpid(),
        settings.db_path,
    )
    try:
        prepare_state_directory(settings.db_path)
    except StartupPrerequisiteError as exc:
        logger.error("Startup prerequisite failed: %s", exc)
        raise SystemExit(2) from exc

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            Database(settings.db_path).initialize(storage_channel_id=settings.storage_channel_id)
        )
        loop.run_until_complete(
            run_legacy_required_channel_import(
                Database(settings.db_path),
                settings.required_channel_id,
                enabled=settings.legacy_channel_import_enabled,
            )
        )
    except DatabaseError as exc:
        logger.error("Database initialization failed: %s", exc)
        loop.close()
        asyncio.set_event_loop(None)
        raise SystemExit(2) from exc

    application = build_application(settings)
    logger.info("Starting Telegram bot in long-polling mode")
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
