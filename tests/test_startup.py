"""Tests for local startup prerequisites and PTB application construction."""

import os
import subprocess
import sys
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
        storage_channel_id=-1001234567890,
        admin_ids=(123456789,),
        db_path=tmp_path / "data" / "bot.db",
        log_level="INFO",
        env="dev",
        max_relay_file_bytes=2_147_483_648,
        default_protect_content=False,
    )


def test_prepare_state_directory_creates_writable_parent(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "state" / "bot.db"

    prepare_state_directory(database_path)

    assert database_path.parent.is_dir()
    assert list(database_path.parent.iterdir()) == []


def test_prepare_state_directory_rejects_directory_as_database_file(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bot.db"
    database_path.mkdir()

    with pytest.raises(StartupPrerequisiteError, match="DB_PATH"):
        prepare_state_directory(database_path)


def test_application_builds_without_network_access(settings: Settings) -> None:
    application = build_application(settings)

    assert isinstance(application, Application)
    commands = {
        command
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert commands == {"start", "whoami", "configcheck"}
    assert application.bot_data["settings"] is settings
    assert application.bot_data["database"].path == settings.db_path


def test_python_m_app_fails_clearly_when_required_configuration_is_missing(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    for key in ("BOT_TOKEN", "STORAGE_CHANNEL_ID", "ADMIN_IDS"):
        environment.pop(key, None)
    environment["PYTHONPATH"] = str(project_root / "src")

    result = subprocess.run(
        [sys.executable, "-m", "app"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 2
    assert "BOT_TOKEN" in result.stderr
    assert "STORAGE_CHANNEL_ID" in result.stderr
    assert "ADMIN_IDS" in result.stderr
