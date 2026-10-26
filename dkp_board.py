"""The live DKP board (/dkp-setup): every player with DKP this season, kept up to date in one channel."""

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
import heartbeat
import util
from dkp import board_embeds, touch

log = logging.getLogger("aion2.dkp")


class DkpBoard(commands.Cog):
    """/dkp-setup keeps a live list of every player's DKP, spread over as many messages as needed."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last: dict[int, list[str]] = {}

    async def cog_load(self):
        self.board_loop.change_interval(minutes=config.DKP_BOARD_MINUTES)
        self.board_loop.start()

    async def cog_unload(self):
        self.board_loop.cancel()

    def _forget(self, guild_id: int):
        db.conn().execute("DELETE FROM dkp_boards WHERE guild_id=?", (guild_id,))
        db.conn().commit()
        self._last.pop(guild_id, None)

    async def _channel(self, channel_id: int):
        return self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)

    async def _delete_all(self, channel, ids):
        for mid in ids:
            try:
                await channel.get_partial_message(mid).delete()
            except discord.HTTPException:
                pass

    async def refresh(self, guild_id: int) -> None:
        row = db.conn().execute("SELECT * FROM dkp_boards WHERE guild_id=?", (guild_id,)).fetchone()
        if not row:
            return
        try:
            channel = await self._channel(row["channel_id"])
            ids = json.loads(row["message_ids"])
            embeds = board_embeds(guild_id)
            payloads = [repr(e.to_dict()) for e in embeds]
            last = self._last.get(guild_id, [])
            new_ids: list[int] = []
            try:
                for i, embed in enumerate(embeds):
                    if i < len(ids):
                        if last[i : i + 1] != [payloads[i]]:
                            await channel.get_partial_message(ids[i]).edit(embed=embed)
                        new_ids.append(ids[i])
                    else:
                        msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                        new_ids.append(msg.id)
            except discord.NotFound:
                # a page was deleted by hand: post the whole board again so the pages stay in order
                await self._delete_all(channel, ids)
                new_ids = []
                for embed in embeds:
                    msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    new_ids.append(msg.id)
                ids = []
            await self._delete_all(channel, ids[len(embeds):])  # the list got shorter
            if new_ids != json.loads(row["message_ids"]):
                db.conn().execute("UPDATE dkp_boards SET message_ids=? WHERE guild_id=?", (json.dumps(new_ids), guild_id))
                db.conn().commit()
            self._last[guild_id] = payloads
        except (discord.NotFound, discord.Forbidden):
            log.warning("DKP board in server %s is gone or not allowed; stopped updating it.", guild_id)
            self._forget(guild_id)
        except discord.HTTPException:
            log.exception("DKP board update failed in server %s, will retry", guild_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self._deleted(payload.guild_id, {payload.message_id})

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        await self._deleted(payload.guild_id, set(payload.message_ids))

    async def _deleted(self, guild_id, gone: set):
        """Someone deleted a board page by hand: force a full check so it is posted again."""
        if not guild_id:
            return
        row = db.conn().execute("SELECT message_ids FROM dkp_boards WHERE guild_id=?", (guild_id,)).fetchone()
        if row and gone & set(json.loads(row["message_ids"])):
            self._last.pop(guild_id, None)
            touch(self.bot, guild_id)

    @tasks.loop(minutes=2)
    async def board_loop(self):
        for row in db.conn().execute("SELECT guild_id FROM dkp_boards").fetchall():
            try:
                await self.refresh(row["guild_id"])
            except Exception:
                log.exception("DKP board error in server %s", row["guild_id"])
        heartbeat.beat("dkp.board", self.board_loop)

    @board_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="dkp-setup", description="Post a live board of everyone's DKP in this channel (admins)")
    @app_commands.describe(stop="Remove the board and stop updating it")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def dkp_setup(self, interaction: discord.Interaction, stop: bool = False):
        if not util.is_admin(interaction):
            await interaction.response.send_message("Only server managers can do that.", ephemeral=True)
            return
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)
        old = db.conn().execute("SELECT * FROM dkp_boards WHERE guild_id=?", (gid,)).fetchone()
        if old:
            try:
                await self._delete_all(await self._channel(old["channel_id"]), json.loads(old["message_ids"]))
            except discord.HTTPException:
                pass
            self._forget(gid)
        if stop:
            await interaction.followup.send("DKP board removed.", ephemeral=True)
            return
        db.conn().execute("INSERT INTO dkp_boards (guild_id, channel_id, message_ids) VALUES (?,?,'[]')", (gid, interaction.channel_id))
        db.conn().commit()
        await self.refresh(gid)
        if not db.conn().execute("SELECT 1 FROM dkp_boards WHERE guild_id=?", (gid,)).fetchone():
            await interaction.followup.send(
                "I couldn't post here. Give me View Channel, Send Messages and Embed Links in this channel.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"Board posted. It lists every player with DKP, over several messages when the list is long, and updates "
            f"a few seconds after any change (and at least every {config.DKP_BOARD_MINUTES} minutes). "
            "Run `/dkp-setup` in another channel to move it, or `/dkp-setup stop:True` to remove it.",
            ephemeral=True,
        )
