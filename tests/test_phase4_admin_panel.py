"""Phase 4 Campaigns and statistics coverage."""
from pathlib import Path
import pytest
from media_bot.db.database import Database
from media_bot.db.models import MediaItem, Offer, RequiredChat
from media_bot.db.repositories import ItemRepository, OfferEventRepository, OfferRepository, OfferRequirementRepository, RequiredChatRepository
from media_bot.db.rules import RequirementRepository
from media_bot.media_transfer import StorageMessageRef
from media_bot.services.core import hash_share_token

OWNER = 9001
STORAGE = -1001234567890

@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "bot.sqlite3")
    await db.initialize()
    return db

async def make_item(db: Database) -> int:
    item = await ItemRepository(db).create_stored_item(MediaItem(owner_user_id=OWNER, media_type="document", file_size_bytes=10, storage_messages=(StorageMessageRef(STORAGE, 101),), file_name="offer.bin"))
    return item.id

@pytest.mark.asyncio
async def test_a1_schema_is_current_and_plaintext_is_null_for_backfilled_shape(database: Database) -> None:
    async with database.connection() as connection:
        version = await (await connection.execute("PRAGMA user_version")).fetchone()
        columns = await (await connection.execute("PRAGMA table_info(offers)")).fetchall()
    assert version[0] == 10
    assert next(row for row in columns if row["name"] == "slug_plain")["notnull"] == 0

@pytest.mark.asyncio
async def test_a2_offer_creation_persists_plaintext_and_matching_hash(database: Database) -> None:
    item_id = await make_item(database); slug = "Abc_-0123"
    offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token(slug), None, "Public offer", None, True, OWNER, 1, 1, slug))
    fetched = await OfferRepository(database).get_by_slug_hash(hash_share_token(slug))
    assert offer.id == fetched.id and fetched.slug_plain == slug and fetched.slug_hash == hash_share_token(slug)

@pytest.mark.asyncio
async def test_a3_panel_slug_lookup_supports_start_flow(database: Database) -> None:
    item_id = await make_item(database); slug = "panel_created_slug"
    await OfferRepository(database).create(Offer(0, item_id, hash_share_token(slug), None, "Deep link", None, True, OWNER, 1, 1, slug))
    offer = await OfferRepository(database).get_by_slug_hash(hash_share_token(slug), active_only=True)
    assert offer is not None and offer.item_id == item_id

@pytest.mark.asyncio
async def test_a4_events_do_not_contain_plaintext_slug(database: Database) -> None:
    item_id = await make_item(database); slug = "secret_slug"
    offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token(slug), None, "Safe", None, True, OWNER, 1, 1, slug))
    await OfferEventRepository(database).append(offer.id, OWNER, "start", at_ms=2)
    async with database.connection() as connection:
        rows = await (await connection.execute("SELECT metadata_json FROM offer_events")).fetchall()
    assert slug not in str(rows)

@pytest.mark.asyncio
async def test_a5b_disabled_offer_is_not_recipient_visible(database: Database) -> None:
    item_id = await make_item(database); slug = "disabled_offer"
    offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token(slug), None, "Disabled", None, True, OWNER, 1, 1, slug))
    await OfferRepository(database).disable(offer.id, updated_at_ms=2)
    assert await OfferRepository(database).get_by_slug_hash(hash_share_token(slug), active_only=True) is None

@pytest.mark.asyncio
async def test_a6b_multiple_required_channels_are_assignable(database: Database) -> None:
    item_id = await make_item(database)
    offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token("multi"), None, "Multi", None, True, OWNER, 1, 1, "multi"))
    chats = RequiredChatRepository(database)
    await chats.upsert(RequiredChat(-1001, "channel", "One", "one", True, 1, 1, "https://t.me/one")); await chats.upsert(RequiredChat(-1002, "channel", "Two", "two", True, 1, 1, "https://t.me/two"))
    requirements = OfferRequirementRepository(database); await requirements.add_required_chat(offer.id, -1001); await requirements.add_required_chat(offer.id, -1002, sort_order=1)
    assert [x.chat_id for x in await requirements.list_required_chats(offer.id)] == [-1001, -1002]

