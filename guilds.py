"""Per-server setup: a welcome message when the bot joins and the optional /setup command.

Nothing here is required. Every feature works as soon as the bot joins a server."""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
import gamedata
import timers
import util

log = logging.getLogger("aion2.guilds")

WELCOME = (
    "**Thanks for adding the AION 2 bot.** It already works, there is nothing to set up.\n\n"
    "**Everyone:** `/checklist` (your private daily and weekly list), `/event create` (plan a raid or boss run), "
    "`/timers show` (boss and event timers), `/link` (map, database, planners), `/timezone set` (so times match your clock).\n\n"
    "**Server managers (Manage Server):** `/checklist-setup` posts the checklist buttons, `/timer-setup` posts a live timer board, "
    "`/setup region` picks your game region so timers and resets follow your server's clock (NA East until you choose), "
    "and `/setup announcements` picks a channel for reset and new-timer notices (off until you choose one).\n\n"
    "Replies to most commands are private, so this won't spam your channels."
)


def welcome_channel(guild: discord.Guild) -> Optional[discord.abc.Messageable]:
    me = guild.me
    candidates = [guild.system_channel] if guild.system_channel else []
    candidates += list(guild.text_channels)
    for ch in candidates:
        if ch and ch.permissions_for(me).send_messages and ch.permissions_for(me).embed_links:
            return ch
    return None


async def send_welcome(guild: discord.Guild) -> None:
    channel = welcome_channel(guild)
    if channel is None:
        return
    try:
        await channel.send(embed=discord.Embed(title="AION 2 bot", description=WELCOME, color=0xA9741A))
    except discord.HTTPException:
        log.info("Could not post the welcome message in %s", guild.id)


class ServerSetup(commands.Cog):
    group = app_commands.Group(
        name="setup",
        description="Optional server settings (admins)",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @group.command(name="announcements", description="Pick the channel for reset and new-timer notices (leave empty to turn off)")
    @app_commands.describe(channel="Channel to post in. Leave empty to turn notices off")
    async def announcements(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        if channel is None:
            db.set_settings(interaction.guild_id, announce_channel=None)
            await interaction.response.send_message("Notices are off for this server.", ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages):
            await interaction.response.send_message(
                f"I can't post in {channel.mention}. Give me View Channel and Send Messages there, then try again.",
                ephemeral=True,
            )
            return
        db.set_settings(interaction.guild_id, announce_channel=channel.id)
        await interaction.response.send_message(
            f"Done. I'll post checklist resets and newly spotted timers in {channel.mention}.", ephemeral=True
        )

    @group.command(name="region", description="Pick the game region whose server clock the timers and resets follow")
    @app_commands.describe(region="The region your characters play on")
    @app_commands.choices(
        region=[app_commands.Choice(name=f"{full} ({short})", value=key) for key, (short, full, _tz) in gamedata.REGIONS.items()]
    )
    async def region(self, interaction: discord.Interaction, region: str):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        db.set_settings(interaction.guild_id, timer_region=region)
        await interaction.response.send_message(
            f"Done. Timers and checklist resets now follow the **{gamedata.REGIONS[region][1]}** server clock.", ephemeral=True
        )

    @group.command(name="show", description="Show this server's settings")
    async def show(self, interaction: discord.Interaction):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        gid = interaction.guild_id
        db.ensure_guild(gid)
        announce = db.get_setting(gid, "announce_channel")
        board_c, board_m = db.get_setting(gid, "board_channel"), db.get_setting(gid, "board_message")
        lines = [
            f"Game region: {gamedata.REGIONS[timers.region_key(gid)][1]} (`/setup region` to change)",
            f"Notices channel: {f'<#{announce}>' if announce else 'off (`/setup announcements` to choose one)'}",
            f"Timer board: {f'https://discord.com/channels/{gid}/{board_c}/{board_m}' if board_c and board_m else 'none (`/timer-setup` posts one)'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerSetup())
