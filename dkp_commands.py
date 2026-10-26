"""/dkp, /gear and /loot."""

import csv
import io
import logging
import time
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import events as events_mod
import heartbeat
import util
from dkp import (COLOR, MAX_POINTS, BatchButton, ClaimButton, add_entry, award_loot, balance, batch_embed, batch_view,
                 cfg, clean_text, decay_all, is_officer, need_officer, new_season, pick_winner, points_ok, rank_of,
                 reservers, reward_embed, sign, standings, totals, touch, undo_entry)
from dkp_board import DkpBoard

log = logging.getLogger("aion2.dkp")


# ---------------------------------------------------------------- autocomplete
async def _item_ac(interaction: discord.Interaction, current: str):
    rows = db.conn().execute(
        "SELECT name, cost FROM loot_items WHERE guild_id=? AND name LIKE ? ORDER BY name LIMIT 25",
        (interaction.guild_id, f"%{current}%"),
    ).fetchall()
    return [app_commands.Choice(name=f"{r['name']} ({r['cost']} DKP)"[:100], value=r["name"][:100]) for r in rows]


async def _reserved_ac(interaction: discord.Interaction, current: str):
    rows = db.conn().execute(
        "SELECT item FROM loot_reservations WHERE guild_id=? AND user_id=? AND item LIKE ? LIMIT 25",
        (interaction.guild_id, interaction.user.id, f"%{current}%"),
    ).fetchall()
    return [app_commands.Choice(name=r["item"][:100], value=r["item"][:100]) for r in rows]


async def _queue_item_ac(interaction: discord.Interaction, current: str):
    rows = db.conn().execute(
        """SELECT DISTINCT item FROM loot_reservations WHERE guild_id=? AND item LIKE ? LIMIT 25""",
        (interaction.guild_id, f"%{current}%"),
    ).fetchall()
    return [app_commands.Choice(name=r["item"][:100], value=r["item"][:100]) for r in rows]


async def _event_ac(interaction: discord.Interaction, current: str):
    rows = db.conn().execute(
        """SELECT * FROM events WHERE guild_id=? AND status!='cancelled' AND starts_at > ?
           ORDER BY starts_at DESC LIMIT 40""",
        (interaction.guild_id, int(time.time()) - 7 * 86400),
    ).fetchall()
    tz = util.get_tz(interaction.user.id)
    out = []
    for ev in rows:
        when = datetime.fromtimestamp(ev["starts_at"], tz).strftime("%a %b %d %I:%M %p")
        label = f"#{ev['id']} {events_mod.event_title(ev)} · {when}"
        if current.lower() in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=str(ev["id"])))
    return out[:25]


