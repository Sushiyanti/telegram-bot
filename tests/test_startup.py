"""Tests for local startup prerequisites and PTB application construction."""

from pathlib import Path

import pytest
from telegram.ext import Application, CommandHandler

from media_bot.application import build_application
from media_bot.config import Settings
from media_bot.startup import StartupPrerequisiteError, prepare_state_directory


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="123:dummy",
        admin_ids=(123456789,),
        storage_chat_id=-1001234567890,
        database_path=tmp_path / "data" / "bot.sqlite3",
        log_level="INFO",
        default_protect_content=False,
    )


def test_prepare_state_directory_creates_writable_parent(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "state" / "bot.sqlite3"

    prepare_state_directory(database_path)

    assert database_path.parent.is_dir()
    assert list(database_path.parent.iterdir()) == []


def test_prepare_state_directory_rejects_directory_as_database_file(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bot.sqlite3"
    database_path.mkdir()

    with pytest.raises(StartupPrerequisiteError, match="not a file"):
        prepare_state_directory(database_path)


def test_application_builds_without_network_access(settings: Settings) -> None:
    application = build_application(settings)

    assert isinstance(application, Application)
    assert any(
        isinstance(handler, CommandHandler)
        for handlers in application.handlers.values()
        for handler in handlers
    )
