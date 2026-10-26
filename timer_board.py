"""The timer list as an embed (/timers show and the live board) and the live board itself (/timer-setup)."""

import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
import gamedata
import heartbeat
import util
from timers import (ABYSS_CATEGORY, FIELD_CATEGORY, FIELD_FACTIONS, FIELD_RESPAWNS, WINDOW_DAYS, events_between,
                    region_key)

log = logging.getLogger("aion2.timers")


def _add_chunked(embed: discord.Embed, name: str, lines: list[str]) -> None:
    chunk, size, first = [], 0, True
    for line in lines:
        if chunk and size + len(line) + 1 > 1000:
            embed.add_field(name=name if first else f"{name} (cont.)", value="\n".join(chunk), inline=False)
            first, chunk, size = False, [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        embed.add_field(name=name if first else f"{name} (cont.)", value="\n".join(chunk), inline=False)


def fmt_minutes(mins: int) -> str:
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "") if p) or "0m"


def build_embed(guild_id: int, category: Optional[str] = None, max_chars: Optional[int] = None) -> Optional[discord.Embed]:
    """The timer list for one server as an embed, or None when there is nothing to show.

    max_chars trims the soonest-last lines so the embed fits Discord's 6000 character cap."""
    now = int(time.time())
    c = db.conn()
    upcoming: dict[str, int] = {}
    for name, _cat, start in events_between(guild_id, now, now + WINDOW_DAYS * 86400):
        key = name.lower()
        if key not in upcoming or start < upcoming[key]:
            upcoming[key] = start
    manual = {
        m["name"].lower(): m
        for m in c.execute("SELECT * FROM guild_timer_manual WHERE guild_id=?", (guild_id,)).fetchall()
    }
    rows = []
    for r in c.execute("SELECT name, category, timing FROM timer_rules").fetchall():
        rows.append({"name": r["name"], "category": r["category"], "timing": r["timing"]})
    known = {r["name"].lower() for r in rows}
    for key, m in manual.items():
        if key not in known:
            rows.append({"name": m["name"], "category": m["category"], "timing": None})
    # Field bosses aren't on the timers site. List the ones for this server's faction (and the
    # contested Abyss bosses, shown to everyone) so they appear even before a kill is recorded.
    # A boss that already has a real row (scraped or /timers killed) keeps it instead of this
    # reference line. Faction None = show both factions.
    have = {r["name"].lower() for r in rows}
    faction = db.get_setting(guild_id, "faction")
    for disp, mins in FIELD_RESPAWNS.items():
        if disp.lower() in have:
            continue
        bf = FIELD_FACTIONS.get(disp, "both")
        if faction and bf != "both" and bf != faction:
            continue
        rows.append({"name": disp, "category": FIELD_CATEGORY, "reference": True,
                     "timing": f"respawn {fmt_minutes(mins)} after kill — not seen yet"})
    for name, _zone, sched in gamedata.ABYSS_BOSSES:
        if name.lower() not in have:
            rows.append({"name": name, "category": ABYSS_CATEGORY, "reference": True, "timing": sched})
    for r in rows:
        m = manual.get(r["name"].lower())
        r["nxt"] = upcoming.get(r["name"].lower())
        r["set_by"], r["iv"], r["mnext"] = (m["set_by"], m["interval_min"], m["next_at"]) if m else (None, None, None)
    rows = [r for r in rows if not (r["timing"] is None and r["nxt"] is None)]  # hide finished manual timers
    if category:
        rows = [r for r in rows if r["category"].lower() == category.lower()]
    if not rows:
        return None
    last_ok = db.meta_get("timers_last_ok")
    embed = discord.Embed(title="AION 2 event timers", color=0xA9741A)
    embed.description = (
        f"Times show in your own timezone. {gamedata.REGIONS[region_key(guild_id)][0]} schedule from gamers4.life"
        + (f" · updated {util.ts(int(last_ok), 'R')}" if last_ok else "")
        + "\nWrong or missing? Fix it with `/timers set` or `/timers killed`. Wrong region? `/setup region` (admins)."
    )
    budget = (max_chars or 5900) - len(embed.title) - len(embed.description) - 120
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    hidden = 0
    for cat in sorted(by_cat):
        items = sorted(by_cat[cat], key=lambda r: (r["nxt"] is None, r["nxt"] or 0))
        lines = []
        for r in items:
            timing = r["timing"] or (f"every {fmt_minutes(r['iv'])} (manual)" if r["iv"] else "set manually")
            edited = ""
            if r["set_by"] and r["mnext"] and (r["iv"] or r["mnext"] >= now):
                edited = f" · set by <@{r['set_by']}>"
            elif r["set_by"] == 0 and r["mnext"] and r["mnext"] >= now:
                edited = " · from combat logs"
            if r["nxt"]:
                line = f"**{r['name']}**: {util.ts(r['nxt'], 'R')} ({util.ts(r['nxt'], 't')}) · {timing}{edited}"
            elif r.get("reference"):
                line = f"**{r['name']}**: {timing}"          # a built-in field boss with no recorded kill yet
            else:
                line = f"**{r['name']}**: no upcoming time found · {timing}"
            line = util.cut(line, 300)
            if budget - len(line) - 1 < 0:
                hidden += 1
                continue
            budget -= len(line) + 1
            lines.append(line)
        if lines:
            budget -= len(cat) + 10
            _add_chunked(embed, cat, lines)
    if hidden:
        embed.set_footer(text=f"{hidden} more not shown. Use /timers show to see everything.")
    return embed