# ---------------------------------------------------------------- /dkp
class Dkp(commands.GroupCog, group_name="dkp", group_description="DKP points for gear"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        self.close_loop.start()

    async def cog_unload(self):
        self.close_loop.cancel()

    # ---- closing check-ins
    @tasks.loop(seconds=30)
    async def close_loop(self):
        try:
            rows = db.conn().execute(
                "SELECT * FROM dkp_rewards WHERE closed=0 AND closes_at <= ?", (int(time.time()),)
            ).fetchall()
            for r in rows:
                db.conn().execute("UPDATE dkp_rewards SET closed=1 WHERE id=?", (r["id"],))
                db.conn().commit()
                n = db.conn().execute("SELECT COUNT(*) AS n FROM dkp_claims WHERE reward_id=?", (r["id"],)).fetchone()["n"]
                if not (r["channel_id"] and r["message_id"]):
                    continue
                try:
                    channel = self.bot.get_channel(r["channel_id"]) or await self.bot.fetch_channel(r["channel_id"])
                    msg = channel.get_partial_message(r["message_id"])
                    view = discord.ui.View()
                    view.add_item(ClaimButton(r["id"], disabled=True))
                    await msg.edit(embed=reward_embed(r, n, True), view=view)
                except discord.HTTPException:
                    log.info("Could not close check-in message %s", r["id"])
            heartbeat.beat("dkp.close", self.close_loop)
        except Exception:
            log.exception("DKP close loop error")

    @close_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---- everyone
    @app_commands.command(name="me", description="Your DKP, rank and reservations (only you see it)")
    async def me(self, interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        bal = balance(gid, uid)
        earned, spent = totals(gid, uid)
        rank = rank_of(gid, uid)
        embed = discord.Embed(title=f"{interaction.user.display_name}'s DKP", color=COLOR)
        embed.add_field(name="Balance", value=f"**{bal:,}**")
        embed.add_field(name="Rank", value=f"#{rank} of {len(standings(gid))}" if rank else "Not ranked yet")
        embed.add_field(name="This season", value=f"Earned {earned:,} · Spent {spent:,}")
        rows = db.conn().execute(
            """SELECT * FROM dkp_ledger WHERE guild_id=? AND user_id=? AND season=? AND undone=0
               ORDER BY id DESC LIMIT 5""",
            (gid, uid, cfg(gid)["season"]),
        ).fetchall()
        if rows:
            embed.add_field(
                name="Recent",
                value="\n".join(f"`{sign(r['delta'])}` {r['reason'] or r['kind']} · {util.ts(r['created_at'], 'R')}" for r in rows),
                inline=False,
            )
        res = db.conn().execute(
            "SELECT r.item, i.cost FROM loot_reservations r LEFT JOIN loot_items i ON i.guild_id=r.guild_id AND i.name=r.item COLLATE NOCASE WHERE r.guild_id=? AND r.user_id=? ORDER BY r.created_at",
            (gid, uid),
        ).fetchall()
        if res:
            embed.add_field(
                name="Reserved gear",
                value="\n".join(f"{r['item']}" + (f" ({r['cost']} DKP)" if r["cost"] is not None else "") for r in res),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="top", description="DKP standings")
    @app_commands.describe(limit="How many to show (default 15)", public="Show it to the whole channel")
    async def top(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 3, 40] = 15,
        public: bool = False,
    ):
        rows = standings(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("Nobody has any DKP yet.", ephemeral=True)
            return
        lines = [f"`{i:>2}.` <@{uid}> · **{bal:,}**" for i, (uid, bal) in enumerate(rows[:limit], 1)]
        embed = discord.Embed(title="DKP standings", description=util.cut("\n".join(lines), 4000), color=COLOR)
        embed.set_footer(text=f"Season {cfg(interaction.guild_id)['season']} · {len(rows)} players")
        await interaction.response.send_message(
            embed=embed, ephemeral=not public, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="history", description="DKP history (yours, or any player's if you are an officer)")
    @app_commands.describe(member="Whose history (officers only)", limit="How many entries (default 10)")
    async def history(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ):
        target = member or interaction.user
        if target.id != interaction.user.id and not is_officer(interaction):
            await interaction.response.send_message("You can only see your own history.", ephemeral=True)
            return
        gid = interaction.guild_id
        rows = db.conn().execute(
            """SELECT * FROM dkp_ledger WHERE guild_id=? AND user_id=? AND season=? ORDER BY id DESC LIMIT ?""",
            (gid, target.id, cfg(gid)["season"], limit),
        ).fetchall()
        if not rows:
            await interaction.response.send_message("No DKP history yet.", ephemeral=True)
            return
        lines = [
            f"{'~~' if r['undone'] else ''}`#{r['id']}` `{sign(r['delta'])}` {r['reason'] or r['kind']} · {util.ts(r['created_at'], 'd')}{'~~ (undone)' if r['undone'] else ''}"
            for r in rows
        ]
        embed = discord.Embed(
            title=f"DKP history: {target.display_name}", description=util.cut("\n".join(lines), 4000), color=COLOR
        )
        embed.set_footer(text=f"Balance {balance(gid, target.id):,}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- officers: awarding
    @app_commands.command(name="award", description="Give a player DKP (officers)")
    @app_commands.describe(member="Who gets it", points="How many DKP", reason="Why (shown in their history)")
    async def award(self, interaction: discord.Interaction, member: discord.Member, points: int, reason: str = "Award"):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        if not points_ok(points) or member.bot:
            await interaction.response.send_message(f"Points must be between 1 and {MAX_POINTS:,}, for a real player.", ephemeral=True)
            return
        eid = add_entry(interaction.guild_id, member.id, points, "award", clean_text(reason), interaction.user.id)
        await interaction.response.send_message(
            f"**+{points:,} DKP** to {member.mention} for {clean_text(reason)}. Balance: {balance(interaction.guild_id, member.id):,} (entry #{eid}).",
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )

    @app_commands.command(name="take", description="Remove DKP from a player (officers)")
    @app_commands.describe(member="Who loses it", points="How many DKP", reason="Why (shown in their history)")
    async def take(self, interaction: discord.Interaction, member: discord.Member, points: int, reason: str = "Penalty"):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        if not points_ok(points) or member.bot:
            await interaction.response.send_message(f"Points must be between 1 and {MAX_POINTS:,}, for a real player.", ephemeral=True)
            return
        eid = add_entry(interaction.guild_id, member.id, -points, "adjust", clean_text(reason), interaction.user.id)
        await interaction.response.send_message(
            f"**-{points:,} DKP** from {member.mention} for {clean_text(reason)}. Balance: {balance(interaction.guild_id, member.id):,} (entry #{eid}).",
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )

    @app_commands.command(name="award-role", description="Give everyone with a role DKP (officers)")
    @app_commands.describe(role="The role", points="How many DKP each", reason="Why")
    async def award_role(self, interaction: discord.Interaction, role: discord.Role, points: int, reason: str = "Award"):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        members = [m for m in role.members if not m.bot]
        if not points_ok(points) or not members:
            await interaction.response.send_message(
                "Points must be 1 or more, and the role needs members I can see. (If the role looks empty, members may not be cached yet.)",
                ephemeral=True,
            )
            return
        for m in members:
            add_entry(interaction.guild_id, m.id, points, "award", clean_text(reason), interaction.user.id)
        await interaction.response.send_message(
            f"**+{points:,} DKP** each to {len(members)} members of {role.mention} for {clean_text(reason)}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="award-event", description="Give DKP to everyone confirmed on a planned event (officers)")
    @app_commands.describe(event="The event", points="How many DKP each", include_maybe="Also reward people who signed up Maybe")
    @app_commands.autocomplete(event=_event_ac)
    async def award_event(self, interaction: discord.Interaction, event: str, points: int, include_maybe: bool = False):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        try:
            ev = events_mod.get_event(int(event.lstrip("#").split()[0]))
        except (ValueError, IndexError):
            ev = None
        if not ev or ev["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Pick an event from the list that appears as you type.", ephemeral=True)
            return
        if not points_ok(points):
            await interaction.response.send_message(f"Points must be between 1 and {MAX_POINTS:,}.", ephemeral=True)
            return
        marker = f"event #{ev['id']}"
        done = db.conn().execute(
            "SELECT COUNT(*) AS n FROM dkp_ledger WHERE guild_id=? AND kind='event' AND reason LIKE ? AND undone=0",
            (interaction.guild_id, f"%{marker}%"),
        ).fetchone()["n"]
        if done:
            await interaction.response.send_message(
                f"DKP was already awarded for that event ({done} entries). Undo them first if you need to redo it.", ephemeral=True
            )
            return
        conf, _wait, maybe = events_mod.split_signups(ev, events_mod.get_signups(ev["id"]))
        people = conf + (maybe if include_maybe else [])
        if not people:
            await interaction.response.send_message("Nobody is signed up for that event.", ephemeral=True)
            return
        reason = f"{events_mod.event_title(ev)} ({marker})"
        for s in people:
            add_entry(interaction.guild_id, s["user_id"], points, "event", reason, interaction.user.id)
        await interaction.response.send_message(
            f"**+{points:,} DKP** each to {len(people)} players for **{events_mod.event_title(ev)}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="reward", description="Post a check-in button that gives DKP to whoever presses it (officers)")
    @app_commands.describe(name="What it is for, like Eternal Ludra clear", points="DKP for each person", minutes="How long check-in stays open (default 30)")
    async def reward(
        self,
        interaction: discord.Interaction,
        name: str,
        points: int,
        minutes: app_commands.Range[int, 1, 1440] = 30,
    ):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        if not points_ok(points) or not clean_text(name):
            await interaction.response.send_message(f"Give it a name and between 1 and {MAX_POINTS:,} points.", ephemeral=True)
            return
        c = db.conn()
        cur = c.execute(
            "INSERT INTO dkp_rewards (guild_id, channel_id, name, points, closes_at, created_by) VALUES (?,?,?,?,?,?)",
            (interaction.guild_id, interaction.channel_id, clean_text(name, 60), points, int(time.time()) + minutes * 60, interaction.user.id),
        )
        c.commit()
        r = c.execute("SELECT * FROM dkp_rewards WHERE id=?", (cur.lastrowid,)).fetchone()
        view = discord.ui.View(timeout=None)
        view.add_item(ClaimButton(r["id"]))
        await interaction.response.send_message(embed=reward_embed(r, 0, False), view=view)
        msg = await interaction.original_response()
        c.execute("UPDATE dkp_rewards SET message_id=? WHERE id=?", (msg.id, r["id"]))
        c.commit()

    # ---- officers: fixing things
    @app_commands.command(name="event-button", description="Post Award and Undo buttons for a planned event (officers)")
    @app_commands.describe(
        event="The planned event",
        points="DKP each confirmed player gets (you can change it on the panel)",
        include_maybe="Also pay people who signed up Maybe",
    )
    @app_commands.autocomplete(event=_event_ac)
    async def event_button(self, interaction: discord.Interaction, event: str, points: int, include_maybe: bool = False):
        if not await need_officer(interaction):
            return
        try:
            ev = events_mod.get_event(int(event.lstrip("#").split()[0]))
        except (ValueError, IndexError):
            ev = None
        if not ev or ev["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Pick an event from the list that appears as you type.", ephemeral=True)
            return
        if not points_ok(points):
            await interaction.response.send_message(f"Points must be between 1 and {MAX_POINTS:,}.", ephemeral=True)
            return
        c = db.conn()
        cur = c.execute(
            "INSERT INTO dkp_batches (guild_id, event_id, points, include_maybe, channel_id, created_by) VALUES (?,?,?,?,?,?)",
            (interaction.guild_id, ev["id"], points, int(include_maybe), interaction.channel_id, interaction.user.id),
        )
        c.commit()
        b = c.execute("SELECT * FROM dkp_batches WHERE id=?", (cur.lastrowid,)).fetchone()
        await interaction.response.send_message(embed=batch_embed(b), view=batch_view(b))
        msg = await interaction.original_response()
        c.execute("UPDATE dkp_batches SET message_id=? WHERE id=?", (msg.id, b["id"]))
        c.commit()

    @app_commands.command(name="undo", description="Undo one DKP entry by its number (officers)")
    @app_commands.describe(entry="The entry number from /dkp history")
    async def undo(self, interaction: discord.Interaction, entry: int):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        row = undo_entry(interaction.guild_id, entry)
        if not row:
            await interaction.response.send_message("I can't find that entry, or it is already undone or from an old season.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Undid entry #{entry} ({sign(row['delta'])} for <@{row['user_id']}>: {row['reason'] or row['kind']}). "
            f"Their balance is now {balance(interaction.guild_id, row['user_id']):,}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="decay", description="Take a percentage off everyone's positive DKP (officers)")
    @app_commands.describe(percent="Percent to remove, 1 to 100")
    async def decay(self, interaction: discord.Interaction, percent: app_commands.Range[int, 1, 100]):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        n = decay_all(interaction.guild_id, percent, interaction.user.id)
        await interaction.response.send_message(f"Removed {percent}% from {n} players' balances.")

    @app_commands.command(name="season", description="Start a new season: everyone goes back to 0 (managers)")
    @app_commands.describe(confirm="Set to True to confirm. Old history is kept but no longer counts")
    async def season(self, interaction: discord.Interaction, confirm: bool = False):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        if not confirm:
            await interaction.response.send_message(
                "This sets everyone's DKP to 0 and clears all gear reservations. Run it again with `confirm:True` to go ahead.",
                ephemeral=True,
            )
            return
        n = new_season(interaction.guild_id)
        touch(interaction.client, interaction.guild_id)
        await interaction.response.send_message(f"Season {n} started. Everyone is at 0 DKP and reservations are cleared.")

    @app_commands.command(name="export", description="Download standings as a CSV file (officers)")
    async def export(self, interaction: discord.Interaction):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        rows = standings(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("Nobody has any DKP yet.", ephemeral=True)
            return
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["rank", "discord_id", "name", "dkp", "earned", "spent_on_loot"])
        for i, (uid, bal) in enumerate(rows, 1):
            m = interaction.guild.get_member(uid)
            earned, spent = totals(interaction.guild_id, uid)
            w.writerow([i, uid, m.display_name if m else "", bal, earned, spent])
        file = discord.File(io.BytesIO(out.getvalue().encode()), filename="dkp-standings.csv")
        await interaction.response.send_message(file=file, ephemeral=True)

    @app_commands.command(name="config", description="DKP settings (managers)")
    @app_commands.describe(
        max_reservations="How many items a player can reserve at once (1 to 10)",
        allow_negative="Let players win gear that costs more than their DKP",
        officer_role="A role that can award and take DKP and run loot, besides managers",
        clear_officer_role="Remove the officer role",
    )
    async def config(
        self,
        interaction: discord.Interaction,
        max_reservations: Optional[app_commands.Range[int, 1, 10]] = None,
        allow_negative: Optional[bool] = None,
        officer_role: Optional[discord.Role] = None,
        clear_officer_role: bool = False,
    ):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        gid = interaction.guild_id
        cfg(gid)
        c = db.conn()
        if max_reservations is not None:
            c.execute("UPDATE dkp_config SET max_reservations=? WHERE guild_id=?", (max_reservations, gid))
        if allow_negative is not None:
            c.execute("UPDATE dkp_config SET allow_negative=? WHERE guild_id=?", (int(allow_negative), gid))
        if officer_role is not None:
            c.execute("UPDATE dkp_config SET officer_role=? WHERE guild_id=?", (officer_role.id, gid))
        if clear_officer_role:
            c.execute("UPDATE dkp_config SET officer_role=NULL WHERE guild_id=?", (gid,))
        c.commit()
        s = cfg(gid)
        role_txt = "<@&%d>" % s["officer_role"] if s["officer_role"] else "none"
        await interaction.response.send_message(
            f"**DKP settings**\nSeason: {s['season']}\nMax reservations per player: {s['max_reservations']}\n"
            f"Winning gear you can't afford: {'allowed' if s['allow_negative'] else 'not allowed'}\n"
            f"Officer role: {role_txt}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# ---------------------------------------------------------------- /gear (players)
class Gear(commands.GroupCog, group_name="gear", group_description="Reserve gear with your DKP"):
    @app_commands.command(name="list", description="Gear that can be reserved, with DKP costs")
    @app_commands.describe(source="Only gear from this boss or place")
    async def list_items(self, interaction: discord.Interaction, source: Optional[str] = None):
        rows = db.conn().execute(
            "SELECT i.*, (SELECT COUNT(*) FROM loot_reservations r WHERE r.guild_id=i.guild_id AND r.item=i.name COLLATE NOCASE) AS n "
            "FROM loot_items i WHERE guild_id=? ORDER BY source, name",
            (interaction.guild_id,),
        ).fetchall()
        if source:
            rows = [r for r in rows if source.lower() in (r["source"] or "").lower()]
        if not rows:
            await interaction.response.send_message(
                "No gear is listed yet. A manager adds it with `/loot add`." if not source else "Nothing matches that source.",
                ephemeral=True,
            )
            return
        by_src: dict[str, list[str]] = {}
        for r in rows:
            slot = f" · {r['slot']}" if r["slot"] else ""
            by_src.setdefault(r["source"] or "Other", []).append(f"**{r['name']}** · {r['cost']} DKP{slot} · {r['n']} reserved")
        embed = discord.Embed(title="Reservable gear", color=COLOR)
        for src in sorted(by_src):
            embed.add_field(name=src[:250], value=util.cut("\n".join(by_src[src]), 1000), inline=False)
            if len(embed.fields) >= 24:
                break
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @list_items.autocomplete("source")
    async def _src_ac(self, interaction: discord.Interaction, current: str):
        rows = db.conn().execute(
            "SELECT DISTINCT source FROM loot_items WHERE guild_id=? AND source IS NOT NULL AND source LIKE ? LIMIT 25",
            (interaction.guild_id, f"%{current}%"),
        ).fetchall()
        return [app_commands.Choice(name=r["source"][:100], value=r["source"][:100]) for r in rows]

    @app_commands.command(name="reserve", description="Reserve a piece of gear. The highest DKP gets it when it drops")
    @app_commands.describe(item="The gear")
    @app_commands.autocomplete(item=_item_ac)
    async def reserve(self, interaction: discord.Interaction, item: str):
        gid, uid = interaction.guild_id, interaction.user.id
        row = db.conn().execute("SELECT * FROM loot_items WHERE guild_id=? AND name=? COLLATE NOCASE", (gid, item)).fetchone()
        if not row:
            await interaction.response.send_message("Pick gear from the list that appears as you type.", ephemeral=True)
            return
        mine = db.conn().execute("SELECT item FROM loot_reservations WHERE guild_id=? AND user_id=?", (gid, uid)).fetchall()
        if any(r["item"].lower() == row["name"].lower() for r in mine):
            await interaction.response.send_message("You already reserved that.", ephemeral=True)
            return
        limit = cfg(gid)["max_reservations"]
        if len(mine) >= limit:
            await interaction.response.send_message(
                f"You can reserve {limit} item{'s' if limit != 1 else ''} at once. Free one with `/gear unreserve`.", ephemeral=True
            )
            return
        db.conn().execute(
            "INSERT INTO loot_reservations (guild_id, user_id, item, created_at) VALUES (?,?,?,?)",
            (gid, uid, row["name"], int(time.time())),
        )
        db.conn().commit()
        bal = balance(gid, uid)
        queue = reservers(gid, row["name"])
        pos = [u for u, _, _ in queue].index(uid) + 1
        warn = ""
        if bal < row["cost"] and not cfg(gid)["allow_negative"]:
            warn = f"\nYou have {bal:,} DKP and it costs {row['cost']}, so you'll need {row['cost'] - bal:,} more to win it."
        await interaction.response.send_message(
            f"Reserved **{row['name']}** ({row['cost']} DKP). You're #{pos} of {len(queue)} right now.{warn}", ephemeral=True
        )

    @app_commands.command(name="unreserve", description="Give up a reservation")
    @app_commands.describe(item="The gear")
    @app_commands.autocomplete(item=_reserved_ac)
    async def unreserve(self, interaction: discord.Interaction, item: str):
        cur = db.conn().execute(
            "DELETE FROM loot_reservations WHERE guild_id=? AND user_id=? AND item=? COLLATE NOCASE",
            (interaction.guild_id, interaction.user.id, item),
        )
        db.conn().commit()
        await interaction.response.send_message(
            f"Removed your reservation for **{item}**." if cur.rowcount else "You haven't reserved that.", ephemeral=True
        )

    @app_commands.command(name="queue", description="Who has reserved a piece of gear, in the order it would go")
    @app_commands.describe(item="The gear")
    @app_commands.autocomplete(item=_queue_item_ac)
    async def queue(self, interaction: discord.Interaction, item: str):
        gid = interaction.guild_id
        row = db.conn().execute("SELECT * FROM loot_items WHERE guild_id=? AND name=? COLLATE NOCASE", (gid, item)).fetchone()
        q = reservers(gid, item)
        if not q:
            await interaction.response.send_message("Nobody has reserved that.", ephemeral=True)
            return
        cost = row["cost"] if row else None
        lines = []
        for i, (uid, bal, _) in enumerate(q, 1):
            ok = "" if cost is None or bal >= cost or cfg(gid)["allow_negative"] else " (can't afford yet)"
            lines.append(f"`{i}.` <@{uid}> · {bal:,} DKP{ok}")
        title = f"{row['name']}" + (f" · {cost} DKP" if cost is not None else "")
        await interaction.response.send_message(
            embed=discord.Embed(title=title, description=util.cut("\n".join(lines), 4000), color=COLOR),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="mine", description="Your reserved gear")
    async def mine(self, interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        rows = db.conn().execute("SELECT item FROM loot_reservations WHERE guild_id=? AND user_id=? ORDER BY created_at", (gid, uid)).fetchall()
        if not rows:
            await interaction.response.send_message("You haven't reserved anything. Use `/gear reserve`.", ephemeral=True)
            return
        lines = []
        for r in rows:
            q = [u for u, _, _ in reservers(gid, r["item"])]
            item = db.conn().execute("SELECT cost FROM loot_items WHERE guild_id=? AND name=? COLLATE NOCASE", (gid, r["item"])).fetchone()
            lines.append(f"**{r['item']}**" + (f" · {item['cost']} DKP" if item else "") + f" · you're #{q.index(uid) + 1} of {len(q)}")
        lines.append(f"\nYour DKP: **{balance(gid, uid):,}**")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------- /loot (officers)
class Loot(commands.GroupCog, group_name="loot", group_description="Manage gear and hand it out (officers)"):
    @app_commands.command(name="add", description="Add gear to the list, or change its cost")
    @app_commands.describe(item="Gear name", cost="DKP cost", slot="Optional slot, like Weapon", source="Optional boss or place it drops")
    async def add(
        self,
        interaction: discord.Interaction,
        item: str,
        cost: app_commands.Range[int, 0, 100000],
        slot: Optional[str] = None,
        source: Optional[str] = None,
    ):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        name = clean_text(item)
        if not name:
            await interaction.response.send_message("Give the gear a name.", ephemeral=True)
            return
        db.conn().execute(
            """INSERT INTO loot_items (guild_id, name, cost, slot, source) VALUES (?,?,?,?,?)
               ON CONFLICT(guild_id, name) DO UPDATE SET cost=excluded.cost,
                 slot=COALESCE(excluded.slot, loot_items.slot), source=COALESCE(excluded.source, loot_items.source)""",
            (interaction.guild_id, name, cost, clean_text(slot, 40) or None if slot else None, clean_text(source, 80) or None if source else None),
        )
        db.conn().commit()
        await interaction.response.send_message(f"Saved **{name}** at {cost} DKP.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove gear from the list (clears its reservations)")
    @app_commands.describe(item="Gear name")
    @app_commands.autocomplete(item=_item_ac)
    async def remove(self, interaction: discord.Interaction, item: str):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        c = db.conn()
        cur = c.execute("DELETE FROM loot_items WHERE guild_id=? AND name=? COLLATE NOCASE", (interaction.guild_id, item))
        c.execute("DELETE FROM loot_reservations WHERE guild_id=? AND item=? COLLATE NOCASE", (interaction.guild_id, item))
        c.commit()
        await interaction.response.send_message(f"Removed **{item}**." if cur.rowcount else "That gear isn't listed.", ephemeral=True)

    @app_commands.command(name="award", description="Give gear to the top reserver, or to a player you pick, and charge their DKP")
    @app_commands.describe(item="The gear that dropped", member="Give it to this player instead of the top reserver")
    @app_commands.autocomplete(item=_item_ac)
    async def award(self, interaction: discord.Interaction, item: str, member: Optional[discord.Member] = None):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        gid = interaction.guild_id
        row = db.conn().execute("SELECT * FROM loot_items WHERE guild_id=? AND name=? COLLATE NOCASE", (gid, item)).fetchone()
        if not row:
            await interaction.response.send_message("Pick gear from the list that appears as you type.", ephemeral=True)
            return
        skipped: list[int] = []
        if member:
            winner = member.id
        else:
            winner, skipped = pick_winner(gid, row["name"])
            if winner is None:
                msg = "Nobody has reserved that."
                if skipped:
                    msg = "Everyone who reserved it is short on DKP. Pick a player yourself, or turn on `/dkp config allow_negative`."
                await interaction.response.send_message(msg, ephemeral=True)
                return
        entry = award_loot(gid, row["name"], winner, interaction.user.id)
        if entry is None:
            await interaction.response.send_message(
                f"<@{winner}> has {balance(gid, winner):,} DKP and **{row['name']}** costs {row['cost']}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        note = f"\n(Skipped, not enough DKP: {', '.join(f'<@{u}>' for u in skipped)})" if skipped else ""
        await interaction.response.send_message(
            f"**{row['name']}** goes to <@{winner}> for **{row['cost']} DKP**. Balance: {balance(gid, winner):,}. (entry #{entry}){note}",
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(winner)]),
        )

    @app_commands.command(name="clear", description="Clear every reservation for a piece of gear")
    @app_commands.describe(item="Gear name")
    @app_commands.autocomplete(item=_queue_item_ac)
    async def clear(self, interaction: discord.Interaction, item: str):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        cur = db.conn().execute("DELETE FROM loot_reservations WHERE guild_id=? AND item=? COLLATE NOCASE", (interaction.guild_id, item))
        db.conn().commit()
        await interaction.response.send_message(f"Cleared {cur.rowcount} reservation(s) for **{item}**.", ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(ClaimButton, BatchButton)
    await bot.add_cog(Dkp(bot))
    await bot.add_cog(DkpBoard(bot))
    await bot.add_cog(Gear())
    await bot.add_cog(Loot())
