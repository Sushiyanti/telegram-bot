"""Codec for short Verify callback payloads.

The nonce is a short cryptographically random, panel-compatible opaque value.
Phase 3 will bind it to the originating callback message/user in the handler;
no persistent nonce table is needed for this service-layer codec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_MAX_CALLBACK_BYTES = 64


@dataclass(frozen=True, slots=True)
class VerifyCallbackPayload:
    """Validated identity carried by a Verify callback."""

    offer_id: int
    user_id: int
    nonce: str

    @classmethod
    def encode(cls, payload: VerifyCallbackPayload) -> str:
        if not isinstance(payload, cls):
            raise TypeError("payload must be VerifyCallbackPayload")
        if payload.offer_id <= 0 or payload.user_id <= 0:
            raise ValueError("offer_id and user_id must be positive integers")
        if not isinstance(payload.nonce, str) or _NONCE_RE.fullmatch(payload.nonce) is None:
            raise ValueError("nonce must contain 1-32 URL-safe characters")
        encoded = f"verify:{payload.offer_id}:{payload.user_id}:{payload.nonce}"
        if len(encoded.encode("utf-8")) > _MAX_CALLBACK_BYTES:
            raise ValueError("callback payload exceeds 64 bytes")
        return encoded

    @classmethod
    def decode(cls, data: str) -> VerifyCallbackPayload | None:
        if not isinstance(data, str) or len(data.encode("utf-8")) > _MAX_CALLBACK_BYTES:
            return None
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "verify":
            return None
        _, offer_raw, user_raw, nonce = parts
        if not offer_raw.isdecimal() or not user_raw.isdecimal():
            return None
        try:
            offer_id = int(offer_raw, 10)
            user_id = int(user_raw, 10)
        except (ValueError, OverflowError):
            return None
        if offer_id <= 0 or user_id <= 0 or _NONCE_RE.fullmatch(nonce) is None:
            return None
        payload = cls(offer_id=offer_id, user_id=user_id, nonce=nonce)
        try:
            return payload if cls.encode(payload) == data else None
        except (TypeError, ValueError):
            return None


__all__ = ["VerifyCallbackPayload"]
