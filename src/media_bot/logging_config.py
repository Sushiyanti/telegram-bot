"""Logging setup with conservative handling of HTTP client logs."""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    """Configure application logging without enabling credential-bearing URLs."""
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError("Invalid logging level")

    logging.basicConfig(level=numeric_level, format=_LOG_FORMAT)
    # Bot API URLs contain the token in their path. Keep HTTP request logs quiet
    # even when application-level DEBUG logging is enabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
