"""AION 2 Discord bot: checklists, event planner, timers, links."""

import asyncio
import hashlib
import json
import logging
import os
import time

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

# The slash commands are registered with Discord again only when they changed, or a week after the last time.
# Registering them on every start, before the health endpoint came up, let a rate limit on that call keep the
# bot offline and its health check refused for as long as Discord made it wait.
COMMANDS_RESYNC_SECONDS = 7 * 24 * 3600


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
        self._sync_task = None
        # What the bot is doing, and where the slash commands stand, for the health endpoint.
        self.phase = "starting"
        self.commands_status = "not registered yet"

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        # The health endpoint comes up before anything that can wait or fail (the login, the database, the
        # command registration), so Docker, monitoring and the updater always get an answer: 503 with what
        # the bot is doing until it is connected to Discord, then 200. It used to start last, so a start that
        # stalled or failed showed only as "Connection refused".
        if self._health_runner is None:
            self._health_runner = await health.start(self)
        self.phase = "logging in"
        await super().start(token, reconnect=reconnect)

    async def setup_hook(self):
        self.phase = "loading"
        db.init()
        for module in (checklist, event_commands, timer_commands, bosscatalog, gameservers, fieldbosses, links, guilds,
                       dkp_commands, news):
            await module.setup(self)
        # Descriptions and choices in Korean and Traditional Chinese for people whose Discord uses those languages.
        await self.tree.set_translator(translations.Translator())
        # Registering the commands waits out Discord's rate limits, so it runs beside the connection, not before it.
        self._sync_task = asyncio.create_task(self.sync_commands())
        self.phase = "connecting to Discord"

    async def commands_digest(self) -> str:
        """A fingerprint of what registering the commands would send Discord, translations included."""
        translator = self.tree.translator
        payload = [await command.get_translated_payload(self.tree, translator) if translator else command.to_dict(self.tree)
                   for command in self.tree.get_commands()]
        text = json.dumps({"application": self.application_id, "guild": config.GUILD_ID, "commands": payload},
                          sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def sync_commands(self) -> None:
        """Register the slash commands with Discord when they changed since the last registration (or it is a week
        old). Commands are global, so any server that adds the bot gets them (new ones can take up to an hour to
        show). A failure is logged and leaves the commands Discord already has; the next start tries again."""
        try:
            digest = await self.commands_digest()
            synced_at = int(db.meta_get("commands_synced_at") or 0)
            if db.meta_get("commands_synced") == digest and time.time() - synced_at < COMMANDS_RESYNC_SECONDS:
                self.commands_status = "unchanged"
                log.info("Slash commands unchanged since they were last registered; not registering them again")
                return
            self.commands_status = "registering"
            await self.tree.sync()
            if config.GUILD_ID:
                # Older versions registered commands on one server only. Remove those copies so nothing shows twice.
                guild = discord.Object(int(config.GUILD_ID))
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            db.meta_set("commands_synced", digest)
            db.meta_set("commands_synced_at", str(int(time.time())))
            self.commands_status = "registered"
            log.info("Slash commands registered with Discord")
        except Exception as err:
            self.commands_status = f"registration failed ({type(err).__name__})"
            log.exception("Registering the slash commands with Discord failed; Discord keeps the ones it had")

    async def on_ready(self):
        for guild in self.guilds:
            db.ensure_guild(guild.id)
        self.phase = "online"
        log.info("Logged in as %s in %d server(s)", self.user, len(self.guilds))

    async def on_disconnect(self):
        self.phase = "reconnecting to Discord"

    async def on_resumed(self):
        self.phase = "online"

    async def on_guild_join(self, guild: discord.Guild):
        if db.ensure_guild(guild.id):
            log.info("Joined server %s (%s)", guild.name, guild.id)
            await guilds.send_welcome(guild)

    async def on_guild_remove(self, guild: discord.Guild):
        log.info("Removed from server %s (%s)", guild.name, guild.id)
        db.forget_guild(guild.id)

    async def close(self):
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        if self._health_runner:
            await self._health_runner.cleanup()
            self._health_runner = None
        await super().close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Put it in the .env file.")
    # The image starts as root only to give the mounted data folder to its own user. Root is dropped here,
    # before anything is read from Discord or the web.
    privileges.drop(config.BOT_USER, os.path.dirname(os.path.abspath(config.DB_PATH)))
    AionBot().run(config.TOKEN, log_handler=None)
