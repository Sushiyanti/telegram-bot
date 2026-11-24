"""Phase 5 closeout coverage for deprecation, privacy, and reporting boundaries."""

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from media_bot.config import Settings
from media_bot.db.database import Database
from media_bot.db.models import MediaItem, Offer
from media_bot.db.models import RequiredChat
from media_bot.db.repositories import (
    ItemRepository,
    OfferEventRepository,
    OfferRepository,
    OfferRequirementRepository,
    RequiredChatRepository,
)
from media_bot.handlers.admin import stats_command
from media_bot.handlers.panel import _create_campaign
from media_bot.handlers.sharing import start_command
from media_bot.media_transfer import StorageMessageRef
from media_bot.services.core import hash_share_token
from media_bot.startup import run_legacy_required_channel_import

STORAGE = -100123
OWNER = 123456789


def env(**overrides: str) -> dict[str, str]:
    values = {
        "BOT_TOKEN": "123:dummy",
        "STORAGE_CHANNEL_ID": str(STORAGE),
        "ADMIN_IDS": str(OWNER),
    }
    values.update(overrides)
    return values


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "bot.db")
    await database.initialize(storage_channel_id=STORAGE)
    return database


async def make_item(database: Database) -> int:
    item = await ItemRepository(database).create_stored_item(
        MediaItem(
            owner_user_id=OWNER,
            media_type="document",
            file_size_bytes=10,
            storage_messages=(StorageMessageRef(STORAGE, 101, 0),),
            file_name="offer.bin",
        )
    )
    return item.id


async def make_join_offer(database: Database, slug: str) -> Offer:
    item_id = await make_item(database)
    offer = await OfferRepository(database).create(
        Offer(0, item_id, hash_share_token(slug), None, "Gated campaign", None, True, OWNER, 1, 1, slug)
    )
    await RequiredChatRepository(database).upsert(
        RequiredChat(STORAGE - 1, "channel", "Required channel", "required", True, 1, 1, "https://t.me/required")
    )
    await OfferRequirementRepository(database).add_required_chat(offer.id, STORAGE - 1)
    return offer


def make_settings(database: Database) -> Settings:
    return Settings(
        bot_token="123:dummy",
        storage_channel_id=STORAGE,
        admin_ids=(OWNER,),
        db_path=database.path,
        log_level="DEBUG",
        env="dev",
        max_relay_file_bytes=10_000,
        default_protect_content=False,
        verified_transfer_max_file_bytes=10_000,
    )


def test_t1_legacy_import_defaults_disabled() -> None:
    settings = Settings.from_env(env(REQUIRED_CHANNEL_ID="-100999"))
    assert settings.required_channel_id == -100999
    assert settings.legacy_channel_import_enabled is False


def test_t2_legacy_import_flag_is_strict_boolean() -> None:
    settings = Settings.from_env(
        env(REQUIRED_CHANNEL_ID="-100999", LEGACY_CHANNEL_IMPORT_ENABLED="true")
    )
    assert settings.legacy_channel_import_enabled is True
    with pytest.raises(ValueError, match="LEGACY_CHANNEL_IMPORT_ENABLED"):
        Settings.from_env(env(LEGACY_CHANNEL_IMPORT_ENABLED="yes"))


@pytest.mark.asyncio
async def test_t3_disabled_legacy_import_logs_exact_warning_once(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert await run_legacy_required_channel_import(database, -100999) == 0
    matches = [
        record for record in caplog.records
        if record.getMessage()
        == "REQUIRED_CHANNEL_ID is set but ignored; offer-level requirements are authoritative. See README."
    ]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_t4_stats_window_is_fixed_and_bounded(database: Database) -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    context = SimpleNamespace(args=[], application=SimpleNamespace(bot_data={"database": database}))
    await stats_command(SimpleNamespace(effective_message=message), context)
    assert "aggregate, last 30 days" in message.reply_text.await_args.args[0]



def test_t5_public_start_path_hashes_before_lookup() -> None:
    source = Path("src/media_bot/handlers/sharing.py").read_text()
    assert "get_by_slug_hash(" in source
    assert "get_by_slug_plain(" not in source


@pytest.mark.asyncio
async def test_a4a_panel_creation_audits_without_plain_slug(database: Database) -> None:
    item_id = await make_item(database)
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    context = SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(bot_data={"database": database, "settings": make_settings(database)}),
    )
    state = {
        "owner_user_id": OWNER,
        "chat_id": OWNER,
        "message_id": 1,
        "nonce": "nonce",
        "wizard": {"item_id": item_id, "title": "Campaign title"},
    }
    await _create_campaign(context, state)
    offer = (await OfferRepository(database).list_active())[0]
    async with database.connection() as connection:
        rows = await (await connection.execute("SELECT * FROM audit_log")).fetchall()
    assert rows and rows[-1]["action"] == "offer_created"
    assert offer.slug_plain not in str(rows)


