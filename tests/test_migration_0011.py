from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from media_bot.db.database import Database, MigrationError
from media_bot.db.repositories import OfferRequirementRepository

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "src" / "media_bot" / "db" / "migrations"


async def _make_v10(path: Path, *, ambiguous: bool = False, with_categories: bool = False) -> None:
    async with aiosqlite.connect(path) as connection:
        await connection.execute("PRAGMA foreign_keys = ON")
        first_names = {
            1: "initial",
            2: "audit_log",
            3: "upload_metadata",
            4: "uploaders_and_delivery",
            5: "dynamic_rules_categories",
            6: "admins",
        }
        for version in range(1, 7):
            await connection.executescript(
                (MIGRATIONS / f"{version:04d}_{first_names[version]}.sql").read_text()
            )
        await connection.execute(
            """INSERT INTO items(owner_user_id, media_type, created_at_ms, category_id)
               VALUES (123, 'document', 1, (SELECT id FROM categories WHERE name='Default'))"""
        )
        await connection.execute(
            """INSERT INTO item_storage_messages(item_id, storage_chat_id, storage_message_id, sequence)
               VALUES (1, -100999, 50, 0)"""
        )
        await connection.execute(
            """INSERT INTO share_links(item_id, token_hash, created_by_user_id, created_at_ms)
               VALUES (1, ?, 123, 1)""",
            (b"t" * 32,),
        )
        await connection.execute(
            """INSERT INTO required_chats(chat_id, chat_type, title, created_at_ms, updated_at_ms)
               VALUES (-100111, 'channel', 'Registered', 1, 1)"""
        )
        parent_id = None
        child_id = None
        if with_categories:
            await connection.execute(
                "INSERT INTO categories(name, created_at_ms) VALUES ('Parent', 2)"
            )
            await connection.execute(
                "INSERT INTO categories(name, created_at_ms) VALUES ('Child', 3)"
            )
            parent_id = (
                await (
                    await connection.execute("SELECT id FROM categories WHERE name='Parent'")
                ).fetchone()
            )[0]
            child_id = (
                await (
                    await connection.execute("SELECT id FROM categories WHERE name='Child'")
                ).fetchone()
            )[0]
            await connection.execute(
                "INSERT INTO category_links(parent_category_id, child_category_id) VALUES (?, ?)",
                (parent_id, child_id),
            )
            await connection.execute("UPDATE items SET category_id = ? WHERE id = 1", (parent_id,))
        await connection.execute(
            """INSERT INTO requirements(type, target, settings_json, created_at_ms)
               VALUES ('JOIN_CHAT', '-100111', '{"is_fake":false,"wait_seconds":0}', 4)"""
        )
        await connection.execute(
            """INSERT INTO category_requirements(category_id, requirement_id)
               VALUES (NULL, 1)"""
        )
        if with_categories:
            await connection.execute(
                """INSERT INTO requirements(type, target, settings_json, created_at_ms)
                   VALUES ('REACT', '-100111', '{"is_fake":false,"reaction_emoji":"👍","react_post":"any_recent"}', 5)"""
            )
            await connection.execute(
                """INSERT INTO category_requirements(category_id, requirement_id) VALUES (?, 2)""",
                (parent_id,),
            )
            await connection.execute(
                """INSERT INTO requirements(type, target, settings_json, created_at_ms)
                   VALUES ('TIMED_WAIT', NULL, '{"is_fake":true,"wait_seconds":1}', 6)"""
            )
            await connection.execute(
                """INSERT INTO category_requirements(category_id, requirement_id) VALUES (?, 3)""",
                (child_id,),
            )
        for version in range(7, 11):
            names = {
                7: "marketing_offers",
                8: "offer_slug_plain",
                9: "exact_reaction_posts",
                10: "reconcile_active_legacy_links",
            }
            await connection.executescript(
                (MIGRATIONS / f"{version:04d}_{names[version]}.sql").read_text()
            )
        offer_id = (
            await (await connection.execute("SELECT id FROM offers WHERE item_id=1")).fetchone()
        )[0]
        if ambiguous:
            await connection.execute(
                """INSERT INTO requirements(type, target, settings_json, created_at_ms)
                   VALUES ('REACT', '-100111', '{"is_fake":false,"reaction_emoji":"🔥","react_post":"any_recent"}', 7)"""
            )
            direct_id = (
                await (await connection.execute("SELECT max(id) FROM requirements")).fetchone()
            )[0]
            await connection.execute(
                "INSERT INTO offer_requirements(offer_id, requirement_id, sort_order) VALUES (?, ?, ?)",
                (offer_id, direct_id, 9 if ambiguous else 0),
            )
        else:
            await connection.execute(
                """INSERT INTO requirements(type, target, settings_json, created_at_ms)
                   VALUES ('REACT', '-100111', '{"is_fake":false,"reaction_emoji":"🔥","react_post":"any_recent"}', 7)"""
            )
            direct_id = (
                await (await connection.execute("SELECT max(id) FROM requirements")).fetchone()
            )[0]
            await connection.execute(
                "INSERT INTO offer_requirements(offer_id, requirement_id, sort_order) VALUES (?, ?, 0)",
                (offer_id, direct_id),
            )
        await connection.commit()


