"""DKP (Dragon Kill Points) for gear: standings, reward events, gear reservations and loot awards.

Everything is per server. Points live in a ledger, so every change is traceable and can be undone."""

import csv
import io
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
import events as events_mod
import util

log = logging.getLogger("aion2.dkp")
MAX_POINTS = 100000
COLOR = 0xA9741A


# ---------------------------------------------------------------- data (no Discord needed)
def cfg(guild_id: int):
    c = db.conn()
    c.execute("INSERT OR IGNORE INTO dkp_config (guild_id) VALUES (?)", (guild_id,))
    c.commit()
    return c.execute("SELECT * FROM dkp_config WHERE guild_id=?", (guild_id,)).fetchone()


def balance(guild_id: int, user_id: int) -> int:
    row = db.conn().execute(
        """SELECT COALESCE(SUM(delta), 0) AS b FROM dkp_ledger
           WHERE guild_id=? AND user_id=? AND season=? AND undone=0""",
        (guild_id, user_id, cfg(guild_id)["season"]),
    ).fetchone()
    return row["b"]


def totals(guild_id: int, user_id: int) -> tuple[int, int]:
    """(earned, spent) this season. Spent counts loot purchases only."""
    s = cfg(guild_id)["season"]
    c = db.conn()
    earned = c.execute(
        "SELECT COALESCE(SUM(delta),0) AS x FROM dkp_ledger WHERE guild_id=? AND user_id=? AND season=? AND undone=0 AND delta>0",
        (guild_id, user_id, s),
    ).fetchone()["x"]
    spent = c.execute(
        "SELECT COALESCE(-SUM(delta),0) AS x FROM dkp_ledger WHERE guild_id=? AND user_id=? AND season=? AND undone=0 AND kind='loot'",
        (guild_id, user_id, s),
    ).fetchone()["x"]
    return earned, spent


def standings(guild_id: int) -> list[tuple[int, int]]:
    """[(user_id, balance)] best first. Ties keep the earlier earner on top."""
    rows = db.conn().execute(
        """SELECT user_id, SUM(delta) AS b, MIN(id) AS first FROM dkp_ledger
           WHERE guild_id=? AND season=? AND undone=0 GROUP BY user_id ORDER BY b DESC, first ASC""",
        (guild_id, cfg(guild_id)["season"]),
    ).fetchall()
    return [(r["user_id"], r["b"]) for r in rows]


def rank_of(guild_id: int, user_id: int) -> Optional[int]:
    for i, (uid, _b) in enumerate(standings(guild_id), 1):
        if uid == user_id:
            return i
    return None


