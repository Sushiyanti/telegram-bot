"""Full admin-to-user campaign unlock regression."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update

from media_bot.db.database import Database
from media_bot.db.models import MediaItem, Offer, RequiredChat
from media_bot.db.repositories import ItemRepository, OfferRepository, OfferRequirementRepository, RequiredChatRepository
from media_bot.db.rules import ReactionRepository, RequirementRepository
from media_bot.handlers.reactions import ReactionUpdateHandler
from media_bot.handlers.sharing import start_command
from media_bot.handlers.verify import verify_callback
from media_bot.media_transfer import StorageMessageRef
from media_bot.services import hash_share_token

USER = 7776557709
OWNER = 9001
STORAGE = -10070001
CHANNEL = -10070002
TOKEN = "full-flow-campaign"


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "bot.sqlite3")
    await db.initialize(storage_channel_id=STORAGE)
    return db


def settings(db: Database):
    from media_bot.config import Settings
    return Settings(bot_token="123:flow", storage_channel_id=STORAGE, admin_ids=(OWNER,),
                    db_path=db.path, log_level="INFO", env="dev", max_relay_file_bytes=10_000_000,
                    default_protect_content=False, verified_transfer_max_file_bytes=10_000_000)


def update_for(user_id: int, args: list[str]):
    msg = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                           effective_chat=SimpleNamespace(type="private", id=user_id),
                           effective_message=msg, args=args)


@pytest.mark.asyncio
async def test_a1_to_a7_full_campaign_flow(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(MediaItem(
        owner_user_id=OWNER, media_type="document", file_size_bytes=100,
        storage_messages=(StorageMessageRef(STORAGE, 10, 0),), file_name="flow.bin",
    ))
    offer = await OfferRepository(database).create(Offer(
        0, item.id, hash_share_token(TOKEN), "flow", "Flow campaign", None, True,
        OWNER, 1, 1, TOKEN,
    ))

    # A1: admin-created registry channel and offer association.
    await RequiredChatRepository(database).upsert(RequiredChat(
        CHANNEL, "channel", "Flow channel", "flow_channel", True, 1, 1,
        "https://t.me/flow_channel",
    ))
    await OfferRequirementRepository(database).add_required_chat(offer.id, CHANNEL)
    assert any(row.chat_id == CHANNEL for row in await RequiredChatRepository(database).list_enabled())
    assert len(await OfferRequirementRepository(database).list_required_chats(offer.id)) == 1

    # A2: campaign-specific REACT rule: requirement + offer link, no global scope.
    requirement = await RequirementRepository(database).create(
        category_id=None, requirement_type="REACT", target=CHANNEL, is_fake=False,
        wait_seconds=0, last_x_messages=5, last_x_days=0,
        link_url="https://t.me/flow_channel/10", attach_scope=False,
        react_post="any_recent", reaction_emoji="👍",
    )
    await OfferRequirementRepository(database).add_requirement(offer.id, requirement.id)
    async with database.connection() as connection:
        assert int((await (await connection.execute(
            "SELECT COUNT(*) FROM category_requirements WHERE requirement_id = ?", (requirement.id,))).fetchone())[0]) == 0
    assert len(await OfferRequirementRepository(database).list_requirements(offer.id)) == 1

    bot = SimpleNamespace(
        get_chat_member=AsyncMock(side_effect=[SimpleNamespace(status="left"), SimpleNamespace(status="member"), SimpleNamespace(status="member")]),
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    context = SimpleNamespace(args=[TOKEN], bot=bot,
        application=SimpleNamespace(bot_data={"database": database, "settings": settings(database)}))

    # A3: initial user prompt is the offer renderer, with JOIN and Verify buttons.
    first = update_for(USER, [TOKEN])
    await start_command(first, context)
    first_text = first.effective_message.reply_text.await_args.args[0]
    first_markup = first.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert first_text.startswith("Flow campaign")
    assert "To unlock, complete the steps below:" in first_text
    assert any(button.url == "https://t.me/flow_channel" for row in first_markup.inline_keyboard for button in row)
    assert any(button.callback_data and button.callback_data.startswith("verify:") for row in first_markup.inline_keyboard for button in row)

    # A4: after joining, Verify still reports the missing reaction using offer messaging.
    callback_data = next(button.callback_data for row in first_markup.inline_keyboard for button in row
                          if button.callback_data and button.callback_data.startswith("verify:"))
    query = SimpleNamespace(data=callback_data, from_user=SimpleNamespace(id=USER),
                            answer=AsyncMock(), edit_message_text=AsyncMock(), message=first.effective_message)
    await verify_callback(SimpleNamespace(callback_query=query), context)
    verify_text = query.edit_message_text.await_args.args[0]
    assert verify_text.startswith("Flow campaign")
    assert "To unlock, complete the steps below:" in verify_text
    assert "missing_reaction" not in verify_text
    refreshed_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    query.data = next(button.callback_data for row in refreshed_markup.inline_keyboard for button in row
                      if button.callback_data and button.callback_data.startswith("verify:"))

    # A5: real Update.de_json channel_post then message_reaction updates persist.
    handler = ReactionUpdateHandler(database)
    post = Update.de_json({"update_id": 1, "channel_post": {
        "message_id": 10, "date": 1_800_000_000, "chat": {"id": CHANNEL, "type": "channel"},
    }}, bot=None)
    reaction = Update.de_json({"update_id": 2, "message_reaction": {
        "user": {"id": USER, "is_bot": False, "first_name": "User"},
        "chat": {"id": CHANNEL, "type": "channel"}, "message_id": 10,
        "date": 1_800_000_001, "new_reaction": [{"type": "emoji", "emoji": "👍"}],
        "old_reaction": [],
    }}, bot=None)
    await handler.handle_update(post, None)
    await handler.handle_update(reaction, None)
    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT user_id, channel_id, message_id, emoji FROM user_reactions WHERE user_id = ?", (USER,))).fetchone()
    assert tuple(row) == (USER, CHANNEL, 10, "👍")

    # A6: Verify after the observed reaction delivers and records unlock.
    await verify_callback(SimpleNamespace(callback_query=query), context)
    assert bot.copy_message.await_count == 1
    async with database.connection() as connection:
        event = await (await connection.execute(
            "SELECT COUNT(*) FROM offer_events WHERE offer_id = ? AND event_type = 'unlock'", (offer.id,))).fetchone()
    assert int(event[0]) == 1

    # A7: no legacy renderer fingerprint is ever emitted.
    for call in first.effective_message.reply_text.await_args_list:
        text = str(call.args[0])
        assert "to access this file" not in text
        assert "React to at least one recent post" not in text
