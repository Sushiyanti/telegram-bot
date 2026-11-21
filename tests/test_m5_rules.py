"""M5 dynamic rules, multi-category storage, and command policy tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from telegram import InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop

from media_bot.config import Settings, configure_admin_ids
from media_bot.db.database import Database
from media_bot.db.models import MediaItem, StorageMessageRef
from media_bot.db.repositories import ItemRepository, ShareLinkRepository, UploaderRepository
from media_bot.db.rules import (
    BotSettingsRepository,
    CategoryRepository,
    ReactionRepository,
    RequirementRepository,
)
from media_bot.handlers.categories import (
    add_requirement_command,
    category_selection_callback,
    delete_category_callback,
    delete_category_command,
)
from media_bot.handlers.policy import enforce_user_command_policy, toggle_commands_command
from media_bot.handlers.reactions import ReactionUpdateHandler
from media_bot.handlers.sharing import start_command
from media_bot.handlers.uploads import UploadHandler
from media_bot.media_transfer import TransferCapabilities
from media_bot.services import ShareLinkService
from media_bot.services.rule_engine import RequirementEvaluator

ADMIN_ID = 601001
USER_ID = 601002
STORAGE_ID = -1007012345001
REACT_CHANNEL = -1007012345002


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "m5.sqlite3")
    await db.initialize(storage_channel_id=STORAGE_ID)
    return db


def settings(database: Database) -> Settings:
    return Settings(
        bot_token="123:dummy",
        storage_channel_id=STORAGE_ID,
        admin_ids=(ADMIN_ID,),
        db_path=database.path,
        log_level="INFO",
        env="dev",
        max_relay_file_bytes=10_000_000,
        default_protect_content=False,
        verified_transfer_max_file_bytes=10_000_000,
    )


def make_item() -> MediaItem:
    return MediaItem(
        owner_user_id=ADMIN_ID,
        media_type="document",
        file_size_bytes=100,
        storage_messages=(StorageMessageRef(STORAGE_ID, 1001),),
        storage_channel_id=STORAGE_ID,
    )


async def share_token(database: Database, item_id: int) -> str:
    service = ShareLinkService(
        ShareLinkRepository(database),
        ItemRepository(database),
        TransferCapabilities(10_000_000),
    )
    return (await service.create(item_id=item_id, created_by_user_id=ADMIN_ID, max_uses=1)).token


def start_update(token: str, user_id: int = USER_ID):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=SimpleNamespace(type="private", id=user_id),
    )
    return update, message


def bot_stub(**kwargs):
    values = {
        "id": 998877,
        "copy_message": AsyncMock(return_value=SimpleNamespace(message_id=44)),
        "get_chat_member": AsyncMock(return_value=SimpleNamespace(status="member")),
        "get_chat": AsyncMock(return_value=SimpleNamespace(username="join_here")),
        "get_me": AsyncMock(return_value=SimpleNamespace(id=998877)),
        "send_message": AsyncMock(),
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def start_context(database: Database, token: str, bot=None):
    application_settings = settings(database)
    bot = bot or bot_stub()
    context = SimpleNamespace(
        args=[token],
        bot=bot,
        application=SimpleNamespace(
            bot_data={"settings": application_settings, "database": database}
        ),
    )
    return context, bot


async def test_schema_v5_seeds_default_and_backfills_legacy_items(tmp_path: Path) -> None:
    migrations = Path(__file__).resolve().parents[1] / "src/media_bot/db/migrations"
    path = tmp_path / "v4.db"
    async with aiosqlite.connect(path) as connection:
        for version in range(1, 5):
            sql_path = next(migrations.glob(f"{version:04d}_*.sql"))
            await connection.executescript(sql_path.read_text(encoding="utf-8"))
        cursor = await connection.execute(
            "INSERT INTO items (owner_user_id, media_type, file_size_bytes, created_at_ms) VALUES (1, 'document', 10, 100)"
        )
        item_id = cursor.lastrowid
        await connection.execute(
            "INSERT INTO item_storage_messages (item_id, storage_chat_id, storage_message_id) VALUES (?, ?, ?)",
            (item_id, STORAGE_ID, 501),
        )
        await connection.commit()

    database = Database(path)
    await database.initialize(storage_channel_id=STORAGE_ID)
    item = await ItemRepository(database).get(item_id)
    default = await CategoryRepository(database).default()
    version, _ = await database.health_check()

    assert version == 6
    assert default.name == "Default"
    assert default.storage_channel_id == STORAGE_ID
    assert item is not None and item.category_id == default.id
    assert item.storage_channel_id == STORAGE_ID


async def test_default_category_is_auto_assigned_for_existing_callers(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    default = await CategoryRepository(database).default()
    assert item.category_id == default.id
    assert item.storage_channel_id == STORAGE_ID


async def test_category_link_inherits_child_requirements_and_deduplicates(database: Database) -> None:
    categories = CategoryRepository(database)
    parent = await categories.create("Parent", -1007012345010)
    child = await categories.create("Child", -1007012345011)
    req = await RequirementRepository(database).create(
        category_id=child.id,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=False,
        wait_seconds=3,
    )
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO category_requirements (category_id, requirement_id) VALUES (?, ?)",
            (parent.id, req.id),
        )
    await categories.link("Parent", "Child")

    inherited = await categories.requirements_for(parent.id)
    assert [item.id for item in inherited] == [req.id]


async def test_category_graph_rejects_cycles(database: Database) -> None:
    categories = CategoryRepository(database)
    await categories.create("A", -1007012345010)
    await categories.create("B", -1007012345011)
    await categories.link("A", "B")
    with pytest.raises(ValueError, match="cycle"):
        await categories.link("B", "A")


async def test_global_requirement_applies_to_every_category(database: Database) -> None:
    category = await CategoryRepository(database).create("Elsewhere", -1007012345012)
    requirement = await RequirementRepository(database).create(
        category_id=None,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=False,
        wait_seconds=0,
    )
    assert [value.id for value in await CategoryRepository(database).requirements_for(category.id)] == [requirement.id]
    async with database.connection() as connection:
        row = await (await connection.execute(
            "SELECT category_id FROM category_requirements WHERE requirement_id = ?",
            (requirement.id,),
        )).fetchone()
    assert row["category_id"] is None


@pytest.mark.parametrize(
    ("delete_orphaned", "expected_count"),
    [(True, 0), (False, 1)],
)
async def test_category_deletion_choice_deletes_or_keeps_orphan_requirements(
    database: Database, delete_orphaned: bool, expected_count: int
) -> None:
    categories = CategoryRepository(database)
    category = await categories.create("Temporary", -1007012345020)
    requirement = await RequirementRepository(database).create(
        category_id=category.id,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=False,
        wait_seconds=1,
    )

    await categories.delete("Temporary", delete_orphaned_requirements=delete_orphaned)

    async with database.connection() as connection:
        count = await (await connection.execute(
            "SELECT COUNT(*) FROM requirements WHERE id = ?", (requirement.id,)
        )).fetchone()
        links = await (await connection.execute(
            "SELECT COUNT(*) FROM category_requirements WHERE category_id = ?", (category.id,)
        )).fetchone()
    assert int(count[0]) == expected_count
    assert int(links[0]) == 0


async def test_category_deletion_keeps_shared_requirement_even_in_delete_orphans_mode(database: Database) -> None:
    categories = CategoryRepository(database)
    doomed = await categories.create("Doomed", -1007012345021)
    survivor = await categories.create("Survivor", -1007012345022)
    requirement = await RequirementRepository(database).create(
        category_id=doomed.id,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=False,
        wait_seconds=1,
    )
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO category_requirements (category_id, requirement_id) VALUES (?, ?)",
            (survivor.id, requirement.id),
        )

    await categories.delete("Doomed", delete_orphaned_requirements=True)

    assert await RequirementRepository(database).get(requirement.id) is not None


async def test_category_deletion_reassigns_items_and_removes_links(database: Database) -> None:
    categories = CategoryRepository(database)
    default = await categories.default()
    removed = await categories.create("StorageA", -1007012345023)
    child = await categories.create("StorageB", -1007012345024)
    await categories.link("StorageA", "StorageB")
    saved = await ItemRepository(database).create_stored_item(
        MediaItem(
            owner_user_id=ADMIN_ID,
            media_type="document",
            file_size_bytes=42,
            storage_messages=(StorageMessageRef(removed.storage_channel_id, 5023),),
            category_id=removed.id,
            storage_channel_id=removed.storage_channel_id,
        )
    )

    await categories.delete("StorageA", delete_orphaned_requirements=True)

    reassigned = await ItemRepository(database).get(saved.id)
    assert reassigned is not None
    assert reassigned.category_id == default.id
    assert reassigned.storage_channel_id == removed.storage_channel_id
    assert reassigned.storage_messages[0].chat_id == removed.storage_channel_id
    async with database.connection() as connection:
        rows = await (await connection.execute(
            "SELECT COUNT(*) FROM category_links WHERE parent_category_id = ? OR child_category_id = ?",
            (removed.id, removed.id),
        )).fetchone()
    assert int(rows[0]) == 0
    assert await categories.get(child.id) is not None


async def test_default_category_cannot_be_deleted(database: Database) -> None:
    with pytest.raises(ValueError, match="Default"):
        await CategoryRepository(database).delete("Default", delete_orphaned_requirements=True)


async def test_requirement_validation_rejects_real_start_bot(database: Database) -> None:
    default = await CategoryRepository(database).default()
    with pytest.raises(ValueError, match="Real START_BOT verification is not supported"):
        await RequirementRepository(database).create(
            category_id=default.id,
            requirement_type="START_BOT",
            target="@example_bot",
            is_fake=False,
            wait_seconds=5,
        )
    accepted = await RequirementRepository(database).create(
        category_id=default.id,
        requirement_type="START_BOT",
        target="@example_bot",
        is_fake=True,
        wait_seconds=5,
    )
    assert accepted.settings["is_fake"] is True


async def test_react_requirement_uses_documented_defaults(database: Database) -> None:
    default = await CategoryRepository(database).default()
    req = await RequirementRepository(database).create(
        category_id=default.id,
        requirement_type="REACT",
        target=REACT_CHANNEL,
        is_fake=False,
        wait_seconds=4,
    )
    assert req.settings == {
        "is_fake": False,
        "last_x_days": 0,
        "last_x_messages": 5,
        "wait_seconds": 4,
    }


async def test_react_requirement_accepts_configured_message_and_day_windows(database: Database) -> None:
    default = await CategoryRepository(database).default()
    req = await RequirementRepository(database).create(
        category_id=default.id,
        requirement_type="REACT",
        target=REACT_CHANNEL,
        is_fake=False,
        wait_seconds=4,
        last_x_messages=12,
        last_x_days=30,
    )
    assert req.settings["last_x_messages"] == 12
    assert req.settings["last_x_days"] == 30


async def test_addrequirement_command_accepts_react_windows(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=ADMIN_ID))
    context = SimpleNamespace(
        args=["Default", "REACT", str(REACT_CHANNEL), "false", "2", "8", "14"],
        application=SimpleNamespace(bot_data={"database": database}),
    )
    await add_requirement_command(update, context)
    requirement = await RequirementRepository(database).get(1)
    assert requirement is not None
    assert requirement.settings["last_x_messages"] == 8
    assert requirement.settings["last_x_days"] == 14


async def test_addrequirement_command_supports_global_scope(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=ADMIN_ID))
    context = SimpleNamespace(
        args=["global", "TIMED_WAIT", "-", "false", "2"],
        application=SimpleNamespace(bot_data={"database": database}),
    )
    await add_requirement_command(update, context)
    message.reply_text.assert_awaited_once_with("Requirement 1 added to Global.")
    requirements = await CategoryRepository(database).requirements_for(
        (await CategoryRepository(database).default()).id
    )
    assert len(requirements) == 1 and requirements[0].type == "TIMED_WAIT"


async def test_start_fake_timed_rule_waits_then_delivers_without_consuming_early(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    await RequirementRepository(database).create(
        category_id=item.category_id,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=False,
        wait_seconds=7,
    )
    token = await share_token(database, item.id)
    update, message = start_update(token)
    context, bot = start_context(database, token)

    with patch("media_bot.services.rule_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await start_command(update, context)

    sleep.assert_awaited_once_with(7)
    assert [call.args[0] for call in message.reply_text.await_args_list][0] == "Verifying..."
    bot.copy_message.assert_awaited_once()
    link = (await ShareLinkRepository(database).list_for_item(item.id))[0]
    assert link.use_count == 1


async def test_real_join_left_refuses_delivery_without_consuming_share_link(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    await RequirementRepository(database).create(
        category_id=item.category_id,
        requirement_type="JOIN_CHAT",
        target=REACT_CHANNEL,
        is_fake=False,
        wait_seconds=0,
    )
    token = await share_token(database, item.id)
    update, message = start_update(token)
    bot = bot_stub(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")))
    context, bot = start_context(database, token, bot)

    await start_command(update, context)

    bot.copy_message.assert_not_awaited()
    bot.get_chat_member.assert_awaited_once_with(chat_id=REACT_CHANNEL, user_id=USER_ID)
    link = (await ShareLinkRepository(database).list_for_item(item.id))[0]
    assert link.use_count == 0
    assert "Join" in message.reply_text.await_args.args[0]


async def test_start_fake_join_waits_five_seconds_then_delivers(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    await RequirementRepository(database).create(
        category_id=item.category_id,
        requirement_type="JOIN_CHAT",
        target=REACT_CHANNEL,
        is_fake=True,
        wait_seconds=5,
    )
    token = await share_token(database, item.id)
    update, message = start_update(token)
    context, bot = start_context(database, token)

    with patch("media_bot.services.rule_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await start_command(update, context)

    sleep.assert_awaited_once_with(5)
    bot.get_chat_member.assert_not_awaited()
    bot.copy_message.assert_awaited_once()
    assert "Verifying..." in [call.args[0] for call in message.reply_text.await_args_list]
    link = (await ShareLinkRepository(database).list_for_item(item.id))[0]
    assert link.use_count == 1


async def test_start_fake_timed_wait_zero_delivers_without_sleep(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    await RequirementRepository(database).create(
        category_id=item.category_id,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=True,
        wait_seconds=0,
    )
    token = await share_token(database, item.id)
    update, message = start_update(token)
    context, bot = start_context(database, token)

    with patch("media_bot.services.rule_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await start_command(update, context)

    sleep.assert_not_awaited()
    bot.copy_message.assert_awaited_once()
    assert "Delivered." in [call.args[0] for call in message.reply_text.await_args_list]
    link = (await ShareLinkRepository(database).list_for_item(item.id))[0]
    assert link.use_count == 1


async def test_start_real_react_with_database_reaction_delivers(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    await RequirementRepository(database).create(
        category_id=item.category_id,
        requirement_type="REACT",
        target=REACT_CHANNEL,
        is_fake=False,
        wait_seconds=0,
        last_x_messages=5,
        last_x_days=0,
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    reactions = ReactionRepository(database)
    await reactions.observe_post(REACT_CHANNEL, 701, now_ms - 1_000)
    await reactions.replace_user_message_reactions(
        user_id=USER_ID,
        channel_id=REACT_CHANNEL,
        message_id=701,
        emojis=("👍",),
        updated_at_ms=now_ms,
    )
    token = await share_token(database, item.id)
    update, _ = start_update(token)
    bot = bot_stub(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")))
    context, bot = start_context(database, token, bot)

    await start_command(update, context)

    bot.copy_message.assert_awaited_once()
    link = (await ShareLinkRepository(database).list_for_item(item.id))[0]
    assert link.use_count == 1


async def test_start_real_react_without_database_reaction_refuses_delivery(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    await RequirementRepository(database).create(
        category_id=item.category_id,
        requirement_type="REACT",
        target=REACT_CHANNEL,
        is_fake=False,
        wait_seconds=0,
        last_x_messages=5,
        last_x_days=0,
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    await ReactionRepository(database).observe_post(REACT_CHANNEL, 701, now_ms - 1_000)
    token = await share_token(database, item.id)
    update, message = start_update(token)
    bot = bot_stub(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")))
    context, bot = start_context(database, token, bot)

    await start_command(update, context)

    bot.copy_message.assert_not_awaited()
    link = (await ShareLinkRepository(database).list_for_item(item.id))[0]
    assert link.use_count == 0
    assert "React to at least one recent post" in message.reply_text.await_args.args[0]


async def test_join_api_failure_falls_back_to_configured_wait_and_delivers(database: Database) -> None:
    item = await ItemRepository(database).create_stored_item(make_item())
    await RequirementRepository(database).create(
        category_id=item.category_id,
        requirement_type="JOIN_CHAT",
        target=REACT_CHANNEL,
        is_fake=False,
        wait_seconds=2,
    )
    token = await share_token(database, item.id)
    update, _ = start_update(token)
    bot = bot_stub(get_chat_member=AsyncMock(side_effect=RuntimeError("unavailable")))
    context, bot = start_context(database, token, bot)

    with patch("media_bot.services.rule_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await start_command(update, context)

    sleep.assert_awaited_once_with(2)
    bot.copy_message.assert_awaited_once()


async def test_fake_join_skips_telegram_membership_api(database: Database) -> None:
    category = await CategoryRepository(database).default()
    await RequirementRepository(database).create(
        category_id=category.id,
        requirement_type="JOIN_CHAT",
        target=REACT_CHANNEL,
        is_fake=True,
        wait_seconds=1,
    )
    bot = bot_stub(get_chat_member=AsyncMock())
    evaluator = RequirementEvaluator(database)
    message = SimpleNamespace(reply_text=AsyncMock())
    with patch("media_bot.services.rule_engine.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await evaluator.verify(category_id=category.id, user_id=USER_ID, bot=bot, message=message)
    assert result is True
    bot.get_chat_member.assert_not_awaited()
    sleep.assert_awaited_once_with(1)


async def test_reaction_updates_are_durable_and_removal_revokes_eligibility(database: Database) -> None:
    repository = ReactionRepository(database)
    handler = ReactionUpdateHandler(database)
    posted_at = datetime(2026, 11, 20, tzinfo=timezone.utc)
    await repository.observe_post(REACT_CHANNEL, 701, int(posted_at.timestamp() * 1000))
    date = datetime(2026, 11, 21, tzinfo=timezone.utc)
    reaction = SimpleNamespace(type="emoji", emoji="👍")
    change = SimpleNamespace(
        user=SimpleNamespace(id=USER_ID),
        chat=SimpleNamespace(id=REACT_CHANNEL),
        message_id=701,
        new_reaction=[reaction],
        date=date,
    )
    await handler.handle_update(SimpleNamespace(channel_post=None, message_reaction=change), None)
    assert await repository.has_recent_reaction(
        user_id=USER_ID, channel_id=REACT_CHANNEL, last_x_messages=5,
        last_x_days=0, now_ms=int(date.timestamp() * 1000),
    )

    change.new_reaction = []
    await handler.handle_update(SimpleNamespace(channel_post=None, message_reaction=change), None)
    assert not await repository.has_recent_reaction(
        user_id=USER_ID, channel_id=REACT_CHANNEL, last_x_messages=5,
        last_x_days=0, now_ms=int(date.timestamp() * 1000),
    )


async def test_reactions_for_unobserved_historical_posts_are_not_backfilled(database: Database) -> None:
    handler = ReactionUpdateHandler(database)
    change = SimpleNamespace(
        user=SimpleNamespace(id=USER_ID),
        chat=SimpleNamespace(id=REACT_CHANNEL),
        message_id=800,
        new_reaction=[SimpleNamespace(type="emoji", emoji="❤️")],
        date=datetime(2026, 11, 21, tzinfo=timezone.utc),
    )
    await handler.handle_update(SimpleNamespace(channel_post=None, message_reaction=change), None)
    assert not await ReactionRepository(database).has_recent_reaction(
        user_id=USER_ID, channel_id=REACT_CHANNEL, last_x_messages=5,
        last_x_days=0, now_ms=int(change.date.timestamp() * 1000),
    )


async def test_react_verification_requires_one_of_last_n_observed_posts(database: Database) -> None:
    repository = ReactionRepository(database)
    now_ms = 1_800_000_000_000
    for message_id in range(1, 4):
        await repository.observe_post(REACT_CHANNEL, message_id, now_ms + message_id)
    await repository.replace_user_message_reactions(
        user_id=USER_ID, channel_id=REACT_CHANNEL, message_id=1,
        emojis=("👍",), updated_at_ms=now_ms + 10,
    )
    assert not await repository.has_recent_reaction(
        user_id=USER_ID, channel_id=REACT_CHANNEL, last_x_messages=2,
        last_x_days=0, now_ms=now_ms + 20,
    )
    await repository.replace_user_message_reactions(
        user_id=USER_ID, channel_id=REACT_CHANNEL, message_id=3,
        emojis=("❤️",), updated_at_ms=now_ms + 20,
    )
    assert await repository.has_recent_reaction(
        user_id=USER_ID, channel_id=REACT_CHANNEL, last_x_messages=2,
        last_x_days=0, now_ms=now_ms + 20,
    )


async def test_togglecommands_and_policy_lock_regular_commands_but_allow_start_redeem(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    assert await BotSettingsRepository(database).user_commands_enabled()
    assert await BotSettingsRepository(database).toggle_user_commands() is False

    def context():
        return SimpleNamespace(application=SimpleNamespace(bot_data={
            "database": database, "settings": settings(database),
        }))

    disallowed = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID),
        effective_message=SimpleNamespace(text="/share 12", reply_text=AsyncMock()),
    )
    with pytest.raises(ApplicationHandlerStop):
        await enforce_user_command_policy(disallowed, context())
    disallowed.effective_message.reply_text.assert_awaited_once_with("Commands are currently locked.")

    for command in ("/start token", "/redeem code"):
        allowed = SimpleNamespace(
            effective_user=SimpleNamespace(id=USER_ID),
            effective_message=SimpleNamespace(text=command, reply_text=AsyncMock()),
        )
        await enforce_user_command_policy(allowed, context())
        allowed.effective_message.reply_text.assert_not_awaited()


async def test_policy_locks_media_uploads_but_admins_bypass(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    await BotSettingsRepository(database).toggle_user_commands()
    context = SimpleNamespace(application=SimpleNamespace(bot_data={
        "database": database, "settings": settings(database),
    }))
    media_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID),
        effective_message=SimpleNamespace(photo=[object()], reply_text=AsyncMock()),
    )
    with pytest.raises(ApplicationHandlerStop):
        await enforce_user_command_policy(media_update, context)
    admin_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=ADMIN_ID),
        effective_message=SimpleNamespace(text="/share 2", reply_text=AsyncMock()),
    )
    await enforce_user_command_policy(admin_update, context)
    admin_update.effective_message.reply_text.assert_not_awaited()

    await UploaderRepository(database).add(user_id=USER_ID, added_by=ADMIN_ID)
    uploader_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID),
        effective_message=SimpleNamespace(photo=[object()], reply_text=AsyncMock()),
    )
    await enforce_user_command_policy(uploader_update, context)
    uploader_update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("choice,expected_count", [("a", 0), ("b", 1)])
async def test_delcategory_requires_inline_choice_and_applies_selected_orphan_policy(
    database: Database, choice: str, expected_count: int
) -> None:
    configure_admin_ids((ADMIN_ID,))
    categories = CategoryRepository(database)
    category = await categories.create("RemoveMe", -1007012345030)
    requirement = await RequirementRepository(database).create(
        category_id=category.id,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=False,
        wait_seconds=0,
    )
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=ADMIN_ID), effective_message=message
    )
    context = SimpleNamespace(
        args=["RemoveMe"],
        application=SimpleNamespace(bot_data={"database": database}),
    )
    await delete_category_command(update, context)
    markup = message.reply_text.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert len(markup.inline_keyboard) == 3
    assert await categories.get(category.id) is not None

    query = SimpleNamespace(
        data=f"delcat:{choice}:{category.id}:{ADMIN_ID}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(),
    )
    callback_update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ADMIN_ID))
    callback_context = SimpleNamespace(application=SimpleNamespace(bot_data={"database": database}))
    await delete_category_callback(callback_update, callback_context)
    stored = await RequirementRepository(database).get(requirement.id)
    assert (stored is None) is (expected_count == 0)
    assert await categories.get(category.id) is None


async def test_category_selection_is_persisted_owner_checked_and_resumed(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    chosen = await CategoryRepository(database).create("Archive", -1007012345040)
    upload_handler = UploadHandler(settings(database), database)
    bot = bot_stub()
    media = SimpleNamespace(
        file_id="upload-file", file_unique_id="upload-unique", file_size=50,
        file_name="a.bin", mime_type="application/octet-stream",
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=ADMIN_ID, type="private"),
        message_id=901,
        media_group_id=None,
        document=media,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        animation=None,
        video_note=None,
        caption=None,
        has_protected_content=False,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=ADMIN_ID))
    await upload_handler.handle_message(update, SimpleNamespace(bot=bot))
    markup = message.reply_text.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    bot.copy_message.assert_not_awaited()
    upload_id = markup.inline_keyboard[0][0].callback_data.split(":")[1]
    upload = await upload_handler.pending.repository.get(upload_id)
    assert upload is not None and upload.category_selection_pending

    bad_query = SimpleNamespace(
        data=markup.inline_keyboard[1][0].callback_data,
        answer=AsyncMock(),
        message=message,
    )
    bad_update = SimpleNamespace(callback_query=bad_query, effective_user=SimpleNamespace(id=USER_ID))
    app_context = SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(bot_data={
            "database": database,
            "upload_handler": upload_handler,
        }),
    )
    await category_selection_callback(bad_update, app_context)
    bad_query.answer.assert_awaited_once()
    bot.copy_message.assert_not_awaited()

    # Recovery must re-present the selector and must not copy the pending file.
    recovery_bot = bot_stub()
    await UploadHandler(settings(database), database).recover(
        SimpleNamespace(bot=recovery_bot)
    )
    recovery_bot.send_message.assert_awaited_once()
    recovery_bot.copy_message.assert_not_awaited()

    good_query = SimpleNamespace(
        data=f"uselect:{upload_id}:{chosen.id}",
        answer=AsyncMock(),
        message=message,
    )
    good_update = SimpleNamespace(callback_query=good_query, effective_user=SimpleNamespace(id=ADMIN_ID))
    await category_selection_callback(good_update, app_context)
    bot.copy_message.assert_awaited_once_with(
        chat_id=chosen.storage_channel_id,
        from_chat_id=ADMIN_ID,
        message_id=901,
        protect_content=False,
    )
    stored = await ItemRepository(database).get(1)
    assert stored is not None and stored.category_id == chosen.id
    assert stored.storage_channel_id == chosen.storage_channel_id


async def test_togglecommands_admin_command_flips_persisted_setting(database: Database) -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=ADMIN_ID), effective_message=message
    )
    context = SimpleNamespace(
        args=[], application=SimpleNamespace(bot_data={"database": database})
    )
    await toggle_commands_command(update, context)
    message.reply_text.assert_awaited_once_with(
        "Regular-user commands and uploads are locked."
    )
    assert await BotSettingsRepository(database).user_commands_enabled() is False