def add_entry(
    guild_id: int, user_id: int, delta: int, kind: str, reason: str, by: Optional[int], batch_id: Optional[int] = None
) -> int:
    c = db.conn()
    cur = c.execute(
        """INSERT INTO dkp_ledger (guild_id, user_id, delta, kind, reason, by_user, season, created_at, batch_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (guild_id, user_id, delta, kind, reason[:200], by, cfg(guild_id)["season"], int(time.time()), batch_id),
    )
    c.commit()
    return cur.lastrowid


def reservers(guild_id: int, item: str) -> list[tuple[int, int, int]]:
    """[(user_id, balance, reserved_at)] in priority order: highest DKP first, earlier reservation breaks ties."""
    rows = db.conn().execute(
        "SELECT user_id, created_at FROM loot_reservations WHERE guild_id=? AND item=? COLLATE NOCASE", (guild_id, item)
    ).fetchall()
    out = [(r["user_id"], balance(guild_id, r["user_id"]), r["created_at"]) for r in rows]
    out.sort(key=lambda x: (-x[1], x[2], x[0]))
    return out


def pick_winner(guild_id: int, item: str):
    """The top reserver who can afford the item. Returns (winner_id or None, [user ids skipped for lack of DKP])."""
    row = db.conn().execute("SELECT cost FROM loot_items WHERE guild_id=? AND name=? COLLATE NOCASE", (guild_id, item)).fetchone()
    if not row:
        return None, []
    allow_neg = bool(cfg(guild_id)["allow_negative"])
    skipped = []
    for uid, bal, _ in reservers(guild_id, item):
        if allow_neg or bal >= row["cost"]:
            return uid, skipped
        skipped.append(uid)
    return None, skipped


def award_loot(guild_id: int, item: str, winner_id: int, by: int) -> Optional[int]:
    """Charge the winner and clear that item's reservations. None if they can't afford it."""
    row = db.conn().execute(
        "SELECT name, cost FROM loot_items WHERE guild_id=? AND name=? COLLATE NOCASE", (guild_id, item)
    ).fetchone()
    if not row:
        return None
    if not cfg(guild_id)["allow_negative"] and balance(guild_id, winner_id) < row["cost"]:
        return None
    entry = add_entry(guild_id, winner_id, -row["cost"], "loot", f"Won {row['name']}", by)
    db.conn().execute("DELETE FROM loot_reservations WHERE guild_id=? AND item=? COLLATE NOCASE", (guild_id, row["name"]))
    db.conn().commit()
    return entry


def decay_all(guild_id: int, percent: int, by: int) -> int:
    n = 0
    for uid, bal in standings(guild_id):
        cut = bal * percent // 100
        if bal > 0 and cut > 0:
            add_entry(guild_id, uid, -cut, "decay", f"{percent}% decay", by)
            n += 1
    return n


def undo_entry(guild_id: int, entry_id: int):
    row = db.conn().execute("SELECT * FROM dkp_ledger WHERE id=? AND guild_id=?", (entry_id, guild_id)).fetchone()
    if not row or row["undone"] or row["season"] != cfg(guild_id)["season"]:
        return None
    db.conn().execute("UPDATE dkp_ledger SET undone=1 WHERE id=?", (entry_id,))
    db.conn().commit()
    return row


def new_season(guild_id: int) -> int:
    n = cfg(guild_id)["season"] + 1
    c = db.conn()
    c.execute("UPDATE dkp_config SET season=? WHERE guild_id=?", (n, guild_id))
    c.execute("DELETE FROM loot_reservations WHERE guild_id=?", (guild_id,))
    c.commit()
    return n


def claim_reward(reward_id: int, user_id: int):
    """Returns (status, reward row). Status: ok, closed, already, missing."""
    c = db.conn()
    r = c.execute("SELECT * FROM dkp_rewards WHERE id=?", (reward_id,)).fetchone()
    if not r:
        return "missing", None
    if r["closed"] or r["closes_at"] <= time.time():
        return "closed", r
    try:
        c.execute("INSERT INTO dkp_claims (reward_id, user_id) VALUES (?,?)", (reward_id, user_id))
    except Exception:
        return "already", r
    c.commit()
    add_entry(r["guild_id"], user_id, r["points"], "event", r["name"], r["created_by"])
    return "ok", r


def sign(n: int) -> str:
    return f"+{n:,}" if n > 0 else f"{n:,}"


def _clean(text: str, limit: int = 100) -> str:
    return " ".join((text or "").split())[:limit]


# ---------------------------------------------------------------- permissions
def is_officer(interaction: discord.Interaction) -> bool:
    if util.is_admin(interaction):
        return True
    role_id = cfg(interaction.guild_id)["officer_role"]
    return bool(role_id and any(r.id == role_id for r in getattr(interaction.user, "roles", [])))


async def need_officer(interaction: discord.Interaction) -> bool:
    if is_officer(interaction):
        return True
    await interaction.response.send_message(
        "Only server managers or the DKP officer role can do that.", ephemeral=True
    )
    return False


def _points_ok(points: int) -> bool:
    return 1 <= points <= MAX_POINTS


# ---------------------------------------------------------------- check-in button
class ClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"dkp:claim:(?P<id>\d+)"):
    def __init__(self, reward_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="Check in", style=discord.ButtonStyle.success, custom_id=f"dkp:claim:{reward_id}", disabled=disabled
            )
        )
        self.reward_id = reward_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        status, r = claim_reward(self.reward_id, interaction.user.id)
        if status == "ok":
            touch(interaction.client, r["guild_id"])
            msg = f"Checked in. You earned **{r['points']:,} DKP** for {r['name']}. Balance: **{balance(r['guild_id'], interaction.user.id):,}**."
        elif status == "already":
            msg = "You already checked in for this one."
        elif status == "closed":
            msg = "Check-in for this event has closed."
        else:
            msg = "That event no longer exists."
        await interaction.response.send_message(msg, ephemeral=True)


def reward_embed(r, count: int, closed: bool) -> discord.Embed:
    embed = discord.Embed(
        title=f"DKP: {r['name']}",
        description=f"**{r['points']:,} DKP** for everyone who checks in.",
        color=COLOR,
    )
    if closed:
        embed.add_field(name="Status", value=f"Closed. {count} checked in.")
    else:
        embed.add_field(name="Closes", value=util.ts(r["closes_at"], "R"))
        embed.add_field(name="Checked in", value=str(count))
    return embed



# ---------------------------------------------------------------- event award panels (award + undo buttons)
def event_paid(guild_id: int, event_id: int) -> int:
    """How many live DKP entries already pay out this event."""
    return db.conn().execute(
        "SELECT COUNT(*) AS n FROM dkp_ledger WHERE guild_id=? AND kind='event' AND reason LIKE ? AND undone=0",
        (guild_id, f"%(event #{event_id})%"),
    ).fetchone()["n"]


def batch_people(b) -> list[int]:
    ev = events_mod.get_event(b["event_id"])
    if not ev:
        return []
    conf, _wait, maybe = events_mod.split_signups(ev, events_mod.get_signups(ev["id"]))
    people = conf + (maybe if b["include_maybe"] else [])
    return [s["user_id"] for s in people]


def award_batch(batch_id: int, by: int):
    """Pay everyone on the event. Returns (status, count): ok, done, paid, empty or missing."""
    c = db.conn()
    b = c.execute("SELECT * FROM dkp_batches WHERE id=?", (batch_id,)).fetchone()
    ev = events_mod.get_event(b["event_id"]) if b else None
    if not b or not ev:
        return "missing", 0
    if b["state"] == "awarded":
        return "done", b["paid_count"]
    if event_paid(b["guild_id"], b["event_id"]):
        return "paid", 0
    people = batch_people(b)
    if not people:
        return "empty", 0
    reason = f"{events_mod.event_title(ev)} (event #{ev['id']})"
    for uid in people:
        add_entry(b["guild_id"], uid, b["points"], "event", reason, by, batch_id)
    c.execute(
        "UPDATE dkp_batches SET state='awarded', paid_count=?, acted_by=?, acted_at=? WHERE id=?",
        (len(people), by, int(time.time()), batch_id),
    )
    c.commit()
    return "ok", len(people)


def undo_batch(batch_id: int, by: int):
    """Reverse everything this panel paid. Returns (status, count): ok, notpaid or missing."""
    c = db.conn()
    b = c.execute("SELECT * FROM dkp_batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        return "missing", 0
    if b["state"] != "awarded":
        return "notpaid", 0
    cur = c.execute("UPDATE dkp_ledger SET undone=1 WHERE batch_id=? AND undone=0", (batch_id,))
    c.execute("UPDATE dkp_batches SET state='undone', acted_by=?, acted_at=? WHERE id=?", (by, int(time.time()), batch_id))
    c.commit()
    return "ok", cur.rowcount


def batch_embed(b) -> discord.Embed:
    ev = events_mod.get_event(b["event_id"])
    title = events_mod.event_title(ev) if ev else f"event #{b['event_id']}"
    embed = discord.Embed(title=f"DKP for {title}", color=COLOR)
    people = batch_people(b)
    embed.add_field(name="Amount", value=f"**{b['points']:,} DKP** each")
    if b["state"] == "awarded":
        embed.add_field(name="Status", value=f"Paid to {b['paid_count']} players by <@{b['acted_by']}> {util.ts(b['acted_at'], 'R')}")
    elif b["state"] == "undone":
        embed.add_field(name="Status", value=f"Undone by <@{b['acted_by']}> {util.ts(b['acted_at'], 'R')}. Nothing is paid now.")
    else:
        embed.add_field(name="Status", value=f"Ready. {len(people)} players would be paid" + (" (including Maybe)" if b["include_maybe"] else "") + ".")
    embed.set_footer(text="Only server managers and the DKP officer role can use these buttons.")
    return embed


def batch_view(b) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    awarded = b["state"] == "awarded"
    view.add_item(BatchButton(b["id"], "award", f"Award {b['points']:,} DKP", discord.ButtonStyle.success, awarded))
    view.add_item(BatchButton(b["id"], "undo", "Undo", discord.ButtonStyle.danger, not awarded))
    view.add_item(BatchButton(b["id"], "amount", "Change amount", discord.ButtonStyle.secondary, awarded))
    return view


class BatchButton(discord.ui.DynamicItem[discord.ui.Button], template=r"dkp:batch:(?P<id>\d+):(?P<act>award|undo|amount)"):
    def __init__(self, batch_id: int, act: str, label: str = "", style=discord.ButtonStyle.secondary, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label=label or act.title(), style=style, disabled=disabled, custom_id=f"dkp:batch:{batch_id}:{act}"
            )
        )
        self.batch_id, self.act = batch_id, act

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]), match["act"])

    async def callback(self, interaction: discord.Interaction):
        b = db.conn().execute("SELECT * FROM dkp_batches WHERE id=?", (self.batch_id,)).fetchone()
        if not b or b["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("This DKP panel no longer exists.", ephemeral=True)
            return
        if not is_officer(interaction):
            await interaction.response.send_message("Only server managers or the DKP officer role can use this.", ephemeral=True)
            return
        if self.act == "amount":
            if b["state"] == "awarded":
                await interaction.response.send_message("Undo the award first, then change the amount.", ephemeral=True)
                return
            await interaction.response.send_modal(AmountModal(self.batch_id, b["points"]))
            return
        if self.act == "award":
            status, n = award_batch(self.batch_id, interaction.user.id)
            problem = {
                "done": "That was already paid.",
                "paid": "DKP for this event was already paid another way. Undo those entries in `/dkp history` first.",
                "empty": "Nobody is signed up for that event.",
                "missing": "That event no longer exists.",
            }.get(status)
        else:
            status, n = undo_batch(self.batch_id, interaction.user.id)
            problem = {"notpaid": "Nothing has been paid by this panel.", "missing": "This DKP panel no longer exists."}.get(status)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        b = db.conn().execute("SELECT * FROM dkp_batches WHERE id=?", (self.batch_id,)).fetchone()
        await interaction.response.edit_message(embed=batch_embed(b), view=batch_view(b))
        touch(interaction.client, b["guild_id"])


class AmountModal(discord.ui.Modal, title="DKP for this event"):
    amount = discord.ui.TextInput(label="DKP for each player", placeholder="For example 10", max_length=6)

    def __init__(self, batch_id: int, current: int):
        super().__init__()
        self.batch_id = batch_id
        self.amount.default = str(current)

    async def on_submit(self, interaction: discord.Interaction):
        b = db.conn().execute("SELECT * FROM dkp_batches WHERE id=?", (self.batch_id,)).fetchone()
        try:
            points = int(str(self.amount.value).replace(",", "").strip())
        except ValueError:
            points = 0
        if not b or b["guild_id"] != interaction.guild_id or not is_officer(interaction):
            await interaction.response.send_message("You can't change that.", ephemeral=True)
            return
        if not _points_ok(points):
            await interaction.response.send_message(f"Enter a whole number from 1 to {MAX_POINTS:,}.", ephemeral=True)
            return
        if b["state"] == "awarded":
            await interaction.response.send_message("Undo the award first, then change the amount.", ephemeral=True)
            return
        db.conn().execute("UPDATE dkp_batches SET points=? WHERE id=?", (points, self.batch_id))
        db.conn().commit()
        b = db.conn().execute("SELECT * FROM dkp_batches WHERE id=?", (self.batch_id,)).fetchone()
        await interaction.response.edit_message(embed=batch_embed(b), view=batch_view(b))


# ---------------------------------------------------------------- live standings board (every player, no limit)
PAGE_CHARS = 3800


def build_pages(rows: list[tuple[int, int]]) -> list[str]:
    """Split the full standings into chunks that each fit one embed."""
    pages, cur, size = [], [], 0
    for i, (uid, bal) in enumerate(rows, 1):
        line = f"`{i:>3}.` <@{uid}> · **{bal:,}**"
        if cur and size + len(line) + 1 > PAGE_CHARS:
            pages.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        pages.append("\n".join(cur))
    return pages


def board_embeds(guild_id: int) -> list[discord.Embed]:
    rows = standings(guild_id)
    season = cfg(guild_id)["season"]
    pages = build_pages(rows) or ["Nobody has any DKP yet."]
    out = []
    for i, text in enumerate(pages, 1):
        embed = discord.Embed(title="DKP standings" if i == 1 else "DKP standings (cont.)", description=text, color=COLOR)
        embed.set_footer(text=f"Season {season} · {len(rows)} players · page {i}/{len(pages)}")
        out.append(embed)
    return out


_pending: dict[int, "object"] = {}


def touch(bot, guild_id: int) -> None:
    """Something changed DKP: refresh that server's board in a few seconds (many changes in a row refresh once)."""
    import asyncio

    cog = bot.get_cog("DkpBoard")
    if cog is None or not db.conn().execute("SELECT 1 FROM dkp_boards WHERE guild_id=?", (guild_id,)).fetchone():
        return
    old = _pending.pop(guild_id, None)
    if old:
        old.cancel()

    async def later():
        try:
            await asyncio.sleep(5)
            _pending.pop(guild_id, None)
            await cog.refresh(guild_id)
        except asyncio.CancelledError:
            pass

    _pending[guild_id] = asyncio.get_running_loop().create_task(later())


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
        if not _points_ok(points) or member.bot:
            await interaction.response.send_message(f"Points must be between 1 and {MAX_POINTS:,}, for a real player.", ephemeral=True)
            return
        eid = add_entry(interaction.guild_id, member.id, points, "award", _clean(reason), interaction.user.id)
        await interaction.response.send_message(
            f"**+{points:,} DKP** to {member.mention} for {_clean(reason)}. Balance: {balance(interaction.guild_id, member.id):,} (entry #{eid}).",
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )

    @app_commands.command(name="take", description="Remove DKP from a player (officers)")
    @app_commands.describe(member="Who loses it", points="How many DKP", reason="Why (shown in their history)")
    async def take(self, interaction: discord.Interaction, member: discord.Member, points: int, reason: str = "Penalty"):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        if not _points_ok(points) or member.bot:
            await interaction.response.send_message(f"Points must be between 1 and {MAX_POINTS:,}, for a real player.", ephemeral=True)
            return
        eid = add_entry(interaction.guild_id, member.id, -points, "adjust", _clean(reason), interaction.user.id)
        await interaction.response.send_message(
            f"**-{points:,} DKP** from {member.mention} for {_clean(reason)}. Balance: {balance(interaction.guild_id, member.id):,} (entry #{eid}).",
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )

    @app_commands.command(name="award-role", description="Give everyone with a role DKP (officers)")
    @app_commands.describe(role="The role", points="How many DKP each", reason="Why")
    async def award_role(self, interaction: discord.Interaction, role: discord.Role, points: int, reason: str = "Award"):
        if not await need_officer(interaction):
            return
        touch(interaction.client, interaction.guild_id)
        members = [m for m in role.members if not m.bot]
        if not _points_ok(points) or not members:
            await interaction.response.send_message(
                "Points must be 1 or more, and the role needs members I can see. (If the role looks empty, members may not be cached yet.)",
                ephemeral=True,
            )
            return
        for m in members:
            add_entry(interaction.guild_id, m.id, points, "award", _clean(reason), interaction.user.id)
        await interaction.response.send_message(
            f"**+{points:,} DKP** each to {len(members)} members of {role.mention} for {_clean(reason)}.",
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
        if not _points_ok(points):
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
        if not _points_ok(points) or not _clean(name):
            await interaction.response.send_message(f"Give it a name and between 1 and {MAX_POINTS:,} points.", ephemeral=True)
            return
        c = db.conn()
        cur = c.execute(
            "INSERT INTO dkp_rewards (guild_id, channel_id, name, points, closes_at, created_by) VALUES (?,?,?,?,?,?)",
            (interaction.guild_id, interaction.channel_id, _clean(name, 60), points, int(time.time()) + minutes * 60, interaction.user.id),
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
        if not _points_ok(points):
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
        name = _clean(item)
        if not name:
            await interaction.response.send_message("Give the gear a name.", ephemeral=True)
            return
        db.conn().execute(
            """INSERT INTO loot_items (guild_id, name, cost, slot, source) VALUES (?,?,?,?,?)
               ON CONFLICT(guild_id, name) DO UPDATE SET cost=excluded.cost,
                 slot=COALESCE(excluded.slot, loot_items.slot), source=COALESCE(excluded.source, loot_items.source)""",
            (interaction.guild_id, name, cost, _clean(slot, 40) or None if slot else None, _clean(source, 80) or None if source else None),
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


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(ClaimButton, BatchButton)
    await bot.add_cog(Dkp(bot))
    await bot.add_cog(DkpBoard(bot))
    await bot.add_cog(Gear())
    await bot.add_cog(Loot())
