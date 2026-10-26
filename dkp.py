"""DKP (Dragon Kill Points) for gear: standings, reward events, gear reservations and loot awards.

Everything is per server. Points live in a ledger, so every change is traceable and can be undone."""

import logging
import time
from typing import Optional

import discord

import db
import errors
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


def clean_text(text: str, limit: int = 100) -> str:
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


def points_ok(points: int) -> bool:
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

    @errors.guarded
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

    @errors.guarded
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

    @errors.guarded
    async def on_submit(self, interaction: discord.Interaction):
        b = db.conn().execute("SELECT * FROM dkp_batches WHERE id=?", (self.batch_id,)).fetchone()
        try:
            points = int(str(self.amount.value).replace(",", "").strip())
        except ValueError:
            points = 0
        if not b or b["guild_id"] != interaction.guild_id or not is_officer(interaction):
            await interaction.response.send_message("You can't change that.", ephemeral=True)
            return
        if not points_ok(points):
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
