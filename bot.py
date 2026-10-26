"""AION 2 Discord bot: checklists, event planner, timers, links."""

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import bosscatalog
import checklist
import config
import db
import dkp_commands
import errors
import event_commands
import fieldbosses
import gameservers
import guilds
import health
import links
import news
import privileges
import timer_commands
import translations

log = logging.getLogger("aion2")


class GuildOnlyTree(app_commands.CommandTree):
    """Everything here is per server, so commands only run inside a server."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return False
        db.ensure_guild(interaction.guild_id)
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await errors.on_command_error(interaction, error)


class AionBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            activity=discord.Game(name="AION 2"),
            tree_cls=GuildOnlyTree,
        )
        self._health_runner = None

    async def setup_hook(self):
        db.init()
        for module in (checklist, event_commands, timer_commands, bosscatalog, gameservers, fieldbosses, links, guilds,
                       dkp_commands, news):
            await module.setup(self)
        # Descriptions and choices in Korean and Traditional Chinese for people whose Discord uses those languages.
        await self.tree.set_translator(translations.Translator())
        # Commands are global, so any server that adds the bot gets them (new ones can take up to an hour to show).
        await self.tree.sync()
        if config.GUILD_ID:
            # Older versions registered commands on one server only. Remove those copies so nothing shows twice.
            guild = discord.Object(int(config.GUILD_ID))
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
        self._health_runner = await health.start(self)

    async def on_ready(self):
        for guild in self.guilds:
            db.ensure_guild(guild.id)
        log.info("Logged in as %s in %d server(s)", self.user, len(self.guilds))

    async def on_guild_join(self, guild: discord.Guild):
        if db.ensure_guild(guild.id):
            log.info("Joined server %s (%s)", guild.name, guild.id)
            await guilds.send_welcome(guild)

    async def on_guild_remove(self, guild: discord.Guild):
        log.info("Removed from server %s (%s)", guild.name, guild.id)
        db.forget_guild(guild.id)

    async def close(self):
        if self._health_runner:
            await self._health_runner.cleanup()
        await super().close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Put it in the .env file.")
    # The image starts as root only to give the mounted data folder to its own user. Root is dropped here,
    # before anything is read from Discord or the web.
    privileges.drop(config.BOT_USER, os.path.dirname(os.path.abspath(config.DB_PATH)))
    AionBot().run(config.TOKEN, log_handler=None)
