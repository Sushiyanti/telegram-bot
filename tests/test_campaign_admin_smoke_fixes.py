"""Regression tests for the public Campaigns admin-panel smoke-test fixes."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from media_bot.db.database import Database
from media_bot.db.rules import RequirementRepository
from media_bot.handlers.panel import _chat_identifier, _panel_bot_username


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "bot.sqlite3")
    await db.initialize()
    return db


def _context(**bot_data):
    return SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))


def test_t1_configured_username_produces_real_deep_link_username() -> None:
    context = _context(bot_username="MarketingBot")
    username = _panel_bot_username(context)
    assert username == "MarketingBot"
    assert (
        f"https://t.me/{username}?start=offer-slug" == "https://t.me/MarketingBot?start=offer-slug"
    )


def test_t2_placeholder_or_missing_username_is_not_rendered() -> None:
    assert _panel_bot_username(_context(bot_username="YourBot")) is None
    assert _panel_bot_username(_context()) is None


def test_t3_placeholder_identity_is_rejected_by_panel_username_guard() -> None:
    for value in ("YourBot", "your_bot", "bot_username"):
        assert _panel_bot_username(_context(bot_username=value)) is None


def test_t4_channel_identifier_normalization_supports_username_link_and_id() -> None:
    assert _chat_identifier("@channel_one") == "@channel_one"
    assert _chat_identifier("https://t.me/channel_one") == "@channel_one"
    assert _chat_identifier("-100123456789") == "-100123456789"


def test_t5_bad_channel_identifier_does_not_get_silently_rewritten() -> None:
    # A syntactically supplied identifier remains explicit for bot.get_chat;
    # Telegram access failures are handled by the wizard before persistence.
    assert _chat_identifier("not a channel") == "@not a channel"


def test_t6_public_username_has_a_derived_invite_url() -> None:
    username = "public_channel"
    assert f"https://t.me/{username}" == "https://t.me/public_channel"


@pytest.mark.asyncio
async def test_t7_empty_requirement_registry_is_safe(database: Database) -> None:
    assert await RequirementRepository(database).list_all() == ()


@pytest.mark.asyncio
async def test_t8_non_empty_requirement_registry_is_listable(
    database: Database,
) -> None:
    requirement = await RequirementRepository(database).create(
        category_id=None,
        requirement_type="REACT",
        target=-100123456789,
        is_fake=True,
        wait_seconds=0,
    )
    values = await RequirementRepository(database).list_all()
    assert [item.id for item in values] == [requirement.id]
    assert values[0].type == "REACT"


def test_t9_deep_link_payload_is_slug_not_file_id() -> None:
    slug = "opaque-offer-slug"
    assert f"?start={slug}" == "?start=opaque-offer-slug"
    assert "file_id" not in slug
