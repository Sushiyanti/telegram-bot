"""Render offer unlock instructions.

Rendering an already-passed evaluation raises ``ValueError('all_passed')``;
Phase 3 should bypass this renderer and perform delivery after verification.
"""

from __future__ import annotations

import secrets
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from media_bot.services.offer_evaluator import OfferEvaluationResult, OfferGateKind
from media_bot.services.verify_callback import VerifyCallbackPayload


def render_gate_message(offer: Any, result: OfferEvaluationResult) -> tuple[str, InlineKeyboardMarkup]:
    """Return stable instructions and buttons for every unmet gate."""
    if result.all_passed:
        raise ValueError("all_passed")

    lines = [str(getattr(offer, "title", "Offer")), "", "To unlock, complete the steps below:"]
    keyboard: list[list[InlineKeyboardButton]] = []
    for gate in result.unmet_gates:
        lines.append(f"• {gate.target_label}")
        if gate.kind is OfferGateKind.JOIN:
            if gate.url is not None:
                keyboard.append([InlineKeyboardButton(f"Join {gate.target_label}", url=gate.url)])
            else:
                lines.append("⚠ This required chat has no valid invite link.")
                keyboard.append([InlineKeyboardButton("Report issue", callback_data=f"report:{result.offer_id}")])
        elif gate.url is not None:
            keyboard.append([InlineKeyboardButton(f"Open {gate.target_label}", url=gate.url)])

    if result.has_misconfigured_gate:
        lines.extend(["", "This offer is misconfigured, please contact the admin."])

    nonce = secrets.token_urlsafe(8)
    verify_data = VerifyCallbackPayload.encode(
        VerifyCallbackPayload(result.offer_id, result.user_id, nonce)
    )
    keyboard.append([InlineKeyboardButton("Verify", callback_data=verify_data)])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


__all__ = ["render_gate_message"]
