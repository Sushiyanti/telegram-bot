"""P4/P5 evidence regressions for broken invite URLs and reaction gates."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from media_bot.application import build_application
from media_bot.config import Settings
from media_bot.db.database import Database
from media_bot.db.models import Offer, RequiredChat
from media_bot.db.repositories import (
    RequiredChatRepository,
)
from media_bot.handlers.admin import fixinvites_command
from media_bot.services.offer_evaluator import OfferEvaluator
from media_bot.services.offer_messaging import render_gate_message


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "bot.sqlite3")
    await db.initialize(storage_channel_id=-10090001)
    return db


@pytest.mark.asyncio
async def test_f1_null_invite_url_is_misconfigured_admin_and_blocks_unlock(
    database: Database,
) -> None:
    offer = Offer(7, 1, b"x" * 32, None, "Broken offer", None, True, 1, 1, 1, "broken")
    evaluator = OfferEvaluator(
        bot=SimpleNamespace(get_chat_member=AsyncMock()),
        offer_repo=SimpleNamespace(get=AsyncMock(return_value=offer)),
        requirement_repo=SimpleNamespace(
            list_required_chats=AsyncMock(return_value=[SimpleNamespace(chat_id=-1001)]),
            list_requirements=AsyncMock(return_value=[]),
        ),
        reaction_repo=SimpleNamespace(),
        chat_repo=SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(title="Private", invite_url=None))
        ),
    )
    result = await evaluator.evaluate(offer.id, 99)
    assert result.gates[0].reason == "misconfigured_admin"
    text, markup = render_gate_message(offer, result)
    assert (
        "This offer is temporarily unavailable — the administrator needs to fix the required channel's join link."
        in text
    )
    assert any(
        button.callback_data and button.callback_data.startswith("verify:")
        for row in markup.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_f2_fixinvites_fills_url_and_join_gate_can_pass(
    database: Database,
) -> None:
    chat_id = -1002
    await RequiredChatRepository(database).upsert(
        RequiredChat(chat_id, "channel", "Public", "public", True, 1, 1, None)
    )
    message = SimpleNamespace(reply_text=AsyncMock())
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"database": database}),
        bot=SimpleNamespace(
            get_chat=AsyncMock(return_value=SimpleNamespace(username="public", invite_link=None))
        ),
    )
    await fixinvites_command(SimpleNamespace(effective_message=message), context)
    assert "Filled: 1" in message.reply_text.await_args.args[0]
    chat = (await RequiredChatRepository(database).list_enabled())[0]
    assert chat.invite_url == "https://t.me/public"


@pytest.mark.asyncio
async def test_f3_startup_warning_lists_null_invites_once_per_boot(
    database: Database, caplog
) -> None:
    await RequiredChatRepository(database).upsert(
        RequiredChat(-1003, "channel", "Missing", None, True, 1, 1, None)
    )
    settings = Settings(
        bot_token="123:test",
        storage_channel_id=-10090001,
        admin_ids=(1,),
        db_path=database.path,
        log_level="INFO",
        env="dev",
        max_relay_file_bytes=10_000,
        default_protect_content=False,
    )
    application = build_application(settings)
    application.bot_data["upload_handler"].recover = AsyncMock()
    with (
        patch(
            "telegram.Bot.get_me",
            new=AsyncMock(return_value=SimpleNamespace(username="testbot")),
        ),
        caplog.at_level("WARNING"),
    ):
        await application.post_init(application)
    matches = [
        record
        for record in caplog.records
        if record.message == "required_chat_invite_urls_missing chat_ids=[-1003]"
    ]
    assert len(matches) == 1
    assert "-1003" in str(matches[0].args)
