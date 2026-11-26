"""Unit tests for the un-wired Phase 2 offer gate services."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from media_bot.db.rules import Requirement
from media_bot.services.offer_evaluator import (
    OfferEvaluator,
    _classify_chat_member,
)
from media_bot.services.offer_messaging import render_gate_message
from media_bot.services.verify_callback import VerifyCallbackPayload

OFFER = SimpleNamespace(id=7, title="Starter pack", is_enabled=True)


def member(status: str, **values: object) -> SimpleNamespace:
    return SimpleNamespace(status=status, **values)


@pytest.mark.parametrize(
    ("status", "values", "expected"),
    [
        ("creator", {}, (True, None)),
        ("administrator", {}, (True, None)),
        ("member", {}, (True, None)),
        ("restricted", {"is_member": True}, (True, None)),
        ("restricted", {"is_member": False}, (False, "restricted_left")),
        ("restricted", {}, (False, "unavailable")),
        ("left", {"is_member": True}, (False, "missing_join")),
        ("kicked", {"is_member": True}, (False, "kicked")),
        ("unknown", {}, (False, "unavailable")),
    ],
)
def test_chat_member_classifier_matrix(
    status: str, values: dict[str, object], expected: tuple[bool, str | None]
) -> None:
    assert _classify_chat_member(member(status, **values)) == expected


@pytest.mark.asyncio
async def test_chat_member_classifier_handles_api_error_and_fresh_multi_channel_checks() -> None:
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=[member("member"), member("left")]))
    evaluator = OfferEvaluator(
        bot=bot,
        offer_repo=SimpleNamespace(get=AsyncMock(return_value=OFFER)),
        requirement_repo=SimpleNamespace(
            list_required_chats=AsyncMock(
                return_value=[
                    SimpleNamespace(chat_id=-1001, sort_order=0),
                    SimpleNamespace(chat_id=-1002, sort_order=1),
                ]
            ),
            list_requirements=AsyncMock(return_value=[]),
        ),
        reaction_repo=SimpleNamespace(),
        chat_repo=SimpleNamespace(
            get=AsyncMock(
                side_effect=[
                    SimpleNamespace(username="first", invite_url="https://t.me/first"),
                    SimpleNamespace(username="second", invite_url="https://t.me/second"),
                ]
            )
        ),
    )
    result = await evaluator.evaluate(OFFER.id, 99)
    assert not result.all_passed
    assert len(result.unmet_gates) == 1
    assert result.unmet_gates[0].reason == "missing_join"
    assert bot.get_chat_member.await_count == 2

    assert _classify_chat_member(member("member")) == (True, None)
    # The evaluator catches TelegramError and applies fail-closed behavior.
    evaluator.chat_repo.get = AsyncMock(
        return_value=SimpleNamespace(username="first", invite_url="https://t.me/first")
    )
    bot.get_chat_member = AsyncMock(side_effect=TelegramError("network"))
    result = await evaluator.evaluate(OFFER.id, 99)
    assert result.gates[0].reason == "unavailable"


@pytest.mark.asyncio
async def test_missing_invite_url_is_misconfigured_and_renderer_has_no_broken_url_button() -> None:
    evaluator = OfferEvaluator(
        bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=member("left"))),
        offer_repo=SimpleNamespace(get=AsyncMock(return_value=OFFER)),
        requirement_repo=SimpleNamespace(
            list_required_chats=AsyncMock(
                return_value=[SimpleNamespace(chat_id=-1001, sort_order=0)]
            ),
            list_requirements=AsyncMock(return_value=[]),
        ),
        reaction_repo=SimpleNamespace(),
        chat_repo=SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(title="Private group", invite_url=None))
        ),
    )
    result = await evaluator.evaluate(OFFER.id, 99)
    assert result.gates[0].reason == "misconfigured_admin"
    text, markup = render_gate_message(OFFER, result)
    assert (
        "This offer is temporarily unavailable — the administrator needs to fix the required channel's join link."
        in text
    )
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert not any(button.url for button in buttons)
    assert any(button.callback_data == "report:7" for button in buttons)


@pytest.mark.asyncio
async def test_reaction_gate_is_prompt_and_wait_only() -> None:
    requirement = Requirement(
        id=3,
        type="REACT",
        target="-1009",
        settings={"wait_seconds": 5},
        created_at_ms=1,
        target_chat_id=-1009,
    )
    evaluator = OfferEvaluator(
        bot=SimpleNamespace(),
        offer_repo=SimpleNamespace(get=AsyncMock(return_value=OFFER)),
        requirement_repo=SimpleNamespace(
            list_required_chats=AsyncMock(return_value=[]),
            list_requirements=AsyncMock(return_value=[requirement]),
        ),
        reaction_repo=SimpleNamespace(
            has_recent_reaction=AsyncMock(side_effect=AssertionError("live lookup removed"))
        ),
        chat_repo=SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(username="target", invite_url="https://t.me/target")
            )
        ),
    )
    result = await evaluator.evaluate(OFFER.id, 99)
    assert not result.gates[0].passed
    assert result.gates[0].reason == "pending_wait"
    completed = await evaluator.evaluate(OFFER.id, 99, fake_wait_completed=True)
    assert completed.gates[0].passed


@pytest.mark.asyncio
async def test_renderer_snapshot_preserves_gate_order_and_verify_codec() -> None:
    evaluator = OfferEvaluator(
        bot=SimpleNamespace(
            get_chat_member=AsyncMock(side_effect=[member("left"), member("left")])
        ),
        offer_repo=SimpleNamespace(get=AsyncMock(return_value=OFFER)),
        requirement_repo=SimpleNamespace(
            list_required_chats=AsyncMock(
                return_value=[
                    SimpleNamespace(chat_id=-1002, sort_order=1),
                    SimpleNamespace(chat_id=-1001, sort_order=0),
                ]
            ),
            list_requirements=AsyncMock(return_value=[]),
        ),
        reaction_repo=SimpleNamespace(),
        chat_repo=SimpleNamespace(
            get=AsyncMock(
                side_effect=[
                    SimpleNamespace(username="second", invite_url="https://t.me/second"),
                    SimpleNamespace(username="first", invite_url="https://t.me/first"),
                ]
            )
        ),
    )
    result = await evaluator.evaluate(OFFER.id, 99)
    text, markup = render_gate_message(OFFER, result)
    assert text.splitlines()[3:5] == ["• @second", "• @first"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [button.url for button in buttons[:2]] == [
        "https://t.me/second",
        "https://t.me/first",
    ]
    verify = buttons[-1].callback_data
    assert verify is not None and VerifyCallbackPayload.decode(verify) is not None


def test_verify_callback_codec_round_trip_and_strict_rejection() -> None:
    payload = VerifyCallbackPayload(123, 456, "Abc_09-x")
    encoded = VerifyCallbackPayload.encode(payload)
    assert len(encoded.encode()) <= 64
    assert VerifyCallbackPayload.decode(encoded) == payload
    malformed = [
        "verify:123:456",  # missing field
        "verify:Abc_09-x:123:456",  # wrong field order
        "verify:" + "9" * 60 + ":1:x",  # oversized
        "verify:nope:1:x",  # non-numeric
        "verify:1:2:x:extra",  # extra field
    ]
    assert all(VerifyCallbackPayload.decode(value) is None for value in malformed)
    assert VerifyCallbackPayload.decode("verify:1:2:") is None