@pytest.mark.asyncio
async def test_a4b_stats_aggregate_and_offer_output_exclude_slug_secrets(
    database: Database,
) -> None:
    item_id = await make_item(database)
    slug = "private_campaign_slug"
    offer = await OfferRepository(database).create(
        Offer(0, item_id, hash_share_token(slug), None, "Public campaign", None, True, OWNER, 1, 1, slug)
    )
    now_ms = time.time_ns() // 1_000_000
    await OfferEventRepository(database).append(offer.id, OWNER, "start", at_ms=now_ms)
    await OfferEventRepository(database).append(offer.id, OWNER, "unlock", at_ms=now_ms + 1)
    for args in ([], [str(offer.id)]):
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(args=args, application=SimpleNamespace(bot_data={"database": database}))
        await stats_command(SimpleNamespace(effective_message=message), context)
        text = message.reply_text.await_args.args[0]
        assert slug not in text
        assert hash_share_token(slug).hex() not in text


@pytest.mark.asyncio
async def test_a4c_start_plain_slug_end_to_end_has_no_slug_in_debug_logs(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    item_id = await make_item(database)
    slug = "debug_private_slug"
    await OfferRepository(database).create(
        Offer(0, item_id, hash_share_token(slug), None, "Debug campaign", None, True, OWNER, 1, 1, slug)
    )
    message = SimpleNamespace(reply_text=AsyncMock())
    bot = SimpleNamespace(copy_message=AsyncMock())
    context = SimpleNamespace(
        args=[slug],
        bot=bot,
        chat_data={},
        application=SimpleNamespace(bot_data={"database": database, "settings": make_settings(database)}),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=OWNER),
        effective_chat=SimpleNamespace(type="private", id=OWNER),
        effective_message=message,
    )
    with caplog.at_level(logging.DEBUG, logger="media_bot.handlers.sharing"):
        await start_command(update, context)
    assert message.reply_text.await_args.args[0] == "Delivered."
    assert all(slug not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_t5_start_offer_gate_api_error_fails_closed(database: Database) -> None:
    offer = await make_join_offer(database, "start_error_slug")
    message = SimpleNamespace(reply_text=AsyncMock())
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(side_effect=TelegramError("network")),
        copy_message=AsyncMock(),
    )
    context = SimpleNamespace(
        args=["start_error_slug"],
        bot=bot,
        chat_data={},
        application=SimpleNamespace(bot_data={"database": database, "settings": make_settings(database)}),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=OWNER),
        effective_chat=SimpleNamespace(type="private", id=OWNER),
        effective_message=message,
    )
    await start_command(update, context)
    assert bot.copy_message.await_count == 0
    text = message.reply_text.await_args.args[0]
    assert "unavailable" in text.lower()
    async with database.connection() as connection:
        rows = await (await connection.execute(
            "SELECT event_type FROM offer_events WHERE offer_id = ? ORDER BY id", (offer.id,)
        )).fetchall()
    event_types = [row["event_type"] for row in rows]
    assert event_types == ["start"]
    assert "unlock" not in event_types
    assert "delivery" not in event_types


@pytest.mark.asyncio
async def test_t5_verify_callback_gate_api_error_fails_closed(database: Database) -> None:
    offer = await make_join_offer(database, "verify_error_slug")
    message = SimpleNamespace(reply_text=AsyncMock())
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")),
        copy_message=AsyncMock(),
    )
    context = SimpleNamespace(
        args=["verify_error_slug"],
        bot=bot,
        chat_data={},
        application=SimpleNamespace(bot_data={"database": database, "settings": make_settings(database)}),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=OWNER),
        effective_chat=SimpleNamespace(type="private", id=OWNER),
        effective_message=message,
    )
    await start_command(update, context)
    markup = message.reply_text.await_args.kwargs["reply_markup"]
    verify_data = next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("verify:")
    )
    bot.get_chat_member = AsyncMock(side_effect=TelegramError("network"))
    query = SimpleNamespace(
        data=verify_data,
        from_user=SimpleNamespace(id=OWNER),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=message,
    )
    from media_bot.handlers.verify import verify_callback

    await verify_callback(SimpleNamespace(callback_query=query), context)
    assert bot.copy_message.await_count == 0
    assert "unavailable" in query.edit_message_text.await_args.args[0].lower()
    async with database.connection() as connection:
        rows = await (await connection.execute(
            "SELECT event_type FROM offer_events WHERE offer_id = ? ORDER BY id", (offer.id,)
        )).fetchall()
    event_types = [row["event_type"] for row in rows]
    assert event_types == ["start"]
    assert "unlock" not in event_types
    assert "delivery" not in event_types
