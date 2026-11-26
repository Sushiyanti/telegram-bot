from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from media_bot.db.models import Offer
from media_bot.services.offer_evaluator import OfferEvaluator

USER = 7776557709
CHANNEL = -1004465118588


@pytest.fixture
def evaluator() -> OfferEvaluator:
    offer = Offer(1, 1, b"x" * 32, None, "Offer", None, True, 1, 1, 1, "offer")
    requirement = SimpleNamespace(
        id=3,
        type="REACT",
        target="-1000000000000",
        target_chat_id=CHANNEL,
        settings={"wait_seconds": 5, "link_url": "https://t.me/target"},
    )
    return OfferEvaluator(
        bot=SimpleNamespace(),
        offer_repo=SimpleNamespace(get=AsyncMock(return_value=offer)),
        requirement_repo=SimpleNamespace(
            list_required_chats=AsyncMock(return_value=[]),
            list_requirements=AsyncMock(return_value=[requirement]),
        ),
        reaction_repo=SimpleNamespace(
            has_reaction=AsyncMock(side_effect=AssertionError("live reaction lookup is obsolete")),
            has_recent_reaction=AsyncMock(
                side_effect=AssertionError("live reaction lookup is obsolete")
            ),
        ),
        chat_repo=SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    chat_id=CHANNEL,
                    username="target",
                    title="Target",
                    invite_url="https://t.me/target",
                )
            ),
        ),
    )


@pytest.mark.asyncio
async def test_react_gate_stays_pending_until_wait_is_completed(
    evaluator: OfferEvaluator,
) -> None:
    result = await evaluator.evaluate(1, USER)
    assert not result.all_passed
    assert result.gates[0].target_id == CHANNEL
    assert result.gates[0].target_label == "@target"
    assert result.gates[0].url == "https://t.me/target"
    assert result.gates[0].reason == "pending_wait"

    completed = await evaluator.evaluate(1, USER, fake_wait_completed=True)
    assert completed.all_passed
    assert completed.gates[0].reason is None


@pytest.mark.asyncio
async def test_configured_wait_uses_prompt_wait_requirement(
    evaluator: OfferEvaluator,
) -> None:
    assert await evaluator.configured_wait_seconds(1) == 5


@pytest.mark.asyncio
async def test_canonical_target_chat_id_wins_over_legacy_target(
    evaluator: OfferEvaluator,
) -> None:
    result = await evaluator.evaluate(1, USER, fake_wait_completed=True)
    assert result.gates[0].target_id == CHANNEL
    evaluator.chat_repo.get.assert_awaited_once_with(CHANNEL)