@pytest.mark.asyncio
async def test_migration_preserves_later_direct_rule_on_legacy_offer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "direct.db"
    await _make_v10(path)
    db = Database(path)
    await db.initialize()
    async with db.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT requirement_id FROM rule_assignments WHERE scope_type='OFFER' ORDER BY requirement_id"
            )
        ).fetchall()
        assert [row[0] for row in rows] == [2]
        assert (await (await connection.execute("PRAGMA foreign_keys")).fetchone())[0] == 1


@pytest.mark.asyncio
async def test_ambiguous_legacy_row_rolls_back_before_dropping_old_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ambiguous.db"
    await _make_v10(path, ambiguous=True)
    with pytest.raises(MigrationError):
        await Database(path).initialize()
    async with aiosqlite.connect(path) as connection:
        assert (await (await connection.execute("PRAGMA user_version")).fetchone())[0] == 10
        tables = {
            row[0]
            for row in await (
                await connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert {
            "category_requirements",
            "offer_requirements",
            "offer_required_chats",
        } <= tables


@pytest.mark.asyncio
async def test_category_inheritance_and_deduplication_survive_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "categories.db"
    await _make_v10(path, with_categories=True)
    db = Database(path)
    await db.initialize()
    async with db.connection() as connection:
        assignments = await (
            await connection.execute(
                "SELECT scope_type, category_id, requirement_id FROM rule_assignments ORDER BY requirement_id"
            )
        ).fetchall()
    assert [(r[0], r[1], r[2]) for r in assignments] == [
        ("GLOBAL", None, 1),
        ("CATEGORY", 2, 2),
        ("CATEGORY", 3, 3),
        ("OFFER", None, 4),
    ]
    effective = await OfferRequirementRepository(db).list_requirements(1)
    assert [row.requirement_id for row in effective] == [2, 3, 4]


@pytest.mark.asyncio
async def test_prompt_wait_migration_preserves_react_assignment_and_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prompt-wait.db"
    await _make_v10(path, with_categories=True)
    db = Database(path)
    await db.initialize()
    async with db.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT type, target_chat_id, settings_json FROM requirements WHERE id = 2"
            )
        ).fetchone()
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        assignment = await (
            await connection.execute(
                "SELECT scope_type, category_id FROM rule_assignments WHERE requirement_id = 2"
            )
        ).fetchone()
    assert tuple(row[:2]) == ("REACT", -100111)
    settings = json.loads(row[2])
    assert settings["is_fake"] == 1
    assert settings["verification_mode"] == "prompt_wait"
    assert "migration_notice" in settings
    assert tuple(assignment) == ("CATEGORY", 2)
    assert version[0] == 13