# ---------------------------------------------------------------- live board (one per server)
class TimerBoard(commands.Cog):
    """/timer-setup posts one timer board per server that the bot keeps editing."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_payload: dict[int, str] = {}

    async def cog_load(self):
        self.board_loop.change_interval(minutes=config.TIMER_BOARD_MINUTES)
        self.board_loop.start()

    async def cog_unload(self):
        self.board_loop.cancel()

    def _board_embed(self, guild_id: int) -> discord.Embed:
        embed = build_embed(guild_id, max_chars=5900)
        if embed is None:
            embed = discord.Embed(
                title="AION 2 event timers",
                description="No timer data yet. It appears after the first fetch, usually within a minute.",
                color=0xA9741A,
            )
        embed.set_footer(
            text=(embed.footer.text + " · " if embed.footer and embed.footer.text else "")
            + f"Board refreshes every {config.TIMER_BOARD_MINUTES} min"
        )
        return embed

    async def _message(self, guild_id: int):
        cid, mid = db.get_setting(guild_id, "board_channel"), db.get_setting(guild_id, "board_message")
        if not cid or not mid:
            return None
        channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
        return channel.get_partial_message(mid)

    def _forget(self, guild_id: int):
        db.set_settings(guild_id, board_channel=None, board_message=None)
        self._last_payload.pop(guild_id, None)

    @tasks.loop(minutes=2)
    async def board_loop(self):
        rows = db.conn().execute(
            "SELECT guild_id FROM guild_settings WHERE board_channel IS NOT NULL AND board_message IS NOT NULL"
        ).fetchall()
        for row in rows:
            gid = row["guild_id"]
            try:
                msg = await self._message(gid)
                if msg is None:
                    continue
                embed = self._board_embed(gid)
                payload = repr(embed.to_dict())
                if payload == self._last_payload.get(gid):
                    continue  # nothing changed, skip the edit
                await msg.edit(embed=embed)
                self._last_payload[gid] = payload
            except (discord.NotFound, discord.Forbidden):
                log.warning("Timer board in server %s is gone or not allowed; stopped updating it.", gid)
                self._forget(gid)
            except discord.HTTPException:
                log.exception("Timer board edit failed in server %s, will retry", gid)
            except Exception:
                log.exception("Timer board error in server %s", gid)
        heartbeat.beat("timers.board", self.board_loop)

    @board_loop.before_loop
    async def _before_board(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="timer-setup", description="Post a live timer board in this channel (admins)")
    @app_commands.describe(stop="Remove the board and stop updating it",
                           region="Optional: set this server's game region first, so the board's times are right",
                           server="Optional: set this server's game server first, for field-boss kill timers")
    @app_commands.choices(
        region=[app_commands.Choice(name=f"{full} ({short})", value=key) for key, (short, full, _tz) in gamedata.REGIONS.items()])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def timer_setup(self, interaction: discord.Interaction, stop: bool = False,
                          region: Optional[str] = None, server: Optional[str] = None):
        if not util.is_admin(interaction):
            await interaction.response.send_message("Only server managers can do that.", ephemeral=True)
            return
        import gameservers
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)
        try:
            old = await self._message(gid)
        except discord.HTTPException:
            old = None
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        if stop:
            self._forget(gid)
            await interaction.followup.send("Timer board removed.", ephemeral=True)
            return
        # Optionally set this guild's region/server before posting, so a board can be put up
        # correctly in one step instead of running /setup region and /setup server separately.
        # Same validation as those commands: region is a known choice; server must match the
        # region's official list. If a given value is wrong, stop before posting rather than
        # pinning a board to the wrong clock.
        notes = []
        if region is not None:
            db.set_settings(gid, timer_region=region)
            if db.get_setting(gid, "game_server") and not gameservers.guild_server(gid):
                db.set_settings(gid, game_server=None)   # old server was in another region
            notes.append(f"region **{gamedata.REGIONS[region][1]}**")
        if server:
            rows = gameservers.servers(region_key(gid))
            typed = server.strip().lower()
            match = ([r for r in rows if r["server_id"] == typed]
                     or [r for r in rows if gameservers.label(r).lower() == typed]
                     or [r for r in rows if r["name"].lower() == typed])
            if len(match) != 1:
                await interaction.followup.send(
                    f"I couldn't match that game server to the {gamedata.REGIONS[region_key(gid)][1]} list. "
                    "Pick one from the suggestions (or set the region first, then the server).",
                    ephemeral=True,
                )
                return
            db.set_settings(gid, game_server=match[0]["server_id"])
            notes.append(f"server **{gameservers.label(match[0])}**")
        try:
            message = await interaction.channel.send(embed=self._board_embed(gid))
        except discord.HTTPException:
            await interaction.followup.send(
                "I couldn't post here. Give me View Channel, Send Messages and Embed Links in this channel.",
                ephemeral=True,
            )
            return
        db.set_settings(gid, board_channel=message.channel.id, board_message=message.id)
        self._last_payload.pop(gid, None)
        prefix = ("Set " + " and ".join(notes) + ". ") if notes else ""
        await interaction.followup.send(
            f"{prefix}Board posted for the **{gamedata.REGIONS[region_key(gid)][1]}** schedule. "
            f"It updates every {config.TIMER_BOARD_MINUTES} minutes (the countdowns themselves tick live). "
            "Run `/timer-setup` in another channel to move it, or `/timer-setup stop:True` to remove it.",
            ephemeral=True,
        )

    @timer_setup.autocomplete("server")
    async def _timer_setup_server_ac(self, interaction: discord.Interaction, current: str):
        """Suggest servers for the region being chosen in this same command (or the guild's
        current region if none is typed yet), like /setup server does."""
        import gameservers
        region = getattr(interaction.namespace, "region", None) or region_key(interaction.guild_id)
        rows = gameservers.servers(region)
        cur = (current or "").strip().lower()
        return [app_commands.Choice(name=gameservers.label(r), value=r["server_id"])
                for r in rows if cur in gameservers.label(r).lower()][:25]
