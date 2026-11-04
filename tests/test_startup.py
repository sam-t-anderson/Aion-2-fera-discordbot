"""Starting up: the health endpoint answers from before the login, and the slash commands are registered with
Discord only when they changed (or a week after the last time), beside the connection rather than in front of it.

The health endpoint used to start last, after the login, the database, every feature and the command
registration. A start that stalled or failed in any of those showed only as "Connection refused" to Docker's
health check, and registering the commands on every start let a Discord rate limit hold the whole bot."""
import asyncio
import socket
import time
from unittest.mock import patch

import aiohttp
import discord

import bot as bot_module
import config
import db


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StopStart(Exception):
    pass


def test_the_health_endpoint_answers_while_the_bot_logs_in():
    port = free_port()
    seen = {}

    async def run():
        bot = bot_module.AionBot()

        async def login(self, token):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as response:
                    seen["status"], seen["body"] = response.status, await response.json()
            raise StopStart

        try:
            with patch.object(config, "HEALTH_PORT", port), patch.object(bot_module.AionBot, "login", login):
                await bot.start("not a real token")
        except StopStart:
            pass
        finally:
            await bot.close()
    asyncio.run(run())
    assert seen["status"] == 503
    assert seen["body"]["status"] == "offline" and seen["body"]["phase"] == "logging in"
    assert seen["body"]["commands"] == "not registered yet"


def make_bot(description="Says pong."):
    bot = bot_module.AionBot()
    bot._connection.application_id = 1234                   # what the login sets

    @bot.tree.command(name="ping", description=description)
    async def ping(interaction: discord.Interaction):
        pass
    return bot


def test_the_commands_are_registered_only_when_they_changed(tmp_path):
    db.init(str(tmp_path / "t.db"))
    calls = []

    async def sync(*, guild=None):
        calls.append(guild)
        return []

    async def run(bot):
        bot.tree.sync = sync
        await bot.sync_commands()
        return bot.commands_status
    assert asyncio.run(run(make_bot())) == "registered" and len(calls) == 1
    assert asyncio.run(run(make_bot())) == "unchanged" and len(calls) == 1          # the same commands: not again
    assert asyncio.run(run(make_bot("Says pong, faster."))) == "registered" and len(calls) == 2
    week_ago = int(time.time()) - bot_module.COMMANDS_RESYNC_SECONDS - 60
    db.meta_set("commands_synced_at", str(week_ago))
    assert asyncio.run(run(make_bot("Says pong, faster."))) == "registered" and len(calls) == 3   # a week on


def test_a_failed_registration_is_logged_and_tried_again_next_start(tmp_path):
    db.init(str(tmp_path / "t.db"))

    async def refused(*, guild=None):
        raise discord.HTTPException(type("Response", (), {"status": 429, "reason": "Too Many Requests"})(), "slow down")

    async def run():
        bot = make_bot()
        bot.tree.sync = refused
        await bot.sync_commands()                        # does not raise: the bot carries on without it
        return bot.commands_status
    assert asyncio.run(run()).startswith("registration failed")
    assert db.meta_get("commands_synced") is None        # so the next start registers them
