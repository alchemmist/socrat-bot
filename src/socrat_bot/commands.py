"""Canonical handlers and Telegram autocomplete menus (not an authorization boundary)."""

import asyncio
from dataclasses import dataclass

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from telegram.error import TelegramError

from socrat_bot.structured_logging import configure_logging

log = configure_logging()


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    handler: str
    owner_only: bool = False


COMMANDS = (
    Command("start", "Open the bot and subscription controls", "on_start"),
    Command("alerts", "View alert settings and choose a receive level", "on_alerts"),
    Command("unsubscribe", "Stop alert notifications; keep your approval", "on_unsubscribe"),
    Command("clients", "List opted-in VPN clients", "on_clients", owner_only=True),
    Command("alertlevel", "Assign an admin level: /alertlevel TELEGRAM_ID LEVEL", "on_alertlevel", owner_only=True),
    Command("mailing", "Send or schedule a VPN mailing: /mailing TEXT", "on_mailing", owner_only=True),
)
# Language-independent fallback plus languages used by this bot's users. Descriptions
# stay English, matching the bot UI. Explicit entries replace old BotFather overrides.
LANGUAGES = ("", "en", "ru")


def menu(owner: bool = False) -> list[BotCommand]:
    return [BotCommand(c.name, c.description) for c in COMMANDS if owner or not c.owner_only]


async def _set_scope(bot: Bot, scope, owner: bool) -> bool:
    success = True
    for language in LANGUAGES:
        try:
            accepted = await bot.set_my_commands(
                menu(owner), scope=scope, language_code=language,
                connect_timeout=5, read_timeout=10, write_timeout=10, pool_timeout=5,
            )
            if not accepted:
                success = False
        except TelegramError:
            # Never serialize Telegram exceptions: they can contain token-bearing URLs.
            success = False
    return success


async def sync_owner_menu(bot: Bot, chat_id: int) -> bool:
    return await _set_scope(bot, BotCommandScopeChat(chat_id), owner=True)


async def sync_commands(bot: Bot, owner_ids: set[int]) -> bool:
    """Publish complete lists. One failed scope must not prevent the others."""
    success = await _set_scope(bot, BotCommandScopeDefault(), owner=False)
    success = await _set_scope(bot, BotCommandScopeAllPrivateChats(), owner=False) and success
    for chat_id in sorted(owner_ids):
        success = await sync_owner_menu(bot, chat_id) and success
    log.info("telegram commands synced" if success else "telegram commands failed")
    return success


async def command_menu_loop(bot: Bot, owner_ids: set[int]) -> None:
    # Cosmetic API errors must not stop webhook/alert delivery. Reconcile on retry,
    # and terminate once all managed scopes have accepted the canonical lists.
    while not await sync_commands(bot, owner_ids):
        await asyncio.sleep(60)
