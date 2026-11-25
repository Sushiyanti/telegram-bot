"""Acceptance coverage for live-shaped reaction persistence and evaluation."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import Update

from media_bot.db.database import Database
from media_bot.db.models import Offer
from media_bot.db.rules import ReactionRepository
from media_bot.handlers.reactions import ReactionUpdateHandler
from media_bot.services.offer_evaluator import OfferEvaluator


USER = 7776557709
CHANNEL = -1004465118588
MESSAGE = 987


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state" / "bot.sqlite3")
    await database.initialize()
    return database


def reaction_update(emojis: list[dict[str, str]]) -> Update:
    return Update.de_json({
        "update_id": 991,
        "message_reaction": {
            "chat": {"id": CHANNEL, "type": "channel", "title": "Target"},
            "message_id": MESSAGE,
            "user": {"id": USER, "is_bot": False, "first_name": "Test"},
            "date": 1_800_000_001,
            "old_reaction": [],
            "new_reaction": emojis,
        },
    }, bot=None)


@pytest.mark.asyncio
async def test_exact_post_reaction_is_persisted_without_observed_channel_post(database: Database) -> None:
    handler = ReactionUpdateHandler(database)
    await handler.handle_update(reaction_update([{"type": "emoji", "emoji": "👍"}]), None)
    repository = ReactionRepository(database)
    assert await repository.has_reaction(
        user_id=USER, channel_id=CHANNEL, message_id=MESSAGE, required_emoji="👍"
    )
    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT user_id, channel_id, message_id, emoji FROM user_reactions"
        )).fetchone()
    assert tuple(row) == (USER, CHANNEL, MESSAGE, "👍")


@pytest.mark.asyncio
async def test_reaction_removal_makes_exact_gate_fail_again(database: Database) -> None:
    repository = ReactionRepository(database)
    await repository.replace_user_message_reactions(
        user_id=USER, channel_id=CHANNEL, message_id=MESSAGE,
        emojis=("👍",), updated_at_ms=1_800_000_001,
    )
    assert await repository.has_reaction(
        user_id=USER, channel_id=CHANNEL, message_id=MESSAGE, required_emoji="👍"
    )
    await repository.replace_user_message_reactions(
        user_id=USER, channel_id=CHANNEL, message_id=MESSAGE,
        emojis=(), updated_at_ms=1_800_000_002,
    )
    assert not await repository.has_reaction(
        user_id=USER, channel_id=CHANNEL, message_id=MESSAGE, required_emoji="👍"
    )


@pytest.mark.asyncio
async def test_evaluator_enforces_specific_post(database: Database) -> None:
    repository = ReactionRepository(database)
    await repository.replace_user_message_reactions(
        user_id=USER, channel_id=CHANNEL, message_id=MESSAGE,
        emojis=("👍",), updated_at_ms=1_800_000_001,
    )
    offer = Offer(1, 1, b"x" * 32, None, "Offer", None, True, 1, 1, 1, "offer")
    evaluator = OfferEvaluator(
        bot=SimpleNamespace(),
        offer_repo=SimpleNamespace(get=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value=offer)),
        requirement_repo=SimpleNamespace(
            list_required_chats=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value=[]),
            list_requirements=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value=[SimpleNamespace(
                requirement_id=3,
            )]),
            get_requirement=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value=SimpleNamespace(
                id=3, type="REACT", target=str(CHANNEL), settings={
                    "react_post": str(MESSAGE), "reaction_emoji": "👍",
                },
            )),
        ),
        reaction_repo=repository,
        chat_repo=SimpleNamespace(),
    )
    result = await evaluator.evaluate(1, USER)
    assert result.all_passed
    assert result.gates[0].reason is None


@pytest.mark.asyncio
async def test_any_recent_requires_observed_post_timestamp(database: Database) -> None:
    repository = ReactionRepository(database)
    await repository.observe_post(CHANNEL, MESSAGE, 1_800_000_000_000)
    await repository.replace_user_message_reactions(
        user_id=USER, channel_id=CHANNEL, message_id=MESSAGE,
        emojis=("👍",), updated_at_ms=1_800_000_001_000,
    )
    assert await repository.has_recent_reaction(
        user_id=USER, channel_id=CHANNEL, last_x_messages=5,
        last_x_days=1_000, now_ms=1_800_000_002_000, required_emoji="👍"
    )
