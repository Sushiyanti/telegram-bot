"""Phase 1 persistence tests for reusable advertise-to-unlock offers."""

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from media_bot.db.database import Database
from media_bot.db.models import MediaItem, Offer
from media_bot.db.repositories import (
    ItemRepository,
    OfferDeliveryRepository,
    OfferEventRepository,
    OfferRepository,
    RequiredChatRepository,
    ShareLinkRepository,
)
from media_bot.media_transfer import StorageMessageRef
from media_bot.services.core import hash_share_token
from media_bot.startup import import_legacy_required_channel

USER_A = 1001
USER_B = 1002
ITEM_OWNER = 2001
STORAGE_CHAT = -1001234567890


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state" / "bot.sqlite3")
    await database.initialize()
    return database


def make_item() -> MediaItem:
    return MediaItem(
        owner_user_id=ITEM_OWNER,
        media_type="document",
        file_size_bytes=100,
        storage_messages=(StorageMessageRef(STORAGE_CHAT, 101),),
        file_name="campaign.pdf",
    )


async def _apply_v1_to_v6(path: Path) -> None:
    migrations = Path(__file__).resolve().parents[1] / "src/media_bot/db/migrations"
    async with aiosqlite.connect(path) as connection:
        for version in range(1, 7):
            migration_path = next(
                name for name in migrations.iterdir() if name.name.startswith(f"{version:04d}_")
            )
            await connection.executescript(migration_path.read_text(encoding="utf-8"))
        await connection.commit()


@pytest.mark.asyncio
async def test_v6_to_v7_migration_backfills_existing_hash_and_preserves_multiple_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v6.sqlite3"
    await _apply_v1_to_v6(path)
    legacy = Database(path)
    item = await ItemRepository(legacy).create_stored_item(make_item())
    links = ShareLinkRepository(legacy)
    first = await links.create(
        item_id=item.id,
        token_hash=hash_share_token("A" * 43),
        created_by_user_id=ITEM_OWNER,
        created_at_ms=100,
        expires_at_ms=None,
    )
    second = await links.create(
        item_id=item.id,
        token_hash=hash_share_token("B" * 43),
        created_by_user_id=ITEM_OWNER,
        created_at_ms=101,
        expires_at_ms=None,
    )

    await legacy.initialize()
    offers = await OfferRepository(legacy).list_active(item_id=item.id)
    assert {offer.slug_hash for offer in offers} == {
        first.token_hash,
        second.token_hash,
    }
    assert len(offers) == 2
    async with legacy.connection() as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
    assert version[0] == 10


@pytest.mark.asyncio
async def test_offer_unlock_is_idempotent_under_concurrent_callbacks(
    database: Database,
) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    offer = await OfferRepository(database).create(
        Offer(
            id=0,
            item_id=item.id,
            slug_hash=hash_share_token("C" * 43),
            slug_hint="test",
            title="Test offer",
            description=None,
            is_enabled=True,
            created_by_user_id=ITEM_OWNER,
            created_at_ms=1,
            updated_at_ms=1,
        )
    )
    repository = OfferDeliveryRepository(database)
    results = await asyncio.gather(
        repository.record_unlock(offer.id, USER_A, at_ms=10),
        repository.record_unlock(offer.id, USER_A, at_ms=11),
    )
    assert sum(inserted for _, inserted in results) == 1
    async with database.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT COUNT(*) AS count FROM offer_events WHERE offer_id = ? AND user_id = ? AND event_type = 'unlock'",
                (offer.id, USER_A),
            )
        ).fetchone()
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_two_users_can_unlock_same_offer_and_delivery_stats_are_distinct_user_based(
    database: Database,
) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    offer = await OfferRepository(database).create(
        Offer(
            id=0,
            item_id=item.id,
            slug_hash=hash_share_token("D" * 43),
            slug_hint="shared",
            title="Shared offer",
            description=None,
            is_enabled=True,
            created_by_user_id=ITEM_OWNER,
            created_at_ms=1,
            updated_at_ms=1,
        )
    )
    events = OfferEventRepository(database)
    await events.append(offer.id, USER_A, "start", at_ms=1)
    await events.append(offer.id, USER_B, "start", at_ms=2)
    deliveries = OfferDeliveryRepository(database)
    await deliveries.record_unlock(offer.id, USER_A, at_ms=3)
    await deliveries.record_unlock(offer.id, USER_B, at_ms=4)
    await deliveries.record_delivery(offer.id, USER_A, at_ms=5)
    await deliveries.record_delivery(offer.id, USER_A, at_ms=6)
    await deliveries.record_delivery(offer.id, USER_B, at_ms=7)
    stats = await events.stats(offer.id)
    assert stats[0].distinct_start_users == 2
    assert stats[0].distinct_unlock_users == 2
    assert stats[0].deliveries == 3
    assert stats[0].conversion_rate_percent == 100.0


@pytest.mark.asyncio
async def test_item_deletion_cascades_offers_and_offer_events(
    database: Database,
) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    offer = await OfferRepository(database).create(
        Offer(
            id=0,
            item_id=item.id,
            slug_hash=hash_share_token("E" * 43),
            slug_hint="delete",
            title="Delete offer",
            description=None,
            is_enabled=True,
            created_by_user_id=ITEM_OWNER,
            created_at_ms=1,
            updated_at_ms=1,
        )
    )
    await OfferDeliveryRepository(database).record_unlock(offer.id, USER_A, at_ms=2)
    await ItemRepository(database).delete(item.id)
    async with database.connection() as connection:
        for table, column in (
            ("offers", "id"),
            ("offer_deliveries", "offer_id"),
            ("offer_events", "offer_id"),
        ):
            row = await (
                await connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ?",
                    (offer.id,),
                )
            ).fetchone()
            assert row["count"] == 0


@pytest.mark.asyncio
async def test_legacy_required_channel_import_is_one_time_and_does_not_reappear(
    database: Database,
) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    offer = await OfferRepository(database).create(
        Offer(
            id=0,
            item_id=item.id,
            slug_hash=hash_share_token("F" * 43),
            slug_hint="import",
            title="Import offer",
            description=None,
            is_enabled=True,
            created_by_user_id=ITEM_OWNER,
            created_at_ms=1,
            updated_at_ms=1,
        )
    )
    first = await import_legacy_required_channel(database, -100555)
    second = await import_legacy_required_channel(database, None)
    third = await import_legacy_required_channel(database, -100555)
    assert first == 1
    assert second == 0
    assert third == 0
    registry = await RequiredChatRepository(database).list_enabled()
    assert [entry.chat_id for entry in registry] == [-100555]
    from media_bot.db.repositories import OfferRequirementRepository

    assigned = await OfferRequirementRepository(database).list_required_chats(offer.id)
    assert [entry.chat_id for entry in assigned] == [-100555]
    async with database.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT value FROM bot_settings WHERE key = 'REQUIRED_CHANNEL_IMPORT_V1'"
            )
        ).fetchone()
    assert row["value"] == "done"
