"""M6 role registry and single-message admin panel tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.ext import ApplicationHandlerStop

from media_bot.config import Settings, configure_admin_ids
from media_bot.db.admins import AdminRepository
from media_bot.db.database import Database
from media_bot.db.repositories import UploaderRepository
from media_bot.db.rules import CategoryRepository, RequirementRepository
from media_bot.handlers.panel import (
    _home_markup,
    admin_panel_callback,
    admin_panel_command,
    admin_panel_input,
    expire_panel_flow_before_command,
)
import media_bot.handlers.panel as panel_handlers

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


async def test_each_admin_command_opens_a_new_panel_and_retires_the_old_one(
    database: Database,
) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    context.bot.send_message.side_effect = [
        SimpleNamespace(message_id=77),
        SimpleNamespace(message_id=78),
    ]
    first_state = await open_panel(context)
    old_nonce = first_state["nonce"]

    second_state = await open_panel(context)

    assert context.bot.send_message.await_count == 2
    assert second_state is not first_state
    assert second_state["message_id"] == 78
    assert second_state["view"] == "home"
    assert context.bot.edit_message_text.await_args.kwargs["message_id"] == 77
    assert context.bot.edit_message_text.await_args.kwargs["reply_markup"] is None
    assert "replaced" in context.bot.edit_message_text.await_args.kwargs["text"]

    update, query = callback_update(ADMIN_ID, 77, f"m6:{old_nonce}:staff")
    await admin_panel_callback(update, context)
    query.answer.assert_awaited_once_with(
        "This panel is expired or unavailable.", show_alert=True
    )


async def test_closed_panel_can_be_reopened_with_a_fresh_message(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    context.bot.send_message.side_effect = [
        SimpleNamespace(message_id=77),
        SimpleNamespace(message_id=78),
    ]
    first_state = await open_panel(context)
    old_nonce = first_state["nonce"]
    await press(context, "close")
    assert first_state["closed"] is True

    second_state = await open_panel(context)

    assert second_state["message_id"] == 78
    assert second_state["closed"] is False
    update, query = callback_update(ADMIN_ID, 77, f"m6:{old_nonce}:staff")
    await admin_panel_callback(update, context)
    query.answer.assert_awaited_once_with(
        "This panel is expired or unavailable.", show_alert=True
    )


async def test_edit_failure_creates_a_fresh_recoverable_panel(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    context.bot.send_message.side_effect = [
        SimpleNamespace(message_id=77),
        SimpleNamespace(message_id=78),
    ]
    state = await open_panel(context)
    old_nonce = state["nonce"]
    data = f"m6:{old_nonce}:staff"
    update, query = callback_update(ADMIN_ID, 77, data)
    query.edit_message_text.side_effect = RuntimeError("message is not editable")

    await admin_panel_callback(update, context)

    assert context.bot.send_message.await_count == 2
    assert state["message_id"] == 78
    assert state["view"] == "home"
    assert state["input"] is None and state["wizard"] is None
    assert "fresh panel" in context.bot.send_message.await_args.kwargs["text"]
    stale_update, stale_query = callback_update(ADMIN_ID, 77, data)
    await admin_panel_callback(stale_update, context)
    stale_query.answer.assert_awaited_once_with(
        "This panel is expired or unavailable.", show_alert=True
    )


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


async def test_add_uploader_persists_and_refreshes_staff_view_in_same_panel(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)

    await press(context, "staff")
    await press(context, "staffrole", "uploaders", 0)
    await press(context, "staffadd", "uploaders")
    assert state["view"] == "input_staff_uploader"
    assert state["input"] == {"kind": "add_uploader"}

    inbound, _ = _private_input(text="810099")
    await _send_expected_panel_input(context, inbound)

    assert await UploaderRepository(database).is_active(810099)
    assert state["input"] is None
    assert state["view"] == "staff_uploaders"
    assert "Uploader added successfully." in context.bot.edit_message_text.await_args.kwargs["text"]
    assert "Remove 810099" in _keyboard_labels(
        context.bot.edit_message_text.await_args.kwargs["reply_markup"]
    )
    assert context.bot.send_message.await_count == 1
    assert context.bot.edit_message_text.await_args.kwargs["chat_id"] == ADMIN_ID
    assert context.bot.edit_message_text.await_args.kwargs["message_id"] == 77


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
    assert "Cannot revoke Environment Super-Admins." in query.edit_message_text.await_args.kwargs["text"]


async def _open_global_rule_wizard(context: SimpleNamespace) -> SimpleNamespace:
    await press(context, "requirements")
    await press(context, "reqscope", "g")
    return await press(context, "reqadd", "g")


def _keyboard_labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _private_input(user_id: int = ADMIN_ID, text: str | None = None, **attrs):
    message = SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=user_id, type="private"),
        reply_text=AsyncMock(),
        **attrs,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=message.chat,
        effective_message=message,
    )
    return update, message


async def _send_expected_panel_input(context: SimpleNamespace, update) -> None:
    with pytest.raises(ApplicationHandlerStop):
        await admin_panel_input(update, context)


async def test_add_category_flow_collects_name_and_forwarded_storage_channel(
    database: Database,
) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    categories = await press(context, "categories")
    assert "Add Category" in _keyboard_labels(categories.edit_message_text.await_args.kwargs["reply_markup"])
    category_list = await press(context, "catadd")

    assert state["view"] == "input_category_name"
    assert "category name" in category_list.edit_message_text.await_args.kwargs["text"]

    invalid, invalid_message = _private_input(text="Global")
    await _send_expected_panel_input(context, invalid)
    assert "reserved" in context.bot.edit_message_text.await_args.kwargs["text"]
    invalid_message.reply_text.assert_not_awaited()

    named, _ = _private_input(text="Education")
    await _send_expected_panel_input(context, named)
    assert state["input"] == {"kind": "category_storage"}
    assert state["view"] == "input_category_storage"

    forwarded, _ = _private_input(
        text=None,
        forward_origin=SimpleNamespace(
            type="channel", chat=SimpleNamespace(id=-100810040)
        ),
    )
    await _send_expected_panel_input(context, forwarded)

    created = await CategoryRepository(database).get_by_name("Education")
    assert created is not None and created.storage_channel_id == -100810040
    assert state["input"] is None
    assert "category_draft_name" not in state
    assert state["view"] == "categories"
    assert context.bot.send_message.await_count == 1
    assert context.bot.edit_message_text.await_count == 3
    assert "Category Education created." in context.bot.edit_message_text.await_args.kwargs["text"]


async def test_add_category_accepts_numeric_storage_channel_and_cancel(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    await press(context, "categories")
    await press(context, "catadd")
    named, _ = _private_input(text="Archive")
    await _send_expected_panel_input(context, named)
    target, _ = _private_input(text="-100810041")
    await _send_expected_panel_input(context, target)
    assert await CategoryRepository(database).get_by_name("Archive") is not None

    await press(context, "catadd")
    cancelled = await press(context, "inputcancel")
    assert state["view"] == "categories"
    assert state["input"] is None
    assert "Category creation cancelled." in cancelled.edit_message_text.await_args.kwargs["text"]


async def test_category_detail_link_and_delete_are_panel_managed(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    categories = CategoryRepository(database)
    parent = await categories.create("Courses", -100810042)
    child = await categories.create("Language", -100810043)
    context = make_context(database)
    await open_panel(context)
    await press(context, "categories")

    detail = await press(context, "cat", parent.id)
    detail_text = detail.edit_message_text.await_args.kwargs["text"]
    assert "Category: Courses" in detail_text
    assert "Storage channel: -100810042" in detail_text
    assert "Link to Child" in _keyboard_labels(detail.edit_message_text.await_args.kwargs["reply_markup"])

    await press(context, "catlink", parent.id)
    await press(context, "catlinkdo", parent.id, child.id)
    assert [value.name for value in (await categories.links_for(parent.id))[1]] == ["Language"]

    confirm = await press(context, "catdelete", parent.id)
    confirm_text = confirm.edit_message_text.await_args.kwargs["text"]
    assert "items will move to Default" in confirm_text
    assert "stored media will remain" in confirm_text
    assert "share links for those items will be revoked" in confirm_text
    assert "linked categories and their storage channels will remain" in confirm_text
    assert "requirements that become orphaned" in confirm_text
    assert {"Delete orphaned requirements", "Keep orphaned requirements", "Cancel"} <= set(
        _keyboard_labels(confirm.edit_message_text.await_args.kwargs["reply_markup"])
    )
    await press(context, "catdeletedo", parent.id, "keep")

    assert await categories.get(parent.id) is None
    assert await categories.get(child.id) is not None
    assert await categories.links_for(child.id) == ((), ())


async def test_rule_wizard_creates_fake_join_rule_without_rule_syntax(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)

    type_screen = await _open_global_rule_wizard(context)
    assert state["view"] == "rule_wizard_type"
    assert set(_keyboard_labels(type_screen.edit_message_text.await_args.kwargs["reply_markup"])) >= {
        "Join a Channel", "React to Posts", "Start a Bot", "Timed Wait", "Cancel",
    }
    assert "TYPE TARGET" not in type_screen.edit_message_text.await_args.kwargs["text"]
    assert not hasattr(panel_handlers, "_parse_rule_input")

    await press(context, "rule_type", "JOIN_CHAT")
    inbound, _ = _private_input(text="-100810020")
    await _send_expected_panel_input(context, inbound)
    await press(context, "rule_link_continue")
    await press(context, "rule_verify", "fake")
    await press(context, "rule_wait", 5)
    confirmation = await press(context, "rule_wait_done")
    assert "Fake (prompt + wait)" in confirmation.edit_message_text.await_args.kwargs["text"]
    saved = await press(context, "rule_save")

    rules = await RequirementRepository(database).list_for_scope(None)
    assert len(rules) == 1
    assert rules[0].type == "JOIN_CHAT"
    assert rules[0].target == "-100810020"
    assert rules[0].settings["is_fake"] is True
    assert rules[0].settings["wait_seconds"] == 5
    assert state["wizard"] is None and state["input"] is None
    assert state["view"] == "requirements_list"
    assert context.bot.send_message.await_count == 1
    assert saved.edit_message_text.await_count == 1


async def test_rule_wizard_saves_custom_telegram_prompt_link(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", "JOIN_CHAT")
    target, _ = _private_input(text="-100810070")
    await _send_expected_panel_input(context, target)
    link_screen = context.bot.edit_message_text.await_args.kwargs
    assert state["view"] == "rule_wizard_link"
    assert "Opening a link does not prove membership" in link_screen["text"]

    await press(context, "rule_link_edit")
    invalid, _ = _private_input(text="https://example.com/channel")
    await _send_expected_panel_input(context, invalid)
    assert state["input"] == {"kind": "rule_link"}
    assert "https://t.me/channelname" in context.bot.edit_message_text.await_args.kwargs["text"]

    valid, _ = _private_input(text="https://t.me/+AbCdEf123")
    await _send_expected_panel_input(context, valid)
    assert state["wizard"]["link_url"] == "https://t.me/+AbCdEf123"
    await press(context, "rule_link_continue")
    await press(context, "rule_verify", "fake")
    await press(context, "rule_wait", 0)
    confirmation = await press(context, "rule_wait_done")
    assert "Prompt link: https://t.me/+AbCdEf123" in confirmation.edit_message_text.await_args.kwargs["text"]
    await press(context, "rule_save")

    rule = (await RequirementRepository(database).list_for_scope(None))[0]
    assert rule.settings["link_url"] == "https://t.me/+AbCdEf123"


async def test_forwarded_public_group_auto_adds_link_to_prompt(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", "JOIN_CHAT")
    forwarded, _ = _private_input(
        text=None,
        forward_origin=SimpleNamespace(
            type="chat", chat=SimpleNamespace(id=-100810071, username="study_group")
        ),
    )
    await _send_expected_panel_input(context, forwarded)

    state = context.user_data["m6_admin_panel"]
    assert state["wizard"]["target"] == -100810071
    assert state["wizard"]["link_url"] == "https://t.me/study_group"
    assert "Prompt link: https://t.me/study_group" in context.bot.edit_message_text.await_args.kwargs["text"]


@pytest.mark.parametrize(
    ("rule_type", "forward_style", "expected_id"),
    [
        ("JOIN_CHAT", "origin_channel", -100810021),
        ("JOIN_CHAT", "origin_group", -100810025),
        ("REACT", "legacy_channel", -100810022),
        ("START_BOT", "origin_user", 810_023),
        ("START_BOT", "legacy_user", 810_024),
    ],
)
async def test_rule_target_is_extracted_from_forwarded_channel_or_user(
    database: Database, rule_type: str, forward_style: str, expected_id: int
) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", rule_type)

    attrs = {}
    if forward_style == "origin_channel":
        attrs["forward_origin"] = SimpleNamespace(
            type="channel", chat=SimpleNamespace(id=expected_id)
        )
    elif forward_style == "origin_group":
        attrs["forward_origin"] = SimpleNamespace(
            type="chat", chat=SimpleNamespace(id=expected_id)
        )
    elif forward_style == "legacy_channel":
        attrs["forward_from_chat"] = SimpleNamespace(id=expected_id)
    elif forward_style == "origin_user":
        attrs["forward_origin"] = SimpleNamespace(
            type="user", sender_user=SimpleNamespace(id=expected_id)
        )
    else:
        attrs["forward_from"] = SimpleNamespace(id=expected_id)
    inbound, _ = _private_input(text=None, **attrs)
    await _send_expected_panel_input(context, inbound)

    assert state["wizard"]["target"] == expected_id
    assert state["view"] == "rule_wizard_link"


@pytest.mark.parametrize(
    ("rule_type", "forwarded_attributes"),
    [
        (
            "JOIN_CHAT",
            {"forward_origin": SimpleNamespace(type="user", sender_user=SimpleNamespace(id=810_025))},
        ),
        (
            "REACT",
            {"forward_from": SimpleNamespace(id=810_026)},
        ),
        (
            "START_BOT",
            {"forward_origin": SimpleNamespace(type="channel", chat=SimpleNamespace(id=-100810027))},
        ),
    ],
)
async def test_rule_wizard_rejects_forwarded_targets_of_wrong_type(
    database: Database, rule_type: str, forwarded_attributes: dict[str, object]
) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", rule_type)

    inbound, _ = _private_input(text="810099", **forwarded_attributes)
    await _send_expected_panel_input(context, inbound)

    assert state["wizard"].get("target") is None
    assert state["view"] == "rule_wizard_target"
    panel = context.bot.edit_message_text.await_args.kwargs
    assert "doesn't match this rule type" in panel["text"]
    assert "Cancel" in _keyboard_labels(panel["reply_markup"])


async def test_real_join_wizard_warns_when_bot_is_not_admin(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    context.bot.id = 998877
    context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    state = await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", "JOIN_CHAT")
    inbound, _ = _private_input(text="-100810025")
    await _send_expected_panel_input(context, inbound)
    await press(context, "rule_link_continue")

    warning = await press(context, "rule_verify", "real")

    assert warning.edit_message_text.await_args.kwargs["text"] == (
        "⚠️ Bot is not an admin in this channel. Real checks may fail."
    )
    assert "Cancel" in _keyboard_labels(warning.edit_message_text.await_args.kwargs["reply_markup"])
    context.bot.get_chat_member.assert_awaited_once_with(chat_id=-100810025, user_id=998877)
    await press(context, "rule_fake")
    assert state["wizard"]["is_fake"] is True
    assert state["view"] == "rule_wizard_wait"


async def test_real_join_wizard_distinguishes_unavailable_admin_lookup(
    database: Database,
) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    context.bot.id = 998877
    context.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("API unavailable"))
    await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", "JOIN_CHAT")
    inbound, _ = _private_input(text="-100810025")
    await _send_expected_panel_input(context, inbound)
    await press(context, "rule_link_continue")

    warning = await press(context, "rule_verify", "real")

    warning_text = warning.edit_message_text.await_args.kwargs["text"]
    assert "Could not verify whether the bot is an admin" in warning_text
    assert "not an admin" not in warning_text
    assert "Continue Anyway" in _keyboard_labels(
        warning.edit_message_text.await_args.kwargs["reply_markup"]
    )
    context.bot.get_chat_member.assert_awaited_once_with(
        chat_id=-100810025, user_id=998877
    )


async def test_react_wizard_saves_button_selected_windows(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", "REACT")
    inbound, _ = _private_input(
        text=None,
        forward_origin=SimpleNamespace(
            type="channel", chat=SimpleNamespace(id=-100810026)
        ),
    )
    await _send_expected_panel_input(context, inbound)
    await press(context, "rule_link_continue")
    await press(context, "rule_verify", "fake")
    await press(context, "rule_wait", 3)
    await press(context, "rule_wait_done")
    window_screen = await press(context, "rule_message_window", 10)
    assert "Cancel" in _keyboard_labels(window_screen.edit_message_text.await_args.kwargs["reply_markup"])
    window_markup = window_screen.edit_message_text.await_args.kwargs["reply_markup"]
    message_choices = {
        int(button.callback_data.rsplit(":", 1)[-1])
        for row in window_markup.inline_keyboard
        for button in row
        if ":rule_message_window:" in button.callback_data
    }
    day_choices = {
        int(button.callback_data.rsplit(":", 1)[-1])
        for row in window_markup.inline_keyboard
        for button in row
        if ":rule_day_window:" in button.callback_data
    }
    assert message_choices == {0, 3, 5, 10}
    assert day_choices == {0, 7, 30}
    await press(context, "rule_day_window", 7)
    await press(context, "rule_windows_done")
    await press(context, "rule_save")

    rule = (await RequirementRepository(database).list_for_scope(None))[0]
    assert rule.type == "REACT"
    assert rule.target == "-100810026"
    assert rule.settings["last_x_messages"] == 10
    assert rule.settings["last_x_days"] == 7
    assert rule.settings["wait_seconds"] == 3
    assert state["wizard"] is None


async def test_timed_wait_wizard_skips_target_and_saves_zero_wait(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    await _open_global_rule_wizard(context)
    wait_screen = await press(context, "rule_type", "TIMED_WAIT")
    assert state["wizard"]["target"] is None
    assert state["view"] == "rule_wizard_wait"
    assert "Real" not in _keyboard_labels(wait_screen.edit_message_text.await_args.kwargs["reply_markup"])
    wait_choices = {
        int(button.callback_data.rsplit(":", 1)[-1])
        for row in wait_screen.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
        if ":rule_wait:" in button.callback_data
    }
    assert wait_choices == {0, 3, 5, 10, 30, 60}
    await press(context, "rule_wait", 0)
    await press(context, "rule_wait_done")
    await press(context, "rule_save")

    rule = (await RequirementRepository(database).list_for_scope(None))[0]
    assert rule.type == "TIMED_WAIT"
    assert rule.target is None
    assert rule.settings["wait_seconds"] == 0


@pytest.mark.parametrize(
    "screen", ["type", "target", "link", "link_input", "verification", "wait", "react_windows", "confirm", "join_warning"]
)
async def test_every_rule_wizard_screen_has_cancel_and_clears_state(
    database: Database, screen: str
) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    query = await _open_global_rule_wizard(context)
    if screen == "target":
        query = await press(context, "rule_type", "JOIN_CHAT")
    elif screen == "link":
        await press(context, "rule_type", "JOIN_CHAT")
        inbound, _ = _private_input(text="-100810027")
        await _send_expected_panel_input(context, inbound)
        query = SimpleNamespace(edit_message_text=AsyncMock())
        await panel_handlers._show_rule_link(context, state, query=query)
    elif screen == "link_input":
        await press(context, "rule_type", "JOIN_CHAT")
        inbound, _ = _private_input(text="-100810027")
        await _send_expected_panel_input(context, inbound)
        query = await press(context, "rule_link_edit")
    elif screen == "verification":
        await press(context, "rule_type", "JOIN_CHAT")
        inbound, _ = _private_input(text="-100810027")
        await _send_expected_panel_input(context, inbound)
        await press(context, "rule_link_continue")
        query = SimpleNamespace(edit_message_text=AsyncMock())
        await panel_handlers._show_rule_verification(context, state, query=query)
    elif screen == "wait":
        await press(context, "rule_type", "TIMED_WAIT")
        query = await press(context, "rule_wait", 0)
    elif screen == "react_windows":
        await press(context, "rule_type", "REACT")
        inbound, _ = _private_input(text="-100810027")
        await _send_expected_panel_input(context, inbound)
        await press(context, "rule_link_continue")
        await press(context, "rule_verify", "fake")
        query = await press(context, "rule_wait_done")
    elif screen == "confirm":
        await press(context, "rule_type", "TIMED_WAIT")
        query = await press(context, "rule_wait_done")
    elif screen == "join_warning":
        context.bot.id = 998877
        context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
        await press(context, "rule_type", "JOIN_CHAT")
        inbound, _ = _private_input(text="-100810028")
        await _send_expected_panel_input(context, inbound)
        await press(context, "rule_link_continue")
        query = await press(context, "rule_verify", "real")

    assert "Cancel" in _keyboard_labels(query.edit_message_text.await_args.kwargs["reply_markup"])
    await press(context, "rule_cancel")
    assert state["wizard"] is None
    assert state["input"] is None
    assert state["view"] == "requirements_list"


async def test_start_bot_wizard_explains_real_verification_is_unsupported(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", "START_BOT")
    inbound, _ = _private_input(text="810029")
    await _send_expected_panel_input(context, inbound)
    verification = await press(context, "rule_link_continue")

    text = verification.edit_message_text.await_args.kwargs["text"]
    assert "Real START_BOT verification is not supported" in text
    labels = _keyboard_labels(verification.edit_message_text.await_args.kwargs["reply_markup"])
    assert "Fake (Prompt + Wait)" in labels
    assert "Real" not in labels


async def test_start_bot_target_rejects_username_input(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    await _open_global_rule_wizard(context)
    await press(context, "rule_type", "START_BOT")

    inbound, _ = _private_input(text="@ExampleBot")
    await _send_expected_panel_input(context, inbound)

    assert state["wizard"].get("target") is None
    assert state["view"] == "rule_wizard_target"
    panel = context.bot.edit_message_text.await_args.kwargs
    assert "numeric ID" in panel["text"]
    assert "Cancel" in _keyboard_labels(panel["reply_markup"])


async def test_staff_lists_show_separate_add_admin_and_add_uploader_actions(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    await open_panel(context)
    await press(context, "staff")
    admins = await press(context, "staffrole", "admins", 0)
    assert "Add Admin" in _keyboard_labels(admins.edit_message_text.await_args.kwargs["reply_markup"])
    assert "Add" not in _keyboard_labels(admins.edit_message_text.await_args.kwargs["reply_markup"])
    uploaders = await press(context, "staffrole", "uploaders", 0)
    assert "Add Uploader" in _keyboard_labels(uploaders.edit_message_text.await_args.kwargs["reply_markup"])


async def test_settings_panel_explains_and_toggles_regular_user_lock(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    await open_panel(context)

    settings = await press(context, "settings")
    text = settings.edit_message_text.await_args.kwargs["text"]
    assert "regular users can use only /start" in text
    assert "Admins and approved Uploaders are unaffected." in text
    assert "Lock Bot (OFF)" in _keyboard_labels(settings.edit_message_text.await_args.kwargs["reply_markup"])
    assert await panel_handlers.BotSettingsRepository(database).user_commands_enabled()
    assert await panel_handlers.BotSettingsRepository(database).real_check_failure_policy() == (
        panel_handlers.BotSettingsRepository.REAL_CHECK_FAIL_CLOSED
    )
    assert "deny without using a share link" in text
    assert any("Real-check errors: Deny" in label for label in _keyboard_labels(
        settings.edit_message_text.await_args.kwargs["reply_markup"]
    ))

    wait_policy = await press(
        context,
        "realpolicy_set",
        panel_handlers.BotSettingsRepository.REAL_CHECK_WAIT_FALLBACK,
    )
    assert await panel_handlers.BotSettingsRepository(database).real_check_failure_policy() == (
        panel_handlers.BotSettingsRepository.REAL_CHECK_WAIT_FALLBACK
    )
    assert "use the rule’s configured wait" in wait_policy.edit_message_text.await_args.kwargs["text"]
    assert any("Real-check errors: Wait fallback" in label for label in _keyboard_labels(
        wait_policy.edit_message_text.await_args.kwargs["reply_markup"]
    ))

    locked = await press(context, "lock")
    assert not await panel_handlers.BotSettingsRepository(database).user_commands_enabled()
    assert "Regular-user command/upload lock: ON" in locked.edit_message_text.await_args.kwargs["text"]
    assert "Lock Bot (ON)" in _keyboard_labels(locked.edit_message_text.await_args.kwargs["reply_markup"])

    unlocked = await press(context, "unlock")
    assert await panel_handlers.BotSettingsRepository(database).user_commands_enabled()
    assert "Lock Bot (OFF)" in _keyboard_labels(unlocked.edit_message_text.await_args.kwargs["reply_markup"])


@pytest.mark.parametrize("already_env_admin", [False, True])
async def test_add_admin_duplicate_paths_show_clear_notice(
    database: Database, already_env_admin: bool
) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    target_id = ADMIN_ID if already_env_admin else NEW_ADMIN_ID
    if not already_env_admin:
        await AdminRepository(database).add(user_id=target_id, added_by=ADMIN_ID)
    await open_panel(context)
    await press(context, "staff")
    await press(context, "staffrole", "admins", 0)
    await press(context, "staffadd", "admins")
    inbound, _ = _private_input(text=str(target_id))
    await _send_expected_panel_input(context, inbound)

    result_text = context.bot.edit_message_text.await_args.kwargs["text"]
    if already_env_admin:
        assert "already an environment super-admin" in result_text
    else:
        assert "User is already an admin." in result_text


@pytest.mark.parametrize("pending_mode", ["wizard", "staff_input"])
async def test_pending_panel_state_expires_after_five_minutes(
    database: Database, pending_mode: str
) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    if pending_mode == "wizard":
        await press(context, "requirements")
        await press(context, "reqscope", "g")
        with patch.object(panel_handlers.time, "monotonic", return_value=100.0):
            await press(context, "reqadd", "g")
        assert state["wizard"] is not None
    else:
        await press(context, "staff")
        await press(context, "staffrole", "admins", 0)
        with patch.object(panel_handlers.time, "monotonic", return_value=100.0):
            await press(context, "staffadd", "admins")
        assert state["input"] is not None

    update, message = _private_input(text="ordinary message")
    with patch.object(panel_handlers.time, "monotonic", return_value=401.0):
        await admin_panel_input(update, context)

    assert state["view"] == "home"
    assert state["wizard"] is None and state["input"] is None
    assert context.bot.edit_message_text.await_args.kwargs["text"] == "Admin Control Panel"
    message.reply_text.assert_not_awaited()
    assert not await AdminRepository(database).is_admin(NEW_ADMIN_ID)


async def test_idle_panel_flow_expires_before_an_unrelated_command(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    context = make_context(database)
    state = await open_panel(context)
    await press(context, "requirements")
    await press(context, "reqscope", "g")
    with patch.object(panel_handlers.time, "monotonic", return_value=100.0):
        await press(context, "reqadd", "g")

    command, message = _private_input(text="/share 12")
    with patch.object(panel_handlers.time, "monotonic", return_value=401.0):
        await expire_panel_flow_before_command(command, context)

    assert state["view"] == "home"
    assert state["wizard"] is None and state["input"] is None
    assert context.bot.edit_message_text.await_args.kwargs["text"] == "Admin Control Panel"
    message.reply_text.assert_not_awaited()


async def _prepare_shared_rule(database: Database):
    categories = CategoryRepository(database)
    first = await categories.create("Scope A", -100810030)
    second = await categories.create("Scope B", -100810031)
    rule = await RequirementRepository(database).create(
        category_id=first.id,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=False,
        wait_seconds=0,
    )
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO category_requirements (category_id, requirement_id) VALUES (?, ?)",
            (second.id, rule.id),
        )
    return first, second, rule


async def _open_category_rules(context: SimpleNamespace, category_id: int) -> None:
    await press(context, "requirements")
    await press(context, "reqscope", category_id)


async def test_shared_rule_offers_unlink_here_or_delete_everywhere(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    first, second, rule = await _prepare_shared_rule(database)
    context = make_context(database)
    await open_panel(context)
    await _open_category_rules(context, first.id)

    choice_screen = await press(context, "reqdelconfirm", first.id, rule.id)

    labels = _keyboard_labels(choice_screen.edit_message_text.await_args.kwargs["reply_markup"])
    assert {"Unlink Here", "Delete Everywhere", "Cancel"} <= set(labels)
    assert second.name in choice_screen.edit_message_text.await_args.kwargs["text"]
    assert await RequirementRepository(database).get(rule.id) is not None


async def test_shared_rule_unlink_preserves_the_other_scope(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    first, second, rule = await _prepare_shared_rule(database)
    context = make_context(database)
    await open_panel(context)
    await _open_category_rules(context, first.id)
    await press(context, "reqdelconfirm", first.id, rule.id)

    await press(context, "reqdeletedo", first.id, rule.id, "unlink")

    assert await RequirementRepository(database).get(rule.id) is not None
    assert await RequirementRepository(database).scopes_for(rule.id) == (second.name,)


async def test_shared_rule_delete_everywhere_removes_all_associations(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    first, _, rule = await _prepare_shared_rule(database)
    context = make_context(database)
    await open_panel(context)
    await _open_category_rules(context, first.id)
    await press(context, "reqdelconfirm", first.id, rule.id)

    await press(context, "reqdeletedo", first.id, rule.id, "all")

    assert await RequirementRepository(database).get(rule.id) is None
    assert await RequirementRepository(database).scopes_for(rule.id) == ()


async def test_unshared_rule_is_deleted_directly_without_choice_screen(database: Database) -> None:
    configure_admin_ids((ADMIN_ID,))
    first = await CategoryRepository(database).create("Only Scope", -100810032)
    rule = await RequirementRepository(database).create(
        category_id=first.id,
        requirement_type="TIMED_WAIT",
        target=None,
        is_fake=False,
        wait_seconds=0,
    )
    context = make_context(database)
    state = await open_panel(context)
    await _open_category_rules(context, first.id)

    result = await press(context, "reqdelconfirm", first.id, rule.id)

    assert await RequirementRepository(database).get(rule.id) is None
    assert state["view"] == "requirements_list"
    assert result.edit_message_text.await_count == 1
    assert "Delete Everywhere" not in _keyboard_labels(result.edit_message_text.await_args.kwargs["reply_markup"])