@pytest.mark.asyncio
async def test_a7_backfilled_shape_has_no_retrievable_plaintext(database: Database) -> None:
    item_id = await make_item(database)
    offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token("legacy-token"), "legacy", "Legacy", None, True, OWNER, 1, 1, None))
    fetched = await OfferRepository(database).get_by_id(offer.id)
    assert fetched is not None and fetched.slug_plain is None

@pytest.mark.asyncio
async def test_a8_aggregate_stats_are_available(database: Database) -> None:
    item_id = await make_item(database); events = OfferEventRepository(database)
    offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token("aggregate"), None, "Aggregate", None, True, OWNER, 1, 1, "aggregate"))
    await events.append(offer.id, 11, "start", at_ms=2); await events.append(offer.id, 11, "unlock", at_ms=3)
    values = await events.stats(); assert values[0].starts == 1 and values[0].unlocks == 1

@pytest.mark.asyncio
async def test_a9_per_offer_stats_count_distinct_users(database: Database) -> None:
    item_id = await make_item(database); offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token("stats-offer"), None, "Stats", None, True, OWNER, 1, 1, "stats-offer")); events = OfferEventRepository(database)
    await events.append(offer.id, 11, "start", at_ms=2); await events.append(offer.id, 11, "start", at_ms=3); await events.append(offer.id, 11, "unlock", at_ms=4)
    stats = (await events.stats(offer.id))[0]; assert stats.starts == 2 and stats.distinct_start_users == 1 and stats.distinct_unlock_users == 1

@pytest.mark.asyncio
async def test_a10_conversion_rate_is_zero_when_offer_has_no_starts(database: Database) -> None:
    item_id = await make_item(database); offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token("no-starts"), None, "No starts", None, True, OWNER, 1, 1, "no-starts")); value = (await OfferEventRepository(database).stats(offer.id))[0]
    assert value.conversion_rate_percent == 0.0

@pytest.mark.asyncio
async def test_a5_non_admin_panel_access_is_denied(database: Database) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from media_bot.config import Settings, configure_admin_ids
    from media_bot.handlers.panel import admin_panel_command
    configure_admin_ids((OWNER,))
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1234), effective_message=message, effective_chat=SimpleNamespace(type="private", id=1234))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"database": database, "settings": Settings(bot_token="123:x", storage_channel_id=STORAGE, admin_ids=(OWNER,), db_path=database.path, log_level="INFO", env="dev", max_relay_file_bytes=10_000, default_protect_content=False)}), user_data={})
    await admin_panel_command(update, context)
    message.reply_text.assert_awaited_once_with("Not authorized.")

@pytest.mark.asyncio
async def test_a6_offer_crud_add_remove_react_toggle_and_soft_delete(database: Database) -> None:
    item_id = await make_item(database)
    offer = await OfferRepository(database).create(Offer(0, item_id, hash_share_token("crud-offer"), None, "CRUD", None, True, OWNER, 1, 1, "crud-offer"))
    chats = RequiredChatRepository(database)
    await chats.upsert(RequiredChat(-1003, "channel", "Three", "three", True, 1, 1, "https://t.me/three"))
    links = OfferRequirementRepository(database)
    await links.add_required_chat(offer.id, -1003)
    assert [x.chat_id for x in await links.list_required_chats(offer.id)] == [-1003]
    await links.remove_required_chat(offer.id, -1003)
    assert await links.list_required_chats(offer.id) == ()
    rule = await RequirementRepository(database).create(category_id=None, requirement_type="REACT", target=-1003, is_fake=True, wait_seconds=0)
    await links.add_requirement(offer.id, rule.id)
    assert [x.requirement_id for x in await links.list_requirements(offer.id)] == [rule.id]
    await links.remove_requirement(offer.id, rule.id)
    assert await links.list_requirements(offer.id) == ()
    await OfferRepository(database).disable(offer.id, updated_at_ms=2)
    disabled = await OfferRepository(database).get_by_id(offer.id)
    assert disabled is not None and disabled.is_enabled is False
