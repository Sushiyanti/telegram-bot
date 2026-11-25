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
from media_bot.db.rules import RequirementRepository

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


@pytest.mark.asyncio
async def test_d1_duplicate_required_chat_returns_false_without_duplicate(database: Database) -> None:
    offer = await make_offer(database, "d1")
    cid = -1001234567890
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "One", "one", True, 1, 1, "https://t.me/one"))
    repo = OfferRequirementRepository(database)
    assert await repo.add_required_chat(offer.id, cid) is True
    assert await repo.add_required_chat(offer.id, cid) is False
    assert len(await repo.list_required_chats(offer.id)) == 1


@pytest.mark.asyncio
async def test_d2_same_channel_can_link_to_two_offers(database: Database) -> None:
    first, second = await make_offer(database, "d2-a"), await make_offer(database, "d2-b")
    cid = -1001234567890
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "One", "one", True, 1, 1, "https://t.me/one"))
    repo = OfferRequirementRepository(database)
    assert await repo.add_required_chat(first.id, cid) is True
    assert await repo.add_required_chat(second.id, cid) is True
    assert len(await repo.list_required_chats(first.id)) == len(await repo.list_required_chats(second.id)) == 1


@pytest.mark.asyncio
async def test_d3_duplicate_wizard_link_shows_graceful_notice(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer = await make_offer(database, "d3")
    cid = -1001234567890
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "Old", "one", True, 1, 1, "https://t.me/old"))
    await OfferRequirementRepository(database).add_required_chat(offer.id, cid)
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign",
             "input": {"kind": "campaign_chat"}, "wizard": {"offer_id": offer.id, "chat_step": "invite_url", "identifier": "@one", "display_name": "New"}}
    bot = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=cid, type="channel", username="one", title="Old")), edit_message_text=AsyncMock(), send_message=AsyncMock())
    await run_message_handler(message_update("https://t.me/new"), context_for(database, state, bot))
    assert "already required" in bot.edit_message_text.await_args.kwargs["text"]
    assert len(await OfferRequirementRepository(database).list_required_chats(offer.id)) == 1


@pytest.mark.asyncio
async def test_d4_duplicate_wizard_name_change_is_explicitly_reported(database: Database) -> None:
    configure_admin_ids((OWNER,))
    offer = await make_offer(database, "d4")
    cid = -1001234567890
    await RequiredChatRepository(database).upsert(RequiredChat(cid, "channel", "Old", "one", True, 1, 1, "https://t.me/old"))
    await OfferRequirementRepository(database).add_required_chat(offer.id, cid)
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign",
             "input": {"kind": "campaign_chat"}, "wizard": {"offer_id": offer.id, "chat_step": "invite_url", "identifier": "@one", "display_name": "New"}}
    bot = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=cid, type="channel", username="one", title="Old")), edit_message_text=AsyncMock(), send_message=AsyncMock())
    await run_message_handler(message_update("https://t.me/new"), context_for(database, state, bot))
    row = (await RequiredChatRepository(database).list_enabled())[0]
    assert row.title == "New"
    assert "Display name updated" in bot.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_user_data_none_channel_post_does_not_crash() -> None:
    from media_bot.handlers.panel import expire_panel_flow_before_command
    update = Update.de_json({"update_id": 99, "channel_post": {"message_id": 1, "date": 1, "chat": {"id": -1001, "type": "channel"}, "text": "/start"}}, bot=None)
    await expire_panel_flow_before_command(update, SimpleNamespace(user_data=None))


@pytest.mark.asyncio
async def test_panel_callback_answer_timeout_does_not_abort_dispatch(database: Database, monkeypatch) -> None:
    configure_admin_ids((OWNER,))
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "home"}
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock(side_effect=TimeoutError("callback expired")))
    edit_message_text = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "edit_message_text", edit_message_text)
    update = callback_update("m6:n:home")
    await CallbackQueryHandler(panel.admin_panel_callback, pattern=r"^m6:").callback(update, context_for(database, state))
    edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_r1_empty_campaign_requirement_picker_prompts_inline_creation(database: Database) -> None:
    offer = await make_offer(database, "r1")
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign"}
    query = SimpleNamespace(edit_message_text=AsyncMock())
    context = context_for(database, state, SimpleNamespace())
    await panel._show_campaign_requirement_picker(context, state, offer.id, query=query)
    assert "Reply 'new'" in query.edit_message_text.await_args.kwargs["text"]
    assert state["input"]["kind"] == "campaign_requirement_new"


