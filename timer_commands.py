"""/timers: reading the schedule site, the commands, manual corrections and the DM alerts."""

import logging
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import bosscatalog
import config
import db
import gamedata
import heartbeat
import util
from timer_board import TimerBoard, build_embed, fmt_minutes
from timers import (CATEGORY_CHOICES, FIELD_RESPAWNS, MAX_SHIFT_SEC, all_categories, all_names, canonical,
                    clear_manual, events_between, field_respawn, guild_tz, nearest_site_time, page_accepted,
                    parse_page_meta, parse_table, purge_old, region_key, save_manual, schedule_changes_key,
                    store_rules)

log = logging.getLogger("aion2.timers")


# ---------------------------------------------------------------- cog
class Timers(commands.GroupCog, group_name="timers", group_description="AION 2 event timers"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        super().__init__()

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self.refresh_loop.change_interval(minutes=config.TIMER_REFRESH_MINUTES)
        self.refresh_loop.start()
        self.notify_loop.start()

    async def cog_unload(self):
        self.refresh_loop.cancel()
        self.notify_loop.cancel()
        if self.session:
            await self.session.close()

    # ---- scraping
    async def fetch_and_store(self) -> str:
        db.meta_set("timers_last_attempt", str(int(time.time())))
        headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html"}
        etag = db.meta_get("timers_etag")
        # A schedule stored before the bot's official schedule changes (gamedata.SCHEDULE_CHANGES) were last
        # changed is read in full again, so they apply even while the page itself hasn't changed.
        if etag and db.meta_get("timers_schedule_changes") == schedule_changes_key():
            headers["If-None-Match"] = etag
        try:
            async with self.session.get(
                config.TIMERS_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status == 304:
                    status, body = "unchanged", None
                elif resp.status == 200:
                    status, body = "ok", await resp.text()
                    if resp.headers.get("ETag"):
                        db.meta_set("timers_etag", resp.headers["ETag"])
                else:
                    raise RuntimeError(f"HTTP {resp.status}")
        except Exception as e:
            db.meta_set("timers_last_error", f"{type(e).__name__}: {e}")
            log.warning("Timer fetch failed: %s", e)
            return "error"

        new: list = []
        if body is not None:
            rows = parse_table(body)
            region, _offset = parse_page_meta(body)
            if not page_accepted(region):
                db.meta_delete("timers_etag")
                db.meta_set(
                    "timers_last_error",
                    f"The page is showing the {region} schedule, not {config.TIMERS_REGION}. Kept the last good schedule.",
                )
                log.warning("Timers page shows region %r, wanted %r; ignoring it", region, config.TIMERS_REGION)
                return "error"
            region_label = config.TIMERS_REGION
            region_changed = db.meta_get("timers_region") != region_label
            kept = 0
            if rows:
                kept, new = store_rules(rows)
                db.meta_set("timers_region", region_label)
                if region_changed:
                    new = []  # first read of this region: don't announce its whole schedule as new
            if not rows or not kept:
                db.meta_set(
                    "timers_last_error",
                    "The events page loaded but no schedule rows were found. The site layout may have changed.",
                )
                log.warning("Timer page parsed but no usable rows (%d raw rows)", len(rows))
                return "error"
        purge_old()
        db.meta_set("timers_last_ok", str(int(time.time())))
        db.meta_set("timers_last_error", "")
        n_rules = db.conn().execute("SELECT COUNT(*) AS n FROM timer_rules").fetchone()["n"]
        log.info("Timers %s: %d scheduled timers tracked", status, n_rules)
        if new:
            await self._announce_new(new)
        return status

    async def _announce_new(self, new: list) -> None:
        lines = [f"**{n}** ({c}) · {t}" for n, c, t in new]
        log.info("New timers spotted: %s", "; ".join(f"{n} ({c})" for n, c, _ in new))
        db.meta_set("timers_last_new", f"{int(time.time())}|" + ", ".join(n for n, _, _ in new))
        text = util.cut("New on the AION 2 event schedule, now tracked:\n" + "\n".join(lines), 1900)
        for gid, cid in db.announce_channels():
            try:
                channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                await channel.send(text)
            except discord.HTTPException:
                log.info("Could not announce new timers in server %s", gid)

    @tasks.loop(minutes=15)
    async def refresh_loop(self):
        try:
            await self.fetch_and_store()
            heartbeat.beat("timers.refresh", self.refresh_loop)
        except Exception:
            log.exception("Timer refresh error")

    @refresh_loop.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    # ---- notifications
    @tasks.loop(seconds=20)
    async def notify_loop(self):
        try:
            now = int(time.time())
            c = db.conn()
            by_guild: dict[int, list] = {}
            for s in c.execute("SELECT * FROM guild_timer_subs").fetchall():
                by_guild.setdefault(s["guild_id"], []).append(s)
            for gid, subs in by_guild.items():
                window = events_between(gid, now - 60, now + 11 * 60)
                for s in subs:
                    for name, cat, start in window:
                        hit = (
                            name.lower() == s["target"].lower()
                            if s["kind"] == "name"
                            else cat.lower() == s["target"].lower()
                        )
                        if not hit or not (start - s["lead"] * 60 <= now < start + 60):
                            continue
                        done = c.execute(
                            "SELECT 1 FROM timer_notified WHERE user_id=? AND name=? AND starts_at=?",
                            (s["user_id"], name, start),
                        ).fetchone()
                        if done:
                            continue
                        c.execute(
                            "INSERT OR IGNORE INTO timer_notified (user_id, name, starts_at) VALUES (?,?,?)",
                            (s["user_id"], name, start),
                        )
                        c.commit()
                        text = f"**{name}** ({cat}) starts {util.ts(start, 'R')} ({util.ts(start, 't')})."
                        try:
                            user = self.bot.get_user(s["user_id"]) or await self.bot.fetch_user(s["user_id"])
                            await user.send(text)
                        except discord.HTTPException:
                            log.info("Could not DM user %s (DMs closed?)", s["user_id"])
            heartbeat.beat("timers.notify", self.notify_loop)
        except Exception:
            log.exception("Timer notify error")

    @notify_loop.before_loop
    async def _before_notify(self):
        await self.bot.wait_until_ready()

    # ---- commands: viewing
    @app_commands.command(name="show", description="When the next rifts, bosses and events start (your time)")
    @app_commands.describe(category="Only show one category")
    async def show(self, interaction: discord.Interaction, category: Optional[str] = None):
        embed = build_embed(interaction.guild_id, category)
        if embed is None:
            msg = "No timer data yet. The first fetch happens shortly after the bot starts, so try again in a minute."
            if category:
                msg = f"No timers found for **{category}**."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @show.autocomplete("category")
    async def _show_ac(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=c, value=c) for c in all_categories(interaction.guild_id) if current.lower() in c.lower()][:25]

    # ---- commands: manual timers
    @staticmethod
    def _name_choices(guild_id: int, current: str):
        db.ensure_guild(guild_id)
        names = all_names(guild_id) + sorted(FIELD_RESPAWNS) + bosscatalog.names()   # this server's timers first, then every known boss
        names += db.bosses(guild_id, "world_boss", current)
        seen, out = set(), []
        for n in names:
            if current.lower() in n.lower() and n.lower() not in seen:
                seen.add(n.lower())
                out.append(app_commands.Choice(name=n[:100], value=n[:100]))
        return out[:25]

    @app_commands.command(name="set", description="Set or fix when a boss or event starts (shared with everyone)")
    @app_commands.describe(
        name="Boss or event name",
        when="Next start: 8:45pm, tomorrow 6am, in 2h",
        repeat="Optional: it repeats every this long, like 4h or 90m",
        category="Category if this is a new timer",
        only_this_time="Only change this one start and go back to the site's times after it",
    )
    @app_commands.choices(category=CATEGORY_CHOICES)
    async def set_timer(
        self,
        interaction: discord.Interaction,
        name: str,
        when: str,
        repeat: Optional[str] = None,
        category: Optional[str] = None,
        only_this_time: bool = False,
    ):
        name = " ".join(name.split())[:60]
        if not name:
            await interaction.response.send_message("Give the timer a name.", ephemeral=True)
            return
        try:
            starts = util.parse_when(when, util.get_tz(interaction.user.id))
            iv = None
            if repeat:
                iv = int(util.parse_duration(repeat).total_seconds() // 60)
                if not 5 <= iv <= 43200:
                    raise ValueError("Repeat must be between 5 minutes and 30 days.")
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        canon, cat, timing = canonical(interaction.guild_id, name)
        ts = int(starts.timestamp())
        site = nearest_site_time(timing, ts, guild_tz(interaction.guild_id))
        shift = None
        if site is not None and not iv and not only_this_time and abs(ts - site) <= MAX_SHIFT_SEC:
            shift = ts - site                      # keep the site's pattern, just moved by this much
        replaces = None if iv else site
        save_manual(interaction.guild_id, canon, cat or category or "Field Bosses", ts, iv, replaces, None, interaction.user.id, shift)
        if shift is not None:
            how = "exactly on the site's time" if shift == 0 else f"{fmt_minutes(abs(shift) // 60) if abs(shift) >= 60 else 'seconds'} {'later' if shift > 0 else 'earlier'} than the site"
            msg = (
                f"Saved. **{canon}** now starts {util.ts(starts, 'F')} ({util.ts(starts, 'R')}), {how}. "
                "Every other start moves by the same amount and stays that way through restarts and site refreshes. "
                f"Use `/timers clear` to go back to the site's times, or add `only_this_time:True` to change just one start."
            )
        else:
            more = f" and every {fmt_minutes(iv)} after that" if iv else ""
            note = ""
            if site is not None and not iv and not only_this_time:
                note = " That's more than 12 hours from the site's time, so I changed only this one start."
            msg = (
                f"Saved. **{canon}** is set for {util.ts(starts, 'F')} ({util.ts(starts, 'R')}){more}.{note} "
                "Everyone sees it in `/timers show`, and people who used `/timers notify` get their DM."
            )
        await interaction.response.send_message(msg, ephemeral=True)

    @set_timer.autocomplete("name")
    async def _set_ac(self, interaction: discord.Interaction, current: str):
        return self._name_choices(interaction.guild_id, current)

    @app_commands.command(name="killed", description="A boss just died: start its respawn timer from now")
    @app_commands.describe(name="Boss name", respawn="Respawn time, like 4h or 90m. Remembered; known field bosses don't need it")
    async def killed(self, interaction: discord.Interaction, name: str, respawn: Optional[str] = None):
        name = " ".join(name.split())[:60]
        canon, cat, timing = canonical(interaction.guild_id, name)
        row = db.conn().execute(
            "SELECT respawn_min FROM guild_timer_manual WHERE guild_id=? AND name = ? COLLATE NOCASE",
            (interaction.guild_id, canon),
        ).fetchone()
        try:
            if respawn:
                mins = int(util.parse_duration(respawn).total_seconds() // 60)
            elif row and row["respawn_min"]:
                mins = row["respawn_min"]
            elif field_respawn(canon):
                mins = field_respawn(canon)
            else:
                zones = (bosscatalog.lookup(canon) or {"zones": ""})["zones"] or ""
                hint = " It respawns at a different speed in each zone, or pick the one with your zone in its name." if "," in zones and any(k.startswith(canon + " (") for k in FIELD_RESPAWNS) else ""
                raise ValueError(f"How long does it take to respawn? Add it like `respawn:4h`. I'll remember it for next time.{hint}")
            if not 1 <= mins <= 43200:
                raise ValueError("Respawn must be between 1 minute and 30 days.")
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        starts = int(time.time()) + mins * 60
        save_manual(interaction.guild_id, canon, cat or "Field Bosses", starts, None, nearest_site_time(timing, starts, guild_tz(interaction.guild_id)), mins, interaction.user.id)
        where = bosscatalog.describe(canon)
        await interaction.response.send_message(
            f"**{canon}** respawns {util.ts(starts, 'R')} ({util.ts(starts, 't')}), {fmt_minutes(mins)} from now."
            + (f"\n{where}" if where else ""),
            ephemeral=True,
        )

    @killed.autocomplete("name")
    async def _killed_ac(self, interaction: discord.Interaction, current: str):
        return self._name_choices(interaction.guild_id, current)

    @app_commands.command(name="clear", description="Remove a manual time and go back to the site's schedule")
    @app_commands.describe(name="The timer to reset")
    async def clear(self, interaction: discord.Interaction, name: str):
        row = db.conn().execute(
            "SELECT * FROM guild_timer_manual WHERE guild_id=? AND name = ? COLLATE NOCASE",
            (interaction.guild_id, name.strip()),
        ).fetchone()
        if not row:
            await interaction.response.send_message("There's no manual time saved for that name.", ephemeral=True)
            return
        if row["set_by"] and row["set_by"] != interaction.user.id and not util.is_admin(interaction):
            await interaction.response.send_message(
                f"Only <@{row['set_by']}> or a server manager can clear that. You can overwrite it with `/timers set`.",
                ephemeral=True,
            )
            return
        clear_manual(interaction.guild_id, row["name"])
        await interaction.response.send_message(f"Cleared **{row['name']}**.", ephemeral=True)

    @clear.autocomplete("name")
    async def _clear_ac(self, interaction: discord.Interaction, current: str):
        rows = db.conn().execute(
            "SELECT name FROM guild_timer_manual WHERE guild_id=? AND name LIKE ? ORDER BY name LIMIT 25",
            (interaction.guild_id, f"%{current}%"),
        ).fetchall()
        return [app_commands.Choice(name=r["name"][:100], value=r["name"][:100]) for r in rows]

    # ---- commands: notifications
    @app_commands.command(name="notify", description="DM me when a timer starts")
    @app_commands.describe(target="An event or a whole category", lead="How early to notify you")
    @app_commands.choices(
        lead=[
            app_commands.Choice(name="When it starts", value=0),
            app_commands.Choice(name="1 minute before", value=1),
            app_commands.Choice(name="3 minutes before", value=3),
            app_commands.Choice(name="5 minutes before", value=5),
            app_commands.Choice(name="10 minutes before", value=10),
        ]
    )
    async def notify(self, interaction: discord.Interaction, target: str, lead: int = 5):
        cats = {c.lower(): c for c in all_categories(interaction.guild_id)}
        names = {n.lower(): n for n in all_names(interaction.guild_id)}
        key = target.strip().lower()
        if key in cats:
            kind, value = "category", cats[key]
        elif key in names:
            kind, value = "name", names[key]
        else:
            await interaction.response.send_message(
                "I don't know that timer. Start typing and pick one from the list.", ephemeral=True
            )
            return
        db.conn().execute(
            """INSERT INTO guild_timer_subs (guild_id, user_id, kind, target, lead) VALUES (?,?,?,?,?)
               ON CONFLICT(guild_id, user_id, kind, target) DO UPDATE SET lead=excluded.lead""",
            (interaction.guild_id, interaction.user.id, kind, value, lead),
        )
        db.conn().commit()
        when = "when it starts" if lead == 0 else f"{lead} minute{'s' if lead != 1 else ''} before"
        extra = " Some categories start every hour, so this can send a lot of DMs." if kind == "category" else ""
        await interaction.response.send_message(
            f"I'll DM you {when} for **{value}**.{extra} Make sure server members are allowed to DM you.",
            ephemeral=True,
        )

    @notify.autocomplete("target")
    async def _target_ac(self, interaction: discord.Interaction, current: str):
        out = [
            app_commands.Choice(name=f"All {c}", value=c)
            for c in all_categories(interaction.guild_id)
            if current.lower() in c.lower() or current.lower() in f"all {c}".lower()
        ]
        out += [
            app_commands.Choice(name=n[:100], value=n[:100]) for n in all_names(interaction.guild_id) if current.lower() in n.lower()
        ]
        return out[:25]

    @app_commands.command(name="unnotify", description="Stop DMs for a timer")
    @app_commands.describe(target="The event or category to stop")
    async def unnotify(self, interaction: discord.Interaction, target: str):
        cur = db.conn().execute(
            "DELETE FROM guild_timer_subs WHERE guild_id=? AND user_id=? AND lower(target)=lower(?)",
            (interaction.guild_id, interaction.user.id, target.strip()),
        )
        db.conn().commit()
        await interaction.response.send_message(
            "Stopped." if cur.rowcount else "You weren't subscribed to that.", ephemeral=True
        )

    @unnotify.autocomplete("target")
    async def _unnotify_ac(self, interaction: discord.Interaction, current: str):
        rows = db.conn().execute(
            "SELECT target FROM guild_timer_subs WHERE guild_id=? AND user_id=? AND target LIKE ? LIMIT 25",
            (interaction.guild_id, interaction.user.id, f"%{current}%"),
        ).fetchall()
        return [app_commands.Choice(name=r["target"][:100], value=r["target"][:100]) for r in rows]

    @app_commands.command(name="mine", description="List the timer DMs you signed up for")
    async def mine(self, interaction: discord.Interaction):
        rows = db.conn().execute(
            "SELECT kind, target, lead FROM guild_timer_subs WHERE guild_id=? AND user_id=? ORDER BY target",
            (interaction.guild_id, interaction.user.id),
        ).fetchall()
        if not rows:
            await interaction.response.send_message("No timer DMs set up. Use `/timers notify`.", ephemeral=True)
            return
        lines = [
            f"**{r['target']}**{' (all)' if r['kind'] == 'category' else ''}: "
            + ("when it starts" if r["lead"] == 0 else f"{r['lead']} min before")
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="status", description="Show when the timers were last refreshed")
    async def status(self, interaction: discord.Interaction):
        ok = db.meta_get("timers_last_ok")
        attempt = db.meta_get("timers_last_attempt")
        err = db.meta_get("timers_last_error")
        new = db.meta_get("timers_last_new")
        c = db.conn()
        n_rules = c.execute("SELECT COUNT(*) AS n FROM timer_rules").fetchone()["n"]
        n_catalog = c.execute("SELECT COUNT(*) AS n FROM timer_catalog").fetchone()["n"]
        n_manual = c.execute(
            "SELECT COUNT(*) AS n FROM guild_timer_manual WHERE guild_id=?", (interaction.guild_id,)
        ).fetchone()["n"]
        lines = [
            f"Last successful refresh: {util.ts(int(ok), 'R') if ok else 'never'}",
            f"Last attempt: {util.ts(int(attempt), 'R') if attempt else 'never'}",
            f"Tracking {n_rules} scheduled timers ({n_catalog} ever seen on the site) and {n_manual} manual timers.",
            f"Refreshes every {config.TIMER_REFRESH_MINUTES} minutes from gamers4.life. Anything new on the site is tracked automatically.",
            f"This server follows the {gamedata.REGIONS[region_key(interaction.guild_id)][1]} server clock"
            " (`/setup region` changes it, admins).",
        ]
        n_bosses = c.execute("SELECT COUNT(*) AS n FROM boss_catalog").fetchone()["n"]
        b_ok, b_err = db.meta_get("bosses_last_ok"), db.meta_get("bosses_last_error")
        if n_bosses:
            lines.append(
                f"Boss list: {n_bosses} open-world bosses from [aion2data.com](<https://aion2data.com/bosses>)"
                + (f" (client {db.meta_get('bosses_version')})" if db.meta_get("bosses_version") else "")
                + (f", checked {util.ts(int(b_ok), 'R')}" if b_ok else "")
                + ". Respawn times aren't in the game files, so they come from kill reports."
            )
        if b_err:
            lines.append(f"Boss list problem: {b_err}")
        if new and "|" in new:
            stamp, names = new.split("|", 1)
            lines.append(f"Last new timer spotted {util.ts(int(stamp), 'R')}: {util.cut(names, 300)}")
        if err:
            lines.append(f"Last problem: {err}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Timers(bot))
    await bot.add_cog(TimerBoard(bot))
