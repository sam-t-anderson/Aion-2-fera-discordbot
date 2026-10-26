"""What a player sees when a command, button or form fails, and what the log records."""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

import errors


def interaction(done=False):
    it = MagicMock()
    it.guild_id, it.channel_id, it.user.id = 11, 22, 33
    it.response.is_done.return_value = done
    it.response.send_message = AsyncMock()
    it.followup.send = AsyncMock()
    it.command.qualified_name = "event create"
    return it


def answer(it):
    """The private reply the player got, whichever way it was sent."""
    for sent in (it.response.send_message, it.followup.send):
        if sent.await_count:
            assert sent.await_args.kwargs == {"ephemeral": True}
            return sent.await_args.args[0]
    return None


def test_a_players_mistake_is_explained():
    forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access")
    cases = {
        app_commands.MissingPermissions(["manage_guild"]): "You need the Manage Guild permission",
        app_commands.BotMissingPermissions(["send_messages", "embed_links"]): "I need the Send Messages, Embed Links",
        app_commands.NoPrivateMessage(): "Use this command in a server.",
        app_commands.CommandOnCooldown(app_commands.Cooldown(1, 30), 12.4): "try again in 12 seconds",
        app_commands.CheckFailure(): "You can't use this here.",
        forbidden: "Discord refused me there",
    }
    for error, words in cases.items():
        assert words in errors.explain(error), error


def test_an_error_inside_a_command_is_unwrapped():
    command = MagicMock()
    command.name = "event"
    wrapped = app_commands.CommandInvokeError(command, app_commands.NoPrivateMessage())
    assert errors.explain(wrapped) == "Use this command in a server."
    assert errors.explain(app_commands.CommandInvokeError(command, ValueError("bug"))) is None


def test_a_fault_of_the_bots_is_logged_with_where_it_happened(caplog):
    it = interaction()
    command = MagicMock()
    command.name = "event"
    with caplog.at_level(logging.ERROR, logger="aion2.errors"):
        asyncio.run(errors.on_command_error(it, app_commands.CommandInvokeError(command, KeyError("starts_at"))))
    assert answer(it) == errors.FAILED
    record = caplog.records[-1]
    assert "/event create failed in guild 11 channel 22 for user 33" in record.getMessage()
    assert record.exc_info[0] is KeyError                   # the traceback is the original error's


def test_a_mistake_is_answered_without_an_error_in_the_log(caplog):
    it = interaction()
    with caplog.at_level(logging.ERROR, logger="aion2.errors"):
        asyncio.run(errors.on_command_error(it, app_commands.MissingPermissions(["manage_guild"])))
    assert answer(it) == "You need the Manage Guild permission to use this."
    assert not caplog.records


def test_an_answered_interaction_gets_a_follow_up():
    it = interaction(done=True)
    asyncio.run(errors.reply(it, "hello"))
    assert answer(it) == "hello" and not it.response.send_message.await_count


def test_an_expired_interaction_is_left_alone():
    it = interaction()
    it.response.send_message.side_effect = discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown interaction")
    asyncio.run(errors.reply(it, "hello"))                  # no exception: the log has the failure already


class Button:
    def __init__(self, fail):
        self.fail = fail

    @errors.guarded
    async def callback(self, interaction):
        if self.fail:
            raise RuntimeError("database is locked")
        await interaction.response.send_message("Ticked.", ephemeral=True)
        return "done"


def test_a_button_that_fails_is_answered_and_logged(caplog):
    it = interaction()
    with caplog.at_level(logging.ERROR, logger="aion2.errors"):
        asyncio.run(Button(fail=True).callback(it))
    assert answer(it) == errors.FAILED
    assert "Button.callback failed in guild 11" in caplog.records[-1].getMessage()


def test_a_button_that_works_is_untouched():
    it = interaction()
    assert asyncio.run(Button(fail=False).callback(it)) == "done"
    assert answer(it) == "Ticked."
    assert Button.callback.__name__ == "callback"


def test_every_button_and_form_is_guarded():
    import checklist
    import dkp
    import events
    handlers = []
    for module in (checklist, dkp, events):
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, (discord.ui.DynamicItem, discord.ui.Modal)):
                for name in ("callback", "on_submit"):
                    if name in vars(value):
                        handlers.append((value.__name__, vars(value)[name]))
    assert len(handlers) >= 11
    unguarded = [name for name, fn in handlers if getattr(fn, "__wrapped__", None) is None]
    assert not unguarded, unguarded
