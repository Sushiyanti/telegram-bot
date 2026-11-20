"""Network-free tests for the identity and configuration diagnostic commands."""

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from media_bot.config import Settings, configure_admin_ids
from media_bot.db.database import Database
from media_bot.handlers.diagnostics import configcheck_command, whoami_command


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="123:secret-test-token",
        storage_channel_id=-1001234567890,
        admin_ids=(991001, 991002),
        db_path=tmp_path / "state" / "bot.db",
        log_level="INFO",
        env="dev",
        max_relay_file_bytes=2_147_483_648,
        default_protect_content=False,
    )


@pytest.fixture
async def database(settings: Settings) -> Database:
    database = Database(settings.db_path)
    await database.initialize()
    return database


def make_update(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )


def make_context(settings: Settings, database: Database, bot: object) -> SimpleNamespace:
    return SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(
            bot_data={"settings": settings, "database": database}
        ),
    )


def make_passing_bot() -> SimpleNamespace:
    return SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=700001)),
        get_chat=AsyncMock(return_value=SimpleNamespace(type="channel", username=None)),
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="administrator")
        ),
    )


async def test_whoami_reports_numeric_id_and_admin_status() -> None:
    configure_admin_ids((991001,))
    update = make_update(123456789)

    await whoami_command(update, SimpleNamespace())

    update.effective_message.reply_text.assert_awaited_once_with(
        "Your user_id: 123456789\nAdmin: no"
    )


async def test_configcheck_mocked_pass_is_redacted(
    settings: Settings, database: Database, caplog
) -> None:
    configure_admin_ids(settings.admin_ids)
    bot = make_passing_bot()
    update = make_update(settings.admin_ids[0])
    context = make_context(settings, database, bot)

    with caplog.at_level(logging.INFO):
        await configcheck_command(update, context)

    output = update.effective_message.reply_text.await_args.args[0]
    assert "[PASS] Bot token valid (getMe): valid" in output
    assert "[PASS] Storage channel reachable (getChat): reachable" in output
    assert "[PASS] Bot is a storage-channel administrator: administrator" in output
    assert "[PASS] Storage channel is private" in output
    assert "2 configured; IDs redacted" in output
    assert "[PASS] Database open: open" in output
    assert "[PASS] Database schema version: version 2" in output
    assert "[PASS] Database writable: writable" in output
    assert "[PASS] MAX_RELAY_FILE_BYTES: 2147483648 bytes" in output
    assert "CONFIGCHECK_EXIT_CODE=0" in caplog.text
    assert settings.bot_token not in output
    assert str(settings.storage_channel_id) not in output
    for admin_id in settings.admin_ids:
        assert str(admin_id) not in output

    bot.get_me.assert_awaited_once()
    bot.get_chat.assert_awaited_once_with(settings.storage_channel_id)
    bot.get_chat_member.assert_awaited_once_with(
        chat_id=settings.storage_channel_id,
        user_id=700001,
    )


async def test_configcheck_mocked_fail_logs_nonzero_and_redacts_errors(
    settings: Settings, database: Database, caplog
) -> None:
    configure_admin_ids(settings.admin_ids)
    async with database.connection() as connection:
        await connection.execute("PRAGMA user_version = 1")

    bot = SimpleNamespace(
        get_me=AsyncMock(side_effect=RuntimeError("secret-test-token")),
        get_chat=AsyncMock(
            return_value=SimpleNamespace(type="channel", username="public_name")
        ),
        get_chat_member=AsyncMock(),
    )
    update = make_update(settings.admin_ids[0])
    context = make_context(settings, database, bot)

    with caplog.at_level(logging.ERROR):
        await configcheck_command(update, context)

    output = update.effective_message.reply_text.await_args.args[0]
    assert "[FAIL] Bot token valid (getMe)" in output
    assert "[FAIL] Bot is a storage-channel administrator" in output
    assert "[FAIL] Storage channel is private" in output
    assert "[FAIL] Database schema version: expected 2; found 1" in output
    assert "[PASS] Database writable: writable" in output
    assert "CONFIGCHECK_EXIT_CODE=1" in caplog.text
    assert "secret-test-token" not in output
    assert "secret-test-token" not in caplog.text
    assert str(settings.storage_channel_id) not in output