@pytest.mark.asyncio
async def test_r2_inline_new_starts_react_requirement_wizard(database: Database) -> None:
    offer = await make_offer(database, "r2")
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "campaign",
             "input": {"kind": "campaign_requirement_new"}, "wizard": {"offer_id": offer.id}}
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    await run_message_handler(message_update("new"), context_for(database, state, bot))
    assert state["wizard"]["campaign_offer_id"] == offer.id
    assert state["wizard"]["step"] == "type"
    assert state["input"] is None
    assert state["view"] == "rule_wizard_type"


@pytest.mark.asyncio
async def test_r3_existing_react_requirement_links_without_duplicate(database: Database) -> None:
    offer = await make_offer(database, "r3")
    requirement = await RequirementRepository(database).create(category_id=None, requirement_type="REACT", target=-1001, is_fake=True, wait_seconds=0)
    state = {"nonce": "n"}
    await panel._add_campaign_requirement(context_for(database, state, SimpleNamespace()), state, offer.id, requirement.id)
    await panel._add_campaign_requirement(context_for(database, state, SimpleNamespace()), state, offer.id, requirement.id)
    assert len(await OfferRequirementRepository(database).list_requirements(offer.id)) == 1


@pytest.mark.asyncio
async def test_requirements_batch_add_two_channels_then_done(database: Database) -> None:
    configure_admin_ids((OWNER,))
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "required_chats"}
    chats = {
        "@a": SimpleNamespace(id=-1001, type="channel", username="a", title="A"),
        "@b": SimpleNamespace(id=-1002, type="channel", username="b", title="B"),
    }
    bot = SimpleNamespace(get_chat=AsyncMock(side_effect=lambda identifier: chats[identifier]), edit_message_text=AsyncMock())
    context = context_for(database, state, bot)
    await panel._start_required_chat_input(context, state)
    await run_message_handler(message_update("@a"), context)
    await run_message_handler(message_update("A"), context)
    await run_message_handler(message_update("https://t.me/a"), context)
    await panel._start_required_chat_input(context, state)
    await run_message_handler(message_update("@b"), context)
    await run_message_handler(message_update("B"), context)
    await run_message_handler(message_update("https://t.me/b"), context)
    rows = await RequiredChatRepository(database).list_enabled()
    assert {row.chat_id for row in rows} == {-1001, -1002}
    assert state["required_chat_batch"] == [-1001, -1002]
    assert "Add another" in str(bot.edit_message_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_requirements_wizard_reuses_registry_completion_without_offer_link(database: Database) -> None:
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "required_chats",
             "input": {"kind": "required_chat"}, "wizard": {"chat_step": "invite_url", "identifier": "@shared", "display_name": "Shared"}}
    bot = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(id=-1003, type="channel", username="shared", title="Shared")), edit_message_text=AsyncMock())
    await panel._finish_channel_chat(context_for(database, state, bot), state)
    assert len(await RequiredChatRepository(database).list_enabled()) == 1
    assert await _linked_offer_count(database, -1003) == 0


async def _linked_offer_count(database: Database, chat_id: int) -> int:
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT COUNT(*) FROM offer_required_chats WHERE chat_id = ?", (chat_id,))).fetchone()
    return int(row[0])


@pytest.mark.asyncio
async def test_requirements_remove_warns_for_two_offers_then_unlinks(database: Database) -> None:
    first, second = await make_offer(database, "remove-a"), await make_offer(database, "remove-b")
    chat_id = -1004
    await RequiredChatRepository(database).upsert(RequiredChat(chat_id, "channel", "Shared", "shared", True, 1, 1, "https://t.me/shared"))
    repo = OfferRequirementRepository(database)
    await repo.add_required_chat(first.id, chat_id)
    await repo.add_required_chat(second.id, chat_id)
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "required_chats"}
    query = SimpleNamespace(edit_message_text=AsyncMock())
    context = context_for(database, state, SimpleNamespace())
    await panel._begin_required_chat_remove(context, state, chat_id, query=query)
    assert "required by 2 offers" in query.edit_message_text.await_args.kwargs["text"]
    await panel._remove_required_chat(context, state, chat_id, query=query)
    assert await _linked_offer_count(database, chat_id) == 0


@pytest.mark.asyncio
async def test_requirements_test_access_reports_admin_status(database: Database) -> None:
    state = {"nonce": "n", "owner_user_id": OWNER, "chat_id": OWNER, "message_id": 77, "closed": False, "view": "required_chats"}
    await RequiredChatRepository(database).upsert(RequiredChat(-1005, "channel", "Access", "access", True, 1, 1, "https://t.me/access"))
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=999)), get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")), edit_message_text=AsyncMock())
    query = SimpleNamespace(edit_message_text=AsyncMock())
    await panel._test_required_chat_access(context_for(database, state, bot), state, -1005, query=query)
    assert "bot is admin" in query.edit_message_text.await_args.kwargs["text"]
