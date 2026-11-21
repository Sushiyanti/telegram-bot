"""M6 role registry and single-message admin panel tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop

from media_bot.config import Settings, configure_admin_ids
from media_bot.db.admins import AdminRepository
from media_bot.db.database import Database
from media_bot.handlers.panel import (
    _home_markup,
    admin_panel_callback,
    admin_panel_command,
    admin_panel_input,
)

ADMIN_ID = 810_001
NEW_ADMIN_ID = 810_002


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    result = Database(tmp_path / "m6.sqlite3")
    await result.initialize(storage_channel_id=-100810001)
    return result


def make_context(database: Database) -> SimpleNamespace:
    settings = Settings(
        bot_token="123:dummy",
        storage_channel_id=-100810001,
        admin_ids=(ADMIN_ID,),
        db_path=database.path,
        log_level="INFO",
        env="dev",
        max_relay_file_bytes=10_000_000,
        default_protect_content=False,
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        user_data={},
        bot=bot,
        application=SimpleNamespace(bot_data={"database": database, "settings": settings}),
    )


def callback_update(user_id: int, message_id: int, data: str) -> tuple[SimpleNamespace, SimpleNamespace]:
    message = SimpleNamespace(
        chat=SimpleNamespace(id=user_id, type="private"),
        message_id=message_id,
    )
    query = SimpleNamespace(
        data=data,
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id)), query


async def open_panel(context: SimpleNamespace) -> dict[str, object]:
    user = SimpleNamespace(id=ADMIN_ID)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=user,
        effective_message=message,
        effective_chat=SimpleNamespace(id=ADMIN_ID, type="private"),
    )
    await admin_panel_command(update, context)
    return context.user_data["m6_admin_panel"]


async def press(context: SimpleNamespace, action: str, *args: object) -> SimpleNamespace:
    state = context.user_data["m6_admin_panel"]
    data = ":".join(("m6", str(state["nonce"]), action, *(str(value) for value in args)))
    update, query = callback_update(ADMIN_ID, 77, data)
    await admin_panel_callback(update, context)
    return query


def test_home_keyboard_has_four_primary_sections() -> None:
    markup = _home_markup({"nonce": "test1234"})
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == [
        "📁 Categories", "📜 Requirements", "👥 Staff", "⚙️ Settings", "Close Menu",
    ]
    for row in markup.inline_keyboard:
        for button in row:
            assert button.callback_data.startswith("m6:test1234:")
            assert len(button.callback_data.encode("utf-8")) <= 64


async def test_panel_callbacks_edit_the_bound_message_without_new_bot_messages(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)

    context.bot.send_message.assert_awaited_once()
    sent_markup = context.bot.send_message.await_args.kwargs["reply_markup"]
    assert [button.text for row in sent_markup.inline_keyboard for button in row][:4] == [
        "📁 Categories", "📜 Requirements", "👥 Staff", "⚙️ Settings",
    ]

    query = await press(context, "staff")
    query.answer.assert_awaited_once_with()
    query.edit_message_text.assert_awaited_once()
    assert context.bot.send_message.await_count == 1
    assert context.bot.edit_message_text.await_count == 0
    assert state["message_id"] == 77


async def test_stale_or_foreign_callback_cannot_edit_panel(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    data = f"m6:{state['nonce']}:staff"
    update, query = callback_update(ADMIN_ID, 999, data)

    await admin_panel_callback(update, context)

    query.answer.assert_awaited_once_with("This panel is expired or unavailable.", show_alert=True)
    query.edit_message_text.assert_not_awaited()
    assert context.bot.send_message.await_count == 1


async def test_previous_generation_callback_is_rejected(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    old_nonce = state["nonce"]
    await press(context, "staff")
    update, query = callback_update(ADMIN_ID, 77, f"m6:{old_nonce}:staff")

    await admin_panel_callback(update, context)

    query.answer.assert_awaited_once_with("This panel is expired or unavailable.", show_alert=True)
    query.edit_message_text.assert_not_awaited()


async def test_add_admin_numeric_input_returns_to_staff_in_same_panel(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)

    await press(context, "staff")
    await press(context, "staffrole", "admins", 0)
    await press(context, "staffadd", "admins")
    assert state["view"] == "input_staff_admin"
    assert state["input"] == {"kind": "add_admin"}

    inbound = SimpleNamespace(
        text=str(NEW_ADMIN_ID),
        chat=SimpleNamespace(id=ADMIN_ID, type="private"),
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=ADMIN_ID),
        effective_chat=inbound.chat,
        effective_message=inbound,
    )
    with pytest.raises(ApplicationHandlerStop):
        await admin_panel_input(update, context)

    assert await AdminRepository(database).is_admin(NEW_ADMIN_ID)
    assert state["input"] is None
    assert state["view"] == "staff_admins"
    assert context.bot.send_message.await_count == 1
    assert context.bot.edit_message_text.await_count == 1
    assert context.bot.edit_message_text.await_args.kwargs["chat_id"] == ADMIN_ID
    assert context.bot.edit_message_text.await_args.kwargs["message_id"] == 77
    assert "Administrator added successfully." in context.bot.edit_message_text.await_args.kwargs["text"]
    inbound.reply_text.assert_not_awaited()


async def test_panel_cannot_remove_environment_super_admin(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    admins = AdminRepository(database)
    await admins.seed_configured((ADMIN_ID,))
    context = make_context(database)
    await open_panel(context)
    await press(context, "staff")
    await press(context, "staffrole", "admins", 0)

    query = await press(context, "staffremove", "admins", ADMIN_ID)

    assert await admins.is_admin(ADMIN_ID)
    assert "Environment super-admins cannot be removed." in query.edit_message_text.await_args.kwargs["text"]
