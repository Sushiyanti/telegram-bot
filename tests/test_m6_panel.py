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
from media_bot.db.rules import CategoryRepository, RequirementRepository
from media_bot.handlers.panel import (
    _home_markup,
    admin_panel_callback,
    admin_panel_command,
    admin_panel_input,
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
    await press(context, "rule_verify", "fake")
    await press(context, "rule_wait", 5)
    confirmation = await press(context, "rule_wait_done")
    assert "Fake (wait only)" in confirmation.edit_message_text.await_args.kwargs["text"]
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


@pytest.mark.parametrize(
    ("rule_type", "forward_style", "expected_id"),
    [
        ("JOIN_CHAT", "origin_channel", -100810021),
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
    assert state["view"] == "rule_wizard_verification"


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

    warning = await press(context, "rule_verify", "real")

    assert warning.edit_message_text.await_args.kwargs["text"] == (
        "⚠️ Bot is not admin in this channel. Real checks will fail."
    )
    assert "Cancel" in _keyboard_labels(warning.edit_message_text.await_args.kwargs["reply_markup"])
    context.bot.get_chat_member.assert_awaited_once_with(chat_id=-100810025, user_id=998877)
    await press(context, "rule_fake")
    assert state["wizard"]["is_fake"] is True
    assert state["view"] == "rule_wizard_wait"


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
    await press(context, "rule_verify", "fake")
    await press(context, "rule_wait", 3)
    await press(context, "rule_wait_done")
    window_screen = await press(context, "rule_message_window", 10)
    assert "Cancel" in _keyboard_labels(window_screen.edit_message_text.await_args.kwargs["reply_markup"])
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
    await press(context, "rule_type", "TIMED_WAIT")
    assert state["wizard"]["target"] is None
    assert state["view"] == "rule_wizard_verification"
    await press(context, "rule_verify", "fake")
    await press(context, "rule_wait", 0)
    await press(context, "rule_wait_done")
    await press(context, "rule_save")

    rule = (await RequirementRepository(database).list_for_scope(None))[0]
    assert rule.type == "TIMED_WAIT"
    assert rule.target is None
    assert rule.settings["wait_seconds"] == 0


@pytest.mark.parametrize(
    "screen", ["type", "target", "verification", "wait", "react_windows", "confirm", "join_warning"]
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
    elif screen == "verification":
        query = await press(context, "rule_type", "TIMED_WAIT")
    elif screen == "wait":
        await press(context, "rule_type", "TIMED_WAIT")
        query = await press(context, "rule_verify", "fake")
    elif screen == "react_windows":
        await press(context, "rule_type", "REACT")
        inbound, _ = _private_input(text="-100810027")
        await _send_expected_panel_input(context, inbound)
        await press(context, "rule_verify", "fake")
        query = await press(context, "rule_wait_done")
    elif screen == "confirm":
        await press(context, "rule_type", "TIMED_WAIT")
        await press(context, "rule_verify", "fake")
        query = await press(context, "rule_wait_done")
    elif screen == "join_warning":
        context.bot.id = 998877
        context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
        await press(context, "rule_type", "JOIN_CHAT")
        inbound, _ = _private_input(text="-100810028")
        await _send_expected_panel_input(context, inbound)
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

    verification = context.bot.edit_message_text.await_args.kwargs
    assert "Real START_BOT verification is not supported" in verification["text"]
    labels = _keyboard_labels(verification["reply_markup"])
    assert "Fake (Wait Only)" in labels
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
