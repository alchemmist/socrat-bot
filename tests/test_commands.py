import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TimedOut

from socrat_bot import app
from socrat_bot.commands import (
    COMMANDS,
    LANGUAGES,
    command_menu_loop,
    menu,
    sync_commands,
    sync_owner_menu,
)


def test_registry_is_valid_and_binds_every_real_command_handler():
    names = [c.name for c in COMMANDS]
    assert len(names) == len(set(names))
    assert set(names) == {"start", "alerts", "unsubscribe", "clients", "alertlevel", "mailing"}
    for c in COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.name)
        assert 1 <= len(c.description) <= 256
        assert callable(getattr(app, c.handler))
    assert {c.command for c in menu()} == {"start", "alerts", "unsubscribe"}
    assert {c.command for c in menu(owner=True)} == set(names)


def test_sync_publishes_complete_scoped_language_lists():
    bot = SimpleNamespace(set_my_commands=AsyncMock(return_value=True))
    assert asyncio.run(sync_commands(bot, {123, 456}))
    calls = list(bot.set_my_commands.call_args_list)
    assert len(calls) == 4 * len(LANGUAGES)
    scopes = set()
    for call in calls:
        commands = {c.command for c in call.args[0]}
        scope = call.kwargs["scope"]
        scopes.add(scope.type)
        assert call.kwargs["language_code"] in LANGUAGES
        if scope.type == "chat":
            assert scope.chat_id in {123, 456}
            assert commands == {c.name for c in COMMANDS}
        else:
            assert commands == {"start", "alerts", "unsubscribe"}
    assert scopes == {"default", "all_private_chats", "chat"}
    # Repeat publishing remains a full replacement, never an incremental one-command list.
    assert asyncio.run(sync_commands(bot, {123, 456}))
    assert bot.set_my_commands.await_count == 2 * len(calls)


def test_owner_menu_can_be_refreshed_after_bootstrap_start():
    bot = SimpleNamespace(set_my_commands=AsyncMock(return_value=True))
    assert asyncio.run(sync_owner_menu(bot, 123))
    assert bot.set_my_commands.await_count == len(LANGUAGES)
    assert all(call.kwargs["scope"].chat_id == 123 for call in bot.set_my_commands.call_args_list)


def test_sync_continues_after_safe_telegram_failure(caplog):
    bot = SimpleNamespace(set_my_commands=AsyncMock(side_effect=[TimedOut("PRIVATE-TOKEN-CANARY")] + [True] * 8))
    assert not asyncio.run(sync_commands(bot, {123}))
    assert bot.set_my_commands.await_count == 9
    assert "PRIVATE-TOKEN-CANARY" not in caplog.text


def test_command_sync_retries_without_blocking_webhook(monkeypatch):
    from socrat_bot import commands

    sync = AsyncMock(side_effect=[False, True])
    sleep = AsyncMock()
    monkeypatch.setattr(commands, "sync_commands", sync)
    monkeypatch.setattr(commands.asyncio, "sleep", sleep)
    asyncio.run(command_menu_loop(SimpleNamespace(), {123}))
    assert sync.await_count == 2
    sleep.assert_awaited_once_with(60)
