"""Tests for environment-backed settings."""

from pathlib import Path

import pytest

from media_bot.config import ConfigurationError, Settings


VALID_ENV = {
    "BOT_TOKEN": "123:dummy",
    "ADMIN_IDS": "123456789, 987654321",
    "STORAGE_CHAT_ID": "-1001234567890",
    "DATABASE_PATH": "./data/bot.sqlite3",
}


def test_settings_parse_required_values() -> None:
    settings = Settings.from_env(VALID_ENV)

    assert settings.admin_ids == (123456789, 987654321)
    assert settings.storage_chat_id == -1001234567890
    assert settings.database_path == Path("data/bot.sqlite3")
    assert settings.log_level == "INFO"
    assert settings.default_protect_content is False


def test_settings_parse_optional_content_protection() -> None:
    settings = Settings.from_env(
        {**VALID_ENV, "DEFAULT_PROTECT_CONTENT": "TRUE", "LOG_LEVEL": "debug"}
    )

    assert settings.default_protect_content is True
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BOT_TOKEN", ""),
        ("BOT_TOKEN", "not-a-token"),
        ("BOT_TOKEN", "123456:"),
        ("BOT_TOKEN", "123456:secret:extra"),
        ("ADMIN_IDS", ""),
        ("ADMIN_IDS", "123,not-numeric"),
        ("ADMIN_IDS", "123,123"),
        ("STORAGE_CHAT_ID", "@private_channel"),
        ("STORAGE_CHAT_ID", "0"),
        ("LOG_LEVEL", "VERBOSE"),
        ("DEFAULT_PROTECT_CONTENT", "sometimes"),
    ],
)
def test_settings_reject_invalid_values(key: str, value: str) -> None:
    invalid_env = {**VALID_ENV, key: value}

    with pytest.raises(ConfigurationError):
        Settings.from_env(invalid_env)


def test_configuration_error_never_includes_token_value() -> None:
    dummy_value = "123:example"
    invalid_env = {**VALID_ENV, "BOT_TOKEN": dummy_value, "ADMIN_IDS": "bad"}

    with pytest.raises(ConfigurationError) as error:
        Settings.from_env(invalid_env)

    assert dummy_value not in str(error.value)
