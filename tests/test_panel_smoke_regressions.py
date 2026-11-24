"""Integration regressions for the second Campaigns panel smoke test."""
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import CallbackQuery, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, MessageHandler, filters

from media_bot.auth import is_admin
from media_bot.config import Settings, configure_admin_ids
from media_bot.db.database import Database
from media_bot.db.models import MediaItem, Offer, RequiredChat
from media_bot.db.repositories import ItemRepository, OfferRepository, OfferRequirementRepository, RequiredChatRepository
from media_bot.handlers import panel
from media_bot.media_transfer import StorageMessageRef
from media_bot.services.core import hash_share_token

OWNER = 9001
STORAGE = -1001234567890


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "bot.sqlite3")
    await db.initialize()
    return db


@pytest.fixture(autouse=True)
def patch_callback_network_methods(monkeypatch):
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    monkeypatch.setattr(CallbackQuery, "edit_message_text", AsyncMock())


async def make_offer(db: Database, slug: str) -> Offer:
    item = await ItemRepository(db).create_stored_item(
        MediaItem(owner_user_id=OWNER, media_type="document", file_size_bytes=10,
                  storage_messages=(StorageMessageRef(STORAGE, abs(hash(slug)) % 1_000_000 + 1),), file_name=f"{slug}.bin")
    )
    return await OfferRepository(db).create(
        Offer(0, item.id, hash_share_token(slug), None, slug, None, True, OWNER, 1, 1, slug)
    )


def settings(db: Database) -> Settings:
    return Settings(bot_token="123:test", storage_channel_id=STORAGE, admin_ids=(OWNER,),
                    db_path=db.path, log_level="INFO", env="dev", max_relay_file_bytes=10_000,
                    default_protect_content=False)


def callback_update(data: str) -> Update:
    return Update.de_json({"update_id": 10, "callback_query": {
        "id": "q", "from": {"id": OWNER, "is_bot": False, "first_name": "Admin"},
        "chat_instance": "ci", "data": data,
        "message": {"message_id": 77, "date": 1,
                     "chat": {"id": OWNER, "type": "private"}, "text": "panel"},
    }}, bot=None)


def message_update(text: str, update_id: int = 20) -> Update:
    return Update.de_json({"update_id": update_id, "message": {
        "message_id": update_id, "date": 1,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "Admin"},
        "text": text,
    }}, bot=None)


def context_for(db: Database, state: dict, bot=None):
    state.setdefault("pending_last_activity", time.monotonic())
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"database": db, "settings": settings(db), "bot_username": "MarketingBot"}),
        user_data={panel._PANEL_KEY: state}, bot=bot,
    )


def bind_callback(update: Update):
    return update


async def run_message_handler(update: Update, context) -> None:
    try:
        await MessageHandler(filters.ALL, panel.admin_panel_input).callback(update, context)
    except ApplicationHandlerStop:
        pass


@pytest.mark.asyncio
async def test_pa1_picker_negative_chat_id_parses_and_inserts(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer = await make_offer(database, "pa1")
    cid = -1001234567890
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "One", "one", True, 1, 1, "https://t.me/one"))
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign"}
    update = bind_callback(callback_update(f"m6:n:cmpchat:{offer.id}:{cid}"))
    handler = CallbackQueryHandler(panel.admin_panel_callback, pattern=r"^m6:")
    await handler.callback(update, context_for(database, state))
    assert [x.chat_id for x in await OfferRequirementRepository(database).list_required_chats(offer.id)] == [cid]


@pytest.mark.asyncio
async def test_pa2_picker_large_row_id_parses_correctly(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer = await make_offer(database, "pa2")
    cid = 1234567890123456789
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "Large", "large", True, 1, 1, "https://t.me/large"))
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign"}
    update = bind_callback(callback_update(f"m6:n:cmpchat:{offer.id}:{cid}"))
    await CallbackQueryHandler(panel.admin_panel_callback, pattern=r"^m6:").callback(update, context_for(database, state))
    assert (await OfferRequirementRepository(database).list_required_chats(offer.id))[0].chat_id == cid


