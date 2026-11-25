#!/usr/bin/env python3
"""Diagnose whether a Telegram start token uses the offer or legacy engine."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from media_bot.config import Settings  # noqa: E402
from media_bot.db.database import Database  # noqa: E402
from media_bot.db.repositories import (  # noqa: E402
    OfferRepository,
    OfferRequirementRepository,
    RequiredChatRepository,
    ShareLinkRepository,
)
from media_bot.db.rules import ReactionRepository, RequirementRepository  # noqa: E402
from media_bot.services import hash_share_token  # noqa: E402
from media_bot.services.offer_evaluator import OfferEvaluator  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Telegram start slug")
    parser.add_argument("user_id", type=int, help="Telegram user ID to evaluate")
    parser.add_argument("--db", type=Path, default=None, help="SQLite database path")
    parser.add_argument("--env-file", type=Path, default=None, help=".env path")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    env_file = args.env_file or ROOT / ".env"
    load_dotenv(env_file, override=False)
    settings = Settings.from_env()
    db_path = args.db or settings.db_path
    database = Database(db_path)
    digest = hash_share_token(args.slug)
    print(f"sha256(slug) hex: {digest.hex()}")

    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT id, title, is_enabled, slug_plain FROM offers WHERE slug_hash = ?",
            (digest,),
        )).fetchone()
        if row is None:
            print("offer row found: no")
            legacy = await (await connection.execute(
                """SELECT id, item_id, category_id, is_active
                   FROM share_links WHERE token_hash = ?""",
                (digest,),
            )).fetchone()
            if legacy is None:
                print("legacy share_links row: no")
            else:
                print(
                    "legacy share_links row: yes "
                    f"id={legacy['id']} item_id={legacy['item_id']} "
                    f"category_id={legacy['category_id']} is_active={legacy['is_active']}"
                )
            print("renderer WOULD be used: rule_engine")
            return

        print(
            "offer row found: yes "
            f"id={row['id']} title={row['title']!r} "
            f"is_enabled={row['is_enabled']} "
            f"slug_plain_present={row['slug_plain'] is not None}"
        )
        offer_id = int(row["id"])
        channels = await (await connection.execute(
            """SELECT offer_id, chat_id, sort_order
               FROM offer_required_chats WHERE offer_id = ? ORDER BY sort_order, chat_id""",
            (offer_id,),
        )).fetchall()
        requirements = await (await connection.execute(
            """SELECT offer_id, requirement_id, sort_order
               FROM offer_requirements WHERE offer_id = ? ORDER BY sort_order, requirement_id""",
            (offer_id,),
        )).fetchall()
        print("offer_required_chats:")
        if channels:
            for item in channels:
                print(f"  offer_id={item['offer_id']} chat_id={item['chat_id']} sort_order={item['sort_order']}")
        else:
            print("  none")
        print("offer_requirements:")
        if requirements:
            for item in requirements:
                print(f"  offer_id={item['offer_id']} requirement_id={item['requirement_id']} sort_order={item['sort_order']}")
        else:
            print("  none")

    if not bool(row["is_enabled"]):
        print("OfferEvaluator result: offer disabled; evaluation is unavailable")
        print("renderer WOULD be used: offer_messaging (if enabled; current response is unavailable)")
        return

    async with Bot(settings.bot_token) as bot:
        evaluator = OfferEvaluator(
            bot=bot,
            offer_repo=OfferRepository(database),
            requirement_repo=OfferRequirementRepository(database),
            reaction_repo=ReactionRepository(database),
            chat_repo=RequiredChatRepository(database),
        )
        result = await evaluator.evaluate(offer_id, args.user_id)
    print("OfferEvaluator result:")
    if not result.gates:
        print("  no gates; all_passed=True")
    for gate in result.gates:
        print(
            f"  {gate.kind.value} | target={gate.target_id} | "
            f"passed={gate.passed} | reason={gate.reason}"
        )
    print(f"  all_passed={result.all_passed}")
    print("renderer WOULD be used: offer_messaging")


if __name__ == "__main__":
    asyncio.run(main())
