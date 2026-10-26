"""What a player sees when a command or a button fails, and what the log records.

Without this, a slash command that raised showed only Discord's "The application did not respond", and a
button or form that raised showed "This interaction failed" (discord.py logs a dynamic button's error
itself and offers no hook). Now the player gets a short private answer, and the log names the server,
channel, user and what failed."""
import functools
import logging

import discord
from discord import app_commands

log = logging.getLogger("aion2.errors")

FAILED = "Something went wrong there and it has been logged. Try again in a moment."


async def reply(interaction: discord.Interaction, text: str) -> None:
    """Answer privately, whether or not the interaction was answered already."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass                                  # the interaction expired or was deleted: the log has it


def explain(error: Exception) -> str | None:
    """A player's own mistake, in words; None for a fault of the bot's."""
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, app_commands.MissingPermissions):
        needed = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        return f"You need the {needed} permission to use this."
    if isinstance(error, app_commands.BotMissingPermissions):
        needed = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        return f"I need the {needed} permission here to do that."
    if isinstance(error, app_commands.NoPrivateMessage):
        return "Use this command in a server."
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"Slow down a little: try again in {error.retry_after:.0f} seconds."
    if isinstance(error, app_commands.TransformerError):
        return f"That value isn't valid: {error.value!r}."
    if isinstance(error, app_commands.CheckFailure):
        return "You can't use this here."
    if isinstance(error, discord.Forbidden):
        return "Discord refused me there. Check that I can see the channel and send messages and embeds in it."
    return None


async def report(interaction: discord.Interaction, error: Exception, where: str) -> None:
    """Tell the player, and log a fault of the bot's with where it happened."""
    text = explain(error)
    if text is None:
        cause = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        log.error("%s failed in guild %s channel %s for user %s", where, interaction.guild_id,
                  interaction.channel_id, interaction.user.id if interaction.user else None,
                  exc_info=(type(cause), cause, cause.__traceback__))
        text = FAILED
    await reply(interaction, text)


async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """The command tree's error handler (``CommandTree.on_error``)."""
    name = interaction.command.qualified_name if interaction.command else "a command"
    await report(interaction, error, f"/{name}")


def guarded(callback):
    """For a button's ``callback`` or a form's ``on_submit``: a failure is answered and logged here."""
    @functools.wraps(callback)
    async def run(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            return await callback(self, interaction, *args, **kwargs)
        except Exception as error:            # noqa: BLE001 - every failure is answered and logged
            await report(interaction, error, f"{type(self).__name__}.{callback.__name__}")
    return run