@pytest.mark.asyncio
async def test_pa3_malformed_callback_is_safe_and_shows_stale_button_message(database: Database) -> None:
    configure_admin_ids((OWNER,))
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign"}
    update = bind_callback(callback_update("m6:n:cmpchat:bad:not-an-id"))
    await CallbackQueryHandler(panel.admin_panel_callback, pattern=r"^m6:").callback(update, context_for(database, state))
    assert "out of date" in update.callback_query.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_pa4_full_picker_flow_adds_existing_channel_to_offer_b(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer_a, offer_b = await make_offer(database, "pa4-a"), await make_offer(database, "pa4-b")
    cid = -1001234567890
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "One", "one", True, 1, 1, "https://t.me/one"))
    await OfferRequirementRepository(database).add_required_chat(offer_a.id, cid)
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign"}
    update = bind_callback(callback_update(f"m6:n:cmpchat:{offer_b.id}:{cid}"))
    await CallbackQueryHandler(panel.admin_panel_callback, pattern=r"^m6:").callback(update, context_for(database, state))
    assert len(await OfferRequirementRepository(database).list_required_chats(offer_b.id)) == 1


@pytest.mark.asyncio
async def test_pb1_wizard_same_name_has_one_registry_row_and_offer_link(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer = await make_offer(database, "pb1")
    cid = -1001234567890
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "One", "one", True, 1, 1, "https://t.me/one"))
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign",
             "input": {"kind": "campaign_chat"}, "wizard": {"offer_id": offer.id, "chat_step": "invite_url", "identifier": "@one", "display_name": "One"}}
    bot = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=cid, type="channel", username="one", title="One")))
    await run_message_handler(message_update("https://t.me/one"), context_for(database, state, bot))
    assert len(await RequiredChatRepository(database).list_enabled()) == 1
    assert len(await OfferRequirementRepository(database).list_required_chats(offer.id)) == 1


@pytest.mark.asyncio
async def test_pb2_wizard_changed_name_upserts_one_final_registry_row(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer = await make_offer(database, "pb2")
    cid = -1001234567890
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "Old", "one", True, 1, 1, "https://t.me/old"))
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign",
             "input": {"kind": "campaign_chat"}, "wizard": {"offer_id": offer.id, "chat_step": "invite_url", "identifier": "@one", "display_name": "New"}}
    bot = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=cid, type="channel", username="one", title="Old")))
    await run_message_handler(message_update("https://t.me/new"), context_for(database, state, bot))
    rows = await RequiredChatRepository(database).list_enabled()
    assert len(rows) == 1 and rows[0].title == "New"
    assert len(await OfferRequirementRepository(database).list_required_chats(offer.id)) == 1


@pytest.mark.asyncio
async def test_pb3_bad_invite_url_has_clear_error_and_no_partial_row(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer = await make_offer(database, "pb3")
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign",
             "input": {"kind": "campaign_chat"}, "wizard": {"offer_id": offer.id, "chat_step": "invite_url", "identifier": "@new", "display_name": "New"}}
    update = message_update("not-a-telegram-url")
    context = context_for(database, state, SimpleNamespace(get_chat=AsyncMock()))
    await run_message_handler(update, context)
    assert await RequiredChatRepository(database).list_enabled() == ()


@pytest.mark.asyncio
async def test_pb4_get_chat_failure_has_access_error_and_no_partial_row(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer = await make_offer(database, "pb4")
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign",
             "input": {"kind": "campaign_chat"}, "wizard": {"offer_id": offer.id, "chat_step": "invite_url", "identifier": "@missing", "display_name": "Missing"}}
    query_bot = SimpleNamespace(get_chat=AsyncMock(side_effect=RuntimeError("network")))
    context = context_for(database, state, query_bot)
    await run_message_handler(message_update("https://t.me/missing"), context)
    assert await RequiredChatRepository(database).list_enabled() == ()
