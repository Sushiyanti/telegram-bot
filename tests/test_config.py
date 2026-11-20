"""Tests for environment-backed settings."""

from pathlib import Path

import pytest

from media_bot.config import ConfigurationError, Settings


VALID_ENV = {
    "BOT_TOKEN": "123:dummy",
    "STORAGE_CHANNEL_ID": "-1001234567890",
    "ADMIN_IDS": "123456789, 987654321",
}


def test_settings_parse_required_values() -> None:
    settings = Settings.from_env(VALID_ENV)

    assert settings.admin_ids == (123456789, 987654321)
    assert settings.storage_channel_id == -1001234567890
    assert settings.db_path == Path("data/bot.db")
    assert settings.log_level == "INFO"
    assert settings.env == "dev"
    assert settings.max_relay_file_bytes == 2_147_483_648
    assert settings.default_protect_content is False


def test_settings_parse_optional_content_protection() -> None:
    settings = Settings.from_env(
        {
            **VALID_ENV,
            "DB_PATH": "~/bot-state/bot.db",
            "LOG_LEVEL": "debug",
            "ENV": "PROD",
            "MAX_RELAY_FILE_BYTES": "987654321",
            "DEFAULT_PROTECT_CONTENT": "TRUE",
        }
    )

    assert settings.db_path == Path("~/bot-state/bot.db").expanduser()
    assert settings.log_level == "DEBUG"
    assert settings.env == "prod"
    assert settings.max_relay_file_bytes == 987654321
    assert settings.default_protect_content is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BOT_TOKEN", ""),
        ("BOT_TOKEN", "not-a-token"),
        ("BOT_TOKEN", "123456:"),
        ("BOT_TOKEN", "123456:secret:extra"),
        ("STORAGE_CHANNEL_ID", ""),
        ("STORAGE_CHANNEL_ID", "@private_channel"),
        ("STORAGE_CHANNEL_ID", "1.2"),
        ("STORAGE_CHANNEL_ID", "0"),
        ("STORAGE_CHANNEL_ID", "9" * 5000),
        ("ADMIN_IDS", ""),
        ("ADMIN_IDS", "123,not-numeric"),
        ("ADMIN_IDS", "@somebody"),
        ("ADMIN_IDS", "123,,456"),
        ("ADMIN_IDS", "123,123"),
        ("ADMIN_IDS", "9" * 5000),
        ("DB_PATH", " "),
        ("LOG_LEVEL", "VERBOSE"),
        ("ENV", "staging"),
        ("MAX_RELAY_FILE_BYTES", "NaN"),
        ("MAX_RELAY_FILE_BYTES", "0"),
        ("MAX_RELAY_FILE_BYTES", "9" * 5000),
        ("DEFAULT_PROTECT_CONTENT", "sometimes"),
    ],
)
def test_settings_reject_invalid_values(key: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=key):
        Settings.from_env({**VALID_ENV, key: value})


def test_missing_required_values_are_all_listed() -> None:
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env({})

    message = str(error.value)
    assert "BOT_TOKEN" in message
    assert "STORAGE_CHANNEL_ID" in message
    assert "ADMIN_IDS" in message


def test_all_invalid_keys_are_listed_without_echoing_values() -> None:
    token = "not-a-token-secret"
    invalid_env = {
        "BOT_TOKEN": token,
        "STORAGE_CHANNEL_ID": "@private_channel",
        "ADMIN_IDS": "@admin",
        "DB_PATH": " ",
        "LOG_LEVEL": "VERBOSE",
        "ENV": "staging",
        "MAX_RELAY_FILE_BYTES": "unlimited",
    }

    with pytest.raises(ConfigurationError) as error:
        Settings.from_env(invalid_env)

    message = str(error.value)
    for key in invalid_env:
        assert key in message
    assert token not in message
    assert "@private_channel" not in message
    assert "@admin" not in message


def test_configuration_error_never_includes_token_value() -> None:
    dummy_value = "123:example"

    with pytest.raises(ConfigurationError) as error:
        Settings.from_env({**VALID_ENV, "BOT_TOKEN": dummy_value, "ADMIN_IDS": "bad"})

    assert dummy_value not in str(error.value)


def test_dotenv_file_is_loaded_from_working_directory(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "BOT_TOKEN=123:dotenv\n"
        "STORAGE_CHANNEL_ID=-100123\n"
        "ADMIN_IDS=123,456\n"
        "MAX_RELAY_FILE_BYTES=1000000\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for key in (
        "BOT_TOKEN",
        "STORAGE_CHANNEL_ID",
        "ADMIN_IDS",
        "MAX_RELAY_FILE_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings.from_env()

    assert settings.admin_ids == (123, 456)
    assert settings.storage_channel_id == -100123
    assert settings.max_relay_file_bytes == 1_000_000
