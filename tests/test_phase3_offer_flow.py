"""Phase 3 integration tests for offer /start, Verify, and delivery wiring."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CallbackQueryHandler

from media_bot.config import Settings
from media_bot.db.database import Database
from media_bot.db.models import MediaItem, Offer, RequiredChat
from media_bot.db.repositories import (
    ItemRepository,
    OfferRepository,
    OfferRequirementRepository,
    RequiredChatRepository,
    ShareLinkRepository,
)
from media_bot.db.rules import BotSettingsRepository
from media_bot.handlers.policy import enforce_user_command_policy
from media_bot.handlers.sharing import start_command
from media_bot.handlers.verify import verify_callback
from media_bot.media_transfer import StorageMessageRef
from media_bot.services import ShareLinkService, hash_share_token
from media_bot.services.offer_evaluator import OfferEvaluator
from media_bot.services.verify_callback import VerifyCallbackPayload

USER_A = 7101
USER_B = 7102
OWNER = 7201
STORAGE = -10077001


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state" / "bot.sqlite3")
    await database.initialize(storage_channel_id=STORAGE)
    return database


def settings(database: Database) -> Settings:
    return Settings(
        bot_token="123:phase3",
        storage_channel_id=STORAGE,
        admin_ids=(9999,),
        db_path=database.path,
        log_level="INFO",
        env="dev",
        max_relay_file_bytes=10_000_000,
        default_protect_content=False,
        verified_transfer_max_file_bytes=10_000_000,
    )


def update_for(user_id: int, args: list[str] | None = None) -> SimpleNamespace:
    message = SimpleNamespace(reply_text=AsyncMock(), text=None)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type="private", id=user_id),
        effective_message=message,
        args=args or [],
    )


def context_for(
    database: Database, user_id: int, bot: SimpleNamespace, args: list[str]
) -> SimpleNamespace:
    return SimpleNamespace(
        args=args,
        bot=bot,
        chat_data={},
        application=SimpleNamespace(
            bot_data={"database": database, "settings": settings(database)}
        ),
    )


async def create_offer(
    database: Database,
    *,
    token: str = "A" * 43,
    chats: tuple[tuple[int, str | None, str | None], ...] = (),
    panel_slug: bool = False,
) -> tuple[Offer, str]:
    item = await ItemRepository(database).create_stored_item(
        MediaItem(
            owner_user_id=OWNER,
            media_type="document",
            file_size_bytes=100,
            storage_messages=(
                StorageMessageRef(STORAGE, 20, 0),
                StorageMessageRef(STORAGE, 10, 1),
            ),
            file_name="offer.bin",
        )
    )
    offer = await OfferRepository(database).create(
        Offer(
            id=0,
            item_id=item.id,
            slug_hash=hash_share_token(token),
            slug_hint="phase3",
            title="Phase 3 offer",
            description=None,
            is_enabled=True,
            created_by_user_id=OWNER,
            created_at_ms=1,
            updated_at_ms=1,
            slug_plain=token if panel_slug else None,
        )
    )
    chat_repo = RequiredChatRepository(database)
    assignments = OfferRequirementRepository(database)
    for index, (chat_id, username, invite_url) in enumerate(chats):
        await chat_repo.upsert(
            RequiredChat(
                chat_id=chat_id,
                chat_type="channel",
                title=f"Chat {index}",
                username=username,
                invite_url=invite_url,
                is_enabled=True,
                created_at_ms=1,
                updated_at_ms=1,
            )
        )
        await assignments.add_required_chat(offer.id, chat_id, sort_order=index)
    return offer, token


def bot_for(*members: object) -> SimpleNamespace:
    return SimpleNamespace(
        get_chat_member=AsyncMock(side_effect=list(members)),
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        get_chat=AsyncMock(return_value=SimpleNamespace(username="fallback")),
    )


async def event_types(database: Database, offer_id: int, user_id: int) -> list[str]:
    async with database.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT event_type FROM offer_events WHERE offer_id = ? AND user_id = ? ORDER BY id",
                (offer_id, user_id),
            )
        ).fetchall()
    return [str(row["event_type"]) for row in rows]


@pytest.mark.asyncio
async def test_p1_happy_path_single_gate_delivers_in_storage_order(
    database: Database,
) -> None:
    offer, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="member"))
    update = update_for(USER_A, [token])
    await start_command(update, context_for(database, USER_A, bot, [token]))
    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [
        20,
        10,
    ]
    assert update.effective_message.reply_text.await_args.args[0] == "Delivered."
    assert await event_types(database, offer.id, USER_A) == [
        "start",
        "unlock",
        "delivery",
    ]
    async with database.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT delivery_count FROM offer_deliveries WHERE offer_id = ? AND user_id = ?",
                (offer.id, USER_A),
            )
        ).fetchone()
    assert row["delivery_count"] == 1


@pytest.mark.asyncio
async def test_p2_multi_channel_join_then_verify_delivers(database: Database) -> None:
    offer, token = await create_offer(
        database,
        chats=((-1001, "one", "https://t.me/one"), (-1002, "two", "https://t.me/two")),
    )
    bot = bot_for(
        SimpleNamespace(status="left"),
        SimpleNamespace(status="left"),
        SimpleNamespace(status="member"),
        SimpleNamespace(status="member"),
    )
    update = update_for(USER_A, [token])
    context = context_for(database, USER_A, bot, [token])
    await start_command(update, context)
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    verify_data = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("verify:")
    ][0]
    query = SimpleNamespace(
        data=verify_data,
        from_user=SimpleNamespace(id=USER_A),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=update.effective_message,
    )
    await verify_callback(SimpleNamespace(callback_query=query), context)
    assert bot.copy_message.await_count == 2
    assert await event_types(database, offer.id, USER_A) == [
        "start",
        "unlock",
        "delivery",
    ]


@pytest.mark.asyncio
async def test_p3_foreign_user_cannot_verify_or_edit(database: Database) -> None:
    _, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="left"))
    update = update_for(USER_A, [token])
    context = context_for(database, USER_A, bot, [token])
    await start_command(update, context)
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    data = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("verify:")
    ][0]
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=USER_B),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=update.effective_message,
    )
    await verify_callback(SimpleNamespace(callback_query=query), context)
    assert any(
        "not for you" in str(call.args[0]) for call in query.answer.await_args_list if call.args
    )
    query.edit_message_text.assert_not_awaited()
    assert bot.copy_message.await_count == 0


@pytest.mark.asyncio
async def test_p4_return_visit_delivers_again_but_unlocks_once(
    database: Database,
) -> None:
    offer, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="member"), SimpleNamespace(status="member"))
    for _ in range(2):
        await start_command(
            update_for(USER_A, [token]), context_for(database, USER_A, bot, [token])
        )
    assert bot.copy_message.await_count == 4
    assert await event_types(database, offer.id, USER_A) == [
        "start",
        "unlock",
        "delivery",
        "start",
        "delivery",
    ]
    async with database.connection() as connection:
        row = await (
            await connection.execute("SELECT delivery_count FROM offer_deliveries")
        ).fetchone()
    assert row["delivery_count"] == 2


@pytest.mark.asyncio
async def test_p5_misconfigured_offer_does_not_unlock_or_copy(
    database: Database,
) -> None:
    offer, token = await create_offer(database, chats=((-1001, None, None),))
    bot = bot_for(SimpleNamespace(status="left"))
    update = update_for(USER_A, [token])
    await start_command(update, context_for(database, USER_A, bot, [token]))
    text = update.effective_message.reply_text.await_args.args[0]
    assert "misconfigured" in text
    assert bot.copy_message.await_count == 0
    assert await event_types(database, offer.id, USER_A) == ["start"]


@pytest.mark.asyncio
async def test_p6_disabled_offer_is_unavailable_without_evaluation(
    database: Database,
) -> None:
    offer, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    await OfferRepository(database).disable(offer.id, updated_at_ms=2)
    bot = bot_for(SimpleNamespace(status="member"))
    update = update_for(USER_A, [token])
    await start_command(update, context_for(database, USER_A, bot, [token]))
    assert update.effective_message.reply_text.await_args.args[0] == "This offer is unavailable."
    bot.get_chat_member.assert_not_awaited()
    bot.copy_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_p7_legacy_link_still_uses_legacy_consumption(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(
        MediaItem(
            owner_user_id=OWNER,
            media_type="document",
            file_size_bytes=100,
            storage_messages=(StorageMessageRef(STORAGE, 55),),
        )
    )
    created = await ShareLinkService(
        ShareLinkRepository(database),
        ItemRepository(database),
        __import__(
            "media_bot.media_transfer", fromlist=["TransferCapabilities"]
        ).TransferCapabilities(10_000),
    ).create(item_id=item.id, created_by_user_id=OWNER)
    token = created.token
    bot = bot_for(SimpleNamespace(status="member"))
    update = update_for(USER_A, [token])
    await start_command(update, context_for(database, USER_A, bot, [token]))
    assert bot.copy_message.await_count == 1
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT use_count FROM share_links")).fetchone()
    assert row["use_count"] == 1


@pytest.mark.asyncio
async def test_p8_restricted_left_fails_without_delivery(database: Database) -> None:
    offer, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="restricted", is_member=False))
    update = update_for(USER_A, [token])
    await start_command(update, context_for(database, USER_A, bot, [token]))
    assert bot.copy_message.await_count == 0
    bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status="restricted", is_member=False)
    )
    result = await OfferEvaluator(
        bot=bot,
        offer_repo=OfferRepository(database),
        requirement_repo=OfferRequirementRepository(database),
        reaction_repo=SimpleNamespace(),
        chat_repo=RequiredChatRepository(database),
    ).evaluate(offer.id, USER_A)
    assert result.gates[0].reason == "restricted_left"


@pytest.mark.asyncio
async def test_p9_restricted_member_delivers(database: Database) -> None:
    _, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="restricted", is_member=True))
    await start_command(update_for(USER_A, [token]), context_for(database, USER_A, bot, [token]))
    assert bot.copy_message.await_count == 2


@pytest.mark.asyncio
async def test_p10_policy_allows_non_admin_start_and_verify_callback(
    database: Database,
) -> None:
    await BotSettingsRepository(database).toggle_user_commands()
    bot = SimpleNamespace()
    update = update_for(USER_A, ["slug"])
    update.effective_message.text = "/start slug"
    context = context_for(database, USER_A, bot, ["slug"])
    await enforce_user_command_policy(update, context)
    callback_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_A),
        effective_message=SimpleNamespace(text=None, caption=None, reply_text=AsyncMock()),
        callback_query=SimpleNamespace(data="verify:1:2:nonce"),
    )
    await enforce_user_command_policy(callback_update, context)
    callback_update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_p11_tampered_nonce_is_rejected_without_evaluation(
    database: Database,
) -> None:
    _, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="left"))
    update = update_for(USER_A, [token])
    context = context_for(database, USER_A, bot, [token])
    await start_command(update, context)
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    original = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("verify:")
    ][0]
    payload = VerifyCallbackPayload.decode(original)
    forged = VerifyCallbackPayload.encode(
        VerifyCallbackPayload(payload.offer_id, payload.user_id, "tampered_nonce")
    )
    query = SimpleNamespace(
        data=forged,
        from_user=SimpleNamespace(id=USER_A),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=update.effective_message,
    )
    await verify_callback(SimpleNamespace(callback_query=query), context)
    assert any("Expired" in str(call.args[0]) for call in query.answer.await_args_list if call.args)
    query.edit_message_text.assert_not_awaited()
    assert bot.copy_message.await_count == 0


@pytest.mark.asyncio
async def test_v1_left_member_gate_markup_contains_verify_button(
    database: Database,
) -> None:
    offer, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="left"))
    update = update_for(USER_A, [token])
    await start_command(update, context_for(database, USER_A, bot, [token]))
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert any(
        button.callback_data and button.callback_data.startswith("verify:")
        for row in markup.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_v2_member_start_delivers_and_records_unlock(database: Database) -> None:
    offer, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="member"))
    await start_command(update_for(USER_A, [token]), context_for(database, USER_A, bot, [token]))
    assert bot.copy_message.await_count == 2
    assert await event_types(database, offer.id, USER_A) == [
        "start",
        "unlock",
        "delivery",
    ]


@pytest.mark.asyncio
async def test_v3_registered_verify_handler_delivers_after_membership_changes(
    database: Database,
) -> None:
    offer, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="left"), SimpleNamespace(status="member"))
    context = context_for(database, USER_A, bot, [token])
    update = update_for(USER_A, [token])
    await start_command(update, context)
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    callback_data = next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("verify:")
    )
    query = SimpleNamespace(
        data=callback_data,
        from_user=SimpleNamespace(id=USER_A),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=update.effective_message,
    )
    handler = CallbackQueryHandler(verify_callback, pattern=r"^verify:")
    await handler.callback(SimpleNamespace(callback_query=query), context)
    assert bot.copy_message.await_count == 2
    assert await event_types(database, offer.id, USER_A) == [
        "start",
        "unlock",
        "delivery",
    ]


@pytest.mark.asyncio
async def test_v4_verify_answer_timeout_does_not_abort_delivery(
    database: Database,
) -> None:
    from telegram.error import TimedOut

    offer, token = await create_offer(database, chats=((-1001, "one", "https://t.me/one"),))
    bot = bot_for(SimpleNamespace(status="left"), SimpleNamespace(status="member"))
    context = context_for(database, USER_A, bot, [token])
    update = update_for(USER_A, [token])
    await start_command(update, context)
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    callback_data = next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("verify:")
    )
    query = SimpleNamespace(
        data=callback_data,
        from_user=SimpleNamespace(id=USER_A),
        answer=AsyncMock(side_effect=TimedOut()),
        edit_message_text=AsyncMock(),
        message=update.effective_message,
    )
    await verify_callback(SimpleNamespace(callback_query=query), context)
    assert bot.copy_message.await_count == 2
    assert await event_types(database, offer.id, USER_A) == [
        "start",
        "unlock",
        "delivery",
    ]


@pytest.mark.asyncio
async def test_r1_panel_slug_uses_offer_renderer_and_verify_button(
    database: Database,
) -> None:
    offer, token = await create_offer(
        database,
        token="panel-r1",
        panel_slug=True,
        chats=((-1001, "one", "https://t.me/one"),),
    )
    update = update_for(USER_A, [token])
    await start_command(
        update,
        context_for(database, USER_A, bot_for(SimpleNamespace(status="left")), [token]),
    )
    text = update.effective_message.reply_text.await_args.args[0]
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert text.startswith(offer.title)
    assert "To unlock, complete the steps below:" in text
    assert "to access this file" not in text
    assert "React to at least one recent post" not in text
    assert any(
        button.callback_data and button.callback_data.startswith("verify:")
        for row in markup.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_r2_panel_slug_lookup_mismatch_does_not_enter_legacy(
    monkeypatch, database: Database
) -> None:
    _, token = await create_offer(database, token="panel-r2", panel_slug=True)
    monkeypatch.setattr(OfferRepository, "get_by_slug_hash", AsyncMock(return_value=None))
    monkeypatch.setattr(OfferRepository, "exists_slug_hash", AsyncMock(return_value=True))
    legacy = AsyncMock(side_effect=AssertionError("legacy path entered"))
    monkeypatch.setattr("media_bot.handlers.sharing.ShareLinkService", legacy)
    update = update_for(USER_A, [token])
    await start_command(update, context_for(database, USER_A, bot_for(), [token]))
    assert (
        update.effective_message.reply_text.await_args.args[0]
        == "This offer is temporarily unavailable."
    )
    legacy.assert_not_called()


@pytest.mark.asyncio
async def test_r3_genuine_legacy_link_still_uses_legacy_path(
    database: Database,
) -> None:
    item = await ItemRepository(database).create_stored_item(
        MediaItem(
            owner_user_id=OWNER,
            media_type="document",
            file_size_bytes=100,
            storage_messages=(StorageMessageRef(STORAGE, 55),),
        )
    )
    created = await ShareLinkService(
        ShareLinkRepository(database),
        ItemRepository(database),
        __import__(
            "media_bot.media_transfer", fromlist=["TransferCapabilities"]
        ).TransferCapabilities(10_000),
    ).create(item_id=item.id, created_by_user_id=OWNER)
    bot = bot_for(SimpleNamespace(status="member"))
    await start_command(
        update_for(USER_A, [created.token]),
        context_for(database, USER_A, bot, [created.token]),
    )
    assert bot.copy_message.await_count == 1


@pytest.mark.asyncio
async def test_r4_panel_offer_output_has_no_legacy_fingerprints(
    database: Database,
) -> None:
    _, token = await create_offer(
        database,
        token="panel-r4",
        panel_slug=True,
        chats=((-1001, "one", "https://t.me/one"),),
    )
    update = update_for(USER_A, [token])
    await start_command(
        update,
        context_for(database, USER_A, bot_for(SimpleNamespace(status="left")), [token]),
    )
    text = update.effective_message.reply_text.await_args.args[0]
    assert "to access this file" not in text
    assert "React to at least one recent post" not in text
