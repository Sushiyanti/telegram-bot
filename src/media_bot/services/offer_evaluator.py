"""Read-only evaluation of reusable offer unlock gates.

This module deliberately performs no event writes, delivery, link consumption,
or handler registration. It treats Telegram/API uncertainty as a failed gate so
callers can fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from media_bot.db.rules import Requirement


class OfferNotFound(LookupError):
    """Raised when an offer is missing or disabled."""


class OfferGateKind(str, Enum):
    JOIN = "join"
    REACT = "react"


@dataclass(frozen=True, slots=True)
class OfferGateResult:
    kind: OfferGateKind
    target_id: int | None
    target_label: str
    url: str | None
    passed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class OfferEvaluationResult:
    offer_id: int
    user_id: int
    gates: tuple[OfferGateResult, ...]

    @property
    def all_passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def unmet_gates(self) -> tuple[OfferGateResult, ...]:
        return tuple(gate for gate in self.gates if not gate.passed)

    @property
    def has_unavailable_gate(self) -> bool:
        return any(gate.reason == "unavailable" for gate in self.unmet_gates)

    @property
    def has_misconfigured_gate(self) -> bool:
        return any(gate.reason == "misconfigured" for gate in self.unmet_gates)


def _classify_chat_member(member: Any) -> tuple[bool, str | None]:
    """Classify Telegram membership, including the restricted-left edge case."""
    status = str(getattr(member, "status", "")).lower()
    if status in {"creator", "administrator", "member"}:
        return True, None
    if status == "restricted":
        is_member = getattr(member, "is_member", None)
        if is_member is True:
            return True, None
        if is_member is False:
            return False, "restricted_left"
        return False, "unavailable"
    if status == "left":
        return False, "missing_join"
    if status == "kicked":
        return False, "kicked"
    return False, "unavailable"


class OfferEvaluator:
    """Evaluate all enabled offer gates using fresh, mockable repository calls."""

    def __init__(self, *, bot: Any, offer_repo: Any, requirement_repo: Any, reaction_repo: Any, chat_repo: Any) -> None:
        self.bot = bot
        self.offer_repo = offer_repo
        self.requirement_repo = requirement_repo
        self.reaction_repo = reaction_repo
        self.chat_repo = chat_repo

    async def evaluate(self, offer_id: int, user_id: int) -> OfferEvaluationResult:
        offer = await self._offer(offer_id)
        if offer is None or not bool(getattr(offer, "is_enabled", False)):
            raise OfferNotFound(f"offer {offer_id} is missing or disabled")

        gates: list[OfferGateResult] = []
        for assignment in await self._list_required_chats(offer_id):
            chat_id = int(getattr(assignment, "chat_id"))
            chat = await self._chat(chat_id)
            label, url = _chat_presentation(chat, chat_id)
            if chat is None:
                gates.append(OfferGateResult(
                    OfferGateKind.JOIN, chat_id, label, None, False, "misconfigured"
                ))
                continue
            if url is None:
                gates.append(OfferGateResult(
                    OfferGateKind.JOIN, chat_id, label, None, False, "misconfigured"
                ))
                continue
            try:
                member = await self.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                passed, reason = _classify_chat_member(member)
            except Exception:
                passed, reason = False, "unavailable"
            gates.append(OfferGateResult(
                OfferGateKind.JOIN, chat_id, label, url, passed, reason
            ))

        for assignment in await self._list_requirements(offer_id):
            requirement = await self._resolve_requirement(assignment)
            if requirement is None or str(getattr(requirement, "type", "")).upper() != "REACT":
                gates.append(OfferGateResult(
                    OfferGateKind.REACT, _target_id(requirement), _requirement_label(requirement),
                    _requirement_url(requirement), False, "unavailable"
                ))
                continue
            target_id = _target_id(requirement)
            settings = getattr(requirement, "settings", {}) or {}
            try:
                channel_id = int(getattr(requirement, "target"))
                passed = await self.reaction_repo.has_recent_reaction(
                    user_id=user_id,
                    channel_id=channel_id,
                    last_x_messages=int(settings.get("last_x_messages", 5)),
                    last_x_days=int(settings.get("last_x_days", 0)),
                )
                reason = None if passed else "missing_reaction"
            except Exception:
                passed, reason = False, "unavailable"
            gates.append(OfferGateResult(
                OfferGateKind.REACT,
                target_id,
                _requirement_label(requirement),
                _requirement_url(requirement),
                bool(passed),
                reason,
            ))
        return OfferEvaluationResult(offer_id, user_id, tuple(gates))

    async def _offer(self, offer_id: int) -> Any:
        for name in ("get", "get_by_id"):
            method = getattr(self.offer_repo, name, None)
            if method is not None:
                return await method(offer_id)
        raise AttributeError("offer_repo must provide get() or get_by_id()")

    async def _list_required_chats(self, offer_id: int) -> tuple[Any, ...]:
        return tuple(await self.requirement_repo.list_required_chats(offer_id))

    async def _list_requirements(self, offer_id: int) -> tuple[Any, ...]:
        return tuple(await self.requirement_repo.list_requirements(offer_id))

    async def _chat(self, chat_id: int) -> Any:
        method = getattr(self.chat_repo, "get", None)
        if method is not None:
            return await method(chat_id)
        rows = await self.chat_repo.list_enabled()
        return next((row for row in rows if int(getattr(row, "chat_id")) == chat_id), None)

    async def _resolve_requirement(self, assignment: Any) -> Any:
        if hasattr(assignment, "type"):
            return assignment
        requirement_id = getattr(assignment, "requirement_id", None)
        for name in ("get_requirement", "get"):
            method = getattr(self.requirement_repo, name, None)
            if method is not None and requirement_id is not None:
                value = await method(requirement_id)
                if value is not None:
                    return value
        return None


# Kept as a class attribute as well as a module function for isolated callers.
OfferEvaluator._classify_chat_member = staticmethod(_classify_chat_member)  # type: ignore[attr-defined]


def _chat_presentation(chat: Any, chat_id: int) -> tuple[str, str | None]:
    if chat is None:
        return f"Chat {chat_id}", None
    username = getattr(chat, "username", None)
    title = getattr(chat, "title", None)
    label = f"@{str(username).lstrip('@')}" if username else str(title or f"Chat {chat_id}")
    invite_url = getattr(chat, "invite_url", None)
    if isinstance(invite_url, str) and invite_url.startswith("https://"):
        return label, invite_url
    if isinstance(username, str) and username.strip():
        return label, f"https://t.me/{username.lstrip('@')}"
    return label, None


def _target_id(requirement: Any) -> int | None:
    try:
        return int(getattr(requirement, "target"))
    except (TypeError, ValueError):
        return None


def _requirement_label(requirement: Any) -> str:
    target = getattr(requirement, "target", None)
    return f"React to a post in {target}" if target is not None else "React to the required post"


def _requirement_url(requirement: Any) -> str | None:
    settings = getattr(requirement, "settings", {}) or {}
    value = settings.get("link_url")
    return value if isinstance(value, str) and value.startswith("https://") else None


__all__ = [
    "OfferEvaluationResult", "OfferEvaluator", "OfferGateKind", "OfferGateResult",
    "OfferNotFound", "_classify_chat_member",
]
