"""Group event planner: signups, roles, sub-parties, DM alerts, boss lists."""

import logging
import time
import zoneinfo
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
import util

log = logging.getLogger("aion2.events")

# Default total players and sub-party size per event type.
# Published: global party = 5, first raid = 10, 10v10 battlegrounds, arena formats 1v1/4v4/8v8.
# Not published (placeholders you can override per event): world boss, faction PvP, rift.
KINDS = {
    "raid": {"label": "Raid", "size": 10, "party": 5},
    "world_boss": {"label": "World Boss", "size": 10, "party": 5},
    "arena": {"label": "Arena", "size": 4, "party": 4},
    "faction_pvp": {"label": "Open World Faction PvP", "size": 10, "party": 5},
    "rift": {"label": "Cross-Territory Rift", "size": 5, "party": 5},
    "custom": {"label": "Custom Event", "size": 5, "party": 5},
}
KIND_CHOICES = [app_commands.Choice(name=v["label"], value=k) for k, v in KINDS.items()]
NAMED_KINDS = ("raid", "world_boss", "custom")  # these need a name

ROLES = {
    "tank": ("Tank", "\U0001f6e1️"),
    "healer": ("Healer", "\U0001f49a"),
    "dps": ("DPS", "⚔️"),
}
# The eight AION 2 classes, in the game's own order (the sign-up panel shows them in two rows of four).
CLASSES = {
    "gladiator": "Gladiator",
    "templar": "Templar",
    "assassin": "Assassin",
    "ranger": "Ranger",
    "sorcerer": "Sorcerer",
    "spiritmaster": "Spiritmaster",
    "cleric": "Cleric",
    "chanter": "Chanter",
}
COLOR_OPEN, COLOR_CLOSED = 0xA9741A, 0x6B7280
EMBED_BUDGET = 5900   # Discord rejects an embed over 6000 characters in total


# ---------------------------------------------------------------- data access
def get_event(event_id: int):
    return db.conn().execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()


def get_signups(event_id: int):
    return db.conn().execute(
        "SELECT * FROM signups WHERE event_id=? ORDER BY joined_at, user_id", (event_id,)
    ).fetchall()


def get_signup(event_id: int, user_id: int):
    return db.conn().execute(
        "SELECT * FROM signups WHERE event_id=? AND user_id=?", (event_id, user_id)
    ).fetchone()


def remembered_class(guild_id: int, user_id: int) -> Optional[str]:
    """The class this member last picked on an event in this server, if any."""
    row = db.conn().execute(
        "SELECT char_class FROM member_classes WHERE guild_id=? AND user_id=?", (guild_id, user_id)
    ).fetchone()
    return row["char_class"] if row and row["char_class"] in CLASSES else None


def remember_class(guild_id: int, user_id: int, char_class: Optional[str]) -> None:
    """Keep (or, with None, forget) a member's class for their next signup in this server."""
    c = db.conn()
    if char_class:
        c.execute(
            """INSERT INTO member_classes (guild_id, user_id, char_class, set_at) VALUES (?,?,?,?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET char_class=excluded.char_class, set_at=excluded.set_at""",
            (guild_id, user_id, char_class, int(time.time())),
        )
    else:
        c.execute("DELETE FROM member_classes WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    c.commit()


def upsert_signup(event_id: int, user_id: int, status: str, role: str) -> None:
    """Sign up or change status and role. A new signup starts with the class the member last picked
    in this server."""
    existing = get_signup(event_id, user_id)
    c = db.conn()
    if existing:
        joined = time.time() if existing["status"] != status else existing["joined_at"]
        c.execute(
            "UPDATE signups SET status=?, role=?, joined_at=? WHERE event_id=? AND user_id=?",
            (status, role, joined, event_id, user_id),
        )
    else:
        ev = get_event(event_id)
        char_class = remembered_class(ev["guild_id"], user_id) if ev else None
        c.execute(
            "INSERT INTO signups (event_id, user_id, status, role, char_class, joined_at) VALUES (?,?,?,?,?,?)",
            (event_id, user_id, status, role, char_class, time.time()),
        )
    c.commit()


def split_signups(ev, signups):
    """Return (confirmed, waitlist, maybe) lists in join order."""
    yes = [s for s in signups if s["status"] == "yes"]
    maybe = [s for s in signups if s["status"] == "maybe"]
    return yes[: ev["capacity"]], yes[ev["capacity"] :], maybe


# ---------------------------------------------------------------- pure logic
def autosort(players: list[dict], party_size: int) -> dict[int, int]:
    """Spread tanks, healers and DPS across even parties, balancing combat power.

    players: dicts with user_id, role, power. Returns {user_id: party_number (1-based)}.
    """
    n = len(players)
    if n == 0:
        return {}
    k = -(-n // party_size)
    parties: list[list[dict]] = [[] for _ in range(k)]

    def free(i):
        return len(parties[i]) < party_size

    def role_count(i, role):
        return sum(1 for p in parties[i] if p["role"] == role)

    def power(i):
        return sum(p["power"] or 0 for p in parties[i])

    for role in ("tank", "healer"):
        group = sorted((p for p in players if p["role"] == role), key=lambda p: -(p["power"] or 0))
        for p in group:
            i = min((i for i in range(k) if free(i)), key=lambda i: (role_count(i, role), len(parties[i]), i))
            parties[i].append(p)
    dps = sorted((p for p in players if p["role"] == "dps"), key=lambda p: -(p["power"] or 0))
    for p in dps:
        i = min((i for i in range(k) if free(i)), key=lambda i: (len(parties[i]), power(i), i))
        parties[i].append(p)

    result = {}
    for idx, members in enumerate(parties, start=1):
        for p in members:
            result[p["user_id"]] = idx
    return result


# ---------------------------------------------------------------- rendering
def event_title(ev) -> str:
    label = KINDS.get(ev["kind"], KINDS["custom"])["label"]
    if ev["kind"] == "custom" or ev["name"] == label:
        return ev["name"]
    return f"{label}: {ev['name']}"


def event_url(ev) -> Optional[str]:
    if not (ev["guild_id"] and ev["channel_id"] and ev["message_id"]):
        return None
    return f"https://discord.com/channels/{ev['guild_id']}/{ev['channel_id']}/{ev['message_id']}"


def class_label(s) -> Optional[str]:
    return CLASSES.get(s["char_class"]) if "char_class" in s.keys() and s["char_class"] else None


def class_summary(players) -> str:
    """'Templar 1 · Cleric 2 · Sorcerer 3' for the players who picked a class, in the game's class order."""
    counts = {}
    for s in players:
        label = class_label(s)
        if label:
            counts[label] = counts.get(label, 0) + 1
    return " · ".join(f"{label} {counts[label]}" for label in CLASSES.values() if label in counts)


def _player_line(s, with_party: bool = True) -> str:
    parts = [f"<@{s['user_id']}>"]
    if class_label(s):
        parts.append(class_label(s))
    if s["power"]:
        parts.append(f"{s['power']:,} CP")
    if s["gear_score"]:
        parts.append(f"{s['gear_score']:,} GS")
    if with_party and s["party"]:
        parts.append(f"P{s['party']}")
    return " · ".join(parts)


def render_event(ev, signups) -> discord.Embed:
    confirmed, waitlist, maybe = split_signups(ev, signups)
    open_ = ev["status"] == "open"
    prefix = "" if open_ else ("CANCELLED — " if ev["status"] == "cancelled" else "ENDED — ")
    embed = discord.Embed(title=prefix + event_title(ev), color=COLOR_OPEN if open_ else COLOR_CLOSED)
    lines = [
        f"**When:** {util.ts(ev['starts_at'], 'F')} ({util.ts(ev['starts_at'], 'R')})",
        f"**Organizer:** <@{ev['organizer_id']}>",
        f"**Confirmed:** {len(confirmed)}/{ev['capacity']}"
        + (f" · {len(waitlist)} on waitlist" if waitlist else ""),
    ]
    classes = class_summary(confirmed)
    if classes:
        lines.append(f"**Classes:** {classes}")
    if ev["description"]:
        lines.append("")
        lines.append(util.cut(ev["description"], 500))
    embed.description = "\n".join(lines)

    for role, (label, icon) in ROLES.items():
        members = [s for s in confirmed if s["role"] == role]
        embed.add_field(
            name=f"{icon} {label}s ({len(members)})",
            value=util.cut("\n".join(_player_line(s) for s in members) or "—"),
            inline=True,
        )
    if maybe:
        embed.add_field(
            name=f"❔ Maybe ({len(maybe)})",
            value=util.cut("\n".join(f"{ROLES[s['role']][1]} {_player_line(s)}" for s in maybe)),
            inline=False,
        )
    if waitlist:
        embed.add_field(
            name=f"⏳ Waitlist ({len(waitlist)})",
            value=util.cut("\n".join(f"{ROLES[s['role']][1]} {_player_line(s)}" for s in waitlist)),
            inline=False,
        )
    embed.set_footer(
        text=f"Event #{ev['id']} · Use Sign up / edit to pick a role and class. "
        f"Alert me sends you a DM {ev['alert_minutes']} min before."
    )
    parties: dict[int, list] = {}
    for s in confirmed:
        if s["party"]:
            parties.setdefault(s["party"], []).append(s)
    party_fields = []
    for num in sorted(parties)[:10]:
        members = sorted(parties[num], key=lambda s: list(ROLES).index(s["role"]))
        full = util.cut("\n".join(f"{ROLES[s['role']][1]} {_player_line(s, False)}" for s in members))
        short = util.cut("\n".join(
            f"{ROLES[s['role']][1]} <@{s['user_id']}>" + (f" · {class_label(s)}" if class_label(s) else "")
            for s in members))
        party_fields.append((f"Party {num} ({len(members)}/{ev['party_size']})", full, short))
    # A big sorted event can outgrow Discord's 6000-character embed limit (the card then stops updating):
    # the party lists drop combat power and gear score first (they are on the role lists above), and as a
    # last resort the lowest fields go.
    compact = len(embed) + sum(len(n) + len(v) for n, v, _ in party_fields) > EMBED_BUDGET
    for name, full, short in party_fields:
        embed.add_field(name=name, value=short if compact else full, inline=True)
    while len(embed) > EMBED_BUDGET and len(embed.fields) > len(ROLES):
        embed.remove_field(-1)
    return embed


# ---------------------------------------------------------------- views
def event_view(event_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(EvBtn("open", event_id, "Sign up / edit", discord.ButtonStyle.primary))
    view.add_item(EvBtn("leave", event_id, "Leave", discord.ButtonStyle.secondary))
    view.add_item(EvBtn("alert", event_id, "Alert me", discord.ButtonStyle.secondary))
    view.add_item(EvBtn("manage", event_id, "Organizer", discord.ButtonStyle.secondary))
    return view


def panel_class(ev, user_id: int, su=None) -> Optional[str]:
    """The class the panel shows as picked: the signup's own, or before signing up the remembered one."""
    if su:
        return su["char_class"] if su["char_class"] in CLASSES else None
    return remembered_class(ev["guild_id"], user_id)


def panel_parts(ev, user_id: int):
    su = get_signup(ev["id"], user_id)
    picked = panel_class(ev, user_id, su)
    embed = discord.Embed(title=f"Your signup — {event_title(ev)}", color=COLOR_OPEN)
    if su:
        label, icon = ROLES[su["role"]]
        state = "✅ Yes" if su["status"] == "yes" else "❔ Maybe"
        cp = f"{su['power']:,}" if su["power"] else "not set"
        gs = f"{su['gear_score']:,}" if su["gear_score"] else "not set"
        alert = f"on ({ev['alert_minutes']} min before)" if su["alert"] else "off"
        embed.description = (
            f"{state} as {icon} **{label}**\n"
            f"Class: **{CLASSES[picked] if picked else 'not set (pick one below)'}**\n"
            f"Combat power: **{cp}**\n"
            f"Gear score: **{gs}**\n"
            f"DM alert: **{alert}**\n"
            + (f"Sub-party: **{su['party']}**\n" if su["party"] else "")
        )
    else:
        embed.description = "You're not signed up yet. Pick Yes or Maybe with a role, and your class."
        if picked:
            embed.description += f"\nClass: **{CLASSES[picked]}**. Your signup will start with it."
    embed.set_footer(
        text="Your class is remembered for your next signup in this server. "
        "Alerts arrive as a direct message, so allow DMs from server members."
    )

    view = discord.ui.View(timeout=None)
    for row, status in enumerate(("yes", "maybe")):
        for role, (label, icon) in ROLES.items():
            chosen = bool(su and su["status"] == status and su["role"] == role)
            style = (
                discord.ButtonStyle.success
                if chosen and status == "yes"
                else discord.ButtonStyle.primary
                if chosen
                else discord.ButtonStyle.secondary
            )
            text = f"{'Yes' if status == 'yes' else 'Maybe'}: {label}"
            view.add_item(EvSet(ev["id"], status, role, text, style, row))
    for index, (key, label) in enumerate(CLASSES.items()):
        style = discord.ButtonStyle.success if key == picked else discord.ButtonStyle.secondary
        view.add_item(EvClass(ev["id"], key, label, style, row=2 + index // 4))
    view.add_item(EvBtn("cp", ev["id"], "Set combat power / gear score", discord.ButtonStyle.secondary, row=4))
    view.add_item(
        EvBtn(
            "alert",
            ev["id"],
            "Alert: on" if su and su["alert"] else "Alert me",
            discord.ButtonStyle.success if su and su["alert"] else discord.ButtonStyle.secondary,
            row=4,
        )
    )
    view.add_item(EvBtn("leave", ev["id"], "Leave event", discord.ButtonStyle.danger, row=4))
    return embed, view


def _is_panel(interaction: discord.Interaction) -> bool:
    msg = interaction.message
    return bool(msg and msg.flags.ephemeral)


def can_manage(interaction: discord.Interaction, ev) -> bool:
    if interaction.user.id == ev["organizer_id"]:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.manage_events or perms.administrator))


async def refresh_message(bot: commands.Bot, event_id: int) -> None:
    ev = get_event(event_id)
    if not ev or not ev["channel_id"] or not ev["message_id"]:
        return
    embed = render_event(ev, get_signups(event_id))
    view = event_view(event_id) if ev["status"] == "open" else None
    try:
        msg = bot.get_partial_messageable(ev["channel_id"]).get_partial_message(ev["message_id"])
        await msg.edit(embed=embed, view=view)
    except discord.HTTPException:
        log.warning("Could not update the message for event %s", event_id)


async def _send_or_edit_panel(interaction: discord.Interaction, ev) -> None:
    embed, view = panel_parts(ev, interaction.user.id)
    if _is_panel(interaction):
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


def _open_event(interaction: discord.Interaction, event_id: int):
    ev = get_event(event_id)
    return ev if ev and ev["status"] == "open" else None


# ---------------------------------------------------------------- button handlers
async def h_open(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev:
        await interaction.response.send_message("This event is closed.", ephemeral=True)
        return
    await _send_or_edit_panel(interaction, ev)


async def h_leave(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev:
        await interaction.response.send_message("This event is closed.", ephemeral=True)
        return
    db.conn().execute("DELETE FROM signups WHERE event_id=? AND user_id=?", (event_id, interaction.user.id))
    db.conn().commit()
    if _is_panel(interaction):
        await _send_or_edit_panel(interaction, ev)
    else:
        await interaction.response.send_message("You left the event.", ephemeral=True)
    await refresh_message(interaction.client, event_id)


async def h_alert(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev:
        await interaction.response.send_message("This event is closed.", ephemeral=True)
        return
    su = get_signup(event_id, interaction.user.id)
    if not su:
        await interaction.response.send_message(
            "Pick a role with **Sign up / edit** first, then turn on the alert.", ephemeral=True
        )
        return
    new = 0 if su["alert"] else 1
    db.conn().execute(
        "UPDATE signups SET alert=?, alerted=0 WHERE event_id=? AND user_id=?",
        (new, event_id, interaction.user.id),
    )
    db.conn().commit()
    if _is_panel(interaction):
        await _send_or_edit_panel(interaction, ev)
    else:
        text = (
            f"Alert on. You'll get a DM {ev['alert_minutes']} minutes before it starts."
            if new
            else "Alert off."
        )
        await interaction.response.send_message(text, ephemeral=True)


async def h_cp(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev:
        await interaction.response.send_message("This event is closed.", ephemeral=True)
        return
    su = get_signup(event_id, interaction.user.id)
    if not su:
        await interaction.response.send_message("Pick a role first, then set your combat power and gear score.", ephemeral=True)
        return
    await interaction.response.send_modal(CPModal(event_id, su["power"], su["gear_score"]))


async def h_manage(interaction, event_id):
    ev = get_event(event_id)
    if not ev or not can_manage(interaction, ev):
        await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
        return
    confirmed, waitlist, maybe = split_signups(ev, get_signups(event_id))
    embed = discord.Embed(
        title=f"Organizer tools — {event_title(ev)}",
        description=(
            f"{len(confirmed)} confirmed, {len(waitlist)} waitlisted, {len(maybe)} maybe.\n"
            f"Starts {util.ts(ev['starts_at'], 'F')} ({util.ts(ev['starts_at'], 'R')}).\n"
            "**Edit time / details** changes the start time, name, size or notes; a new time is "
            "announced in the channel and everyone's DM alert follows it.\n"
            f"Auto-sort splits confirmed players into parties of {ev['party_size']}, "
            "balancing tanks, healers and combat power.\n"
            "To move one person, use `/event assign`."
        ),
        color=COLOR_OPEN,
    )
    view = discord.ui.View(timeout=None)
    view.add_item(EvBtn("edit", event_id, "Edit time / details", discord.ButtonStyle.primary))
    view.add_item(EvBtn("auto", event_id, "Auto-sort into parties", discord.ButtonStyle.primary))
    view.add_item(EvBtn("clr", event_id, "Clear parties", discord.ButtonStyle.secondary))
    view.add_item(EvBtn("cancel", event_id, "Cancel event", discord.ButtonStyle.danger))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


def run_autosort(ev, party_size: Optional[int] = None) -> tuple[int, int]:
    size = party_size or ev["party_size"]
    if party_size and party_size != ev["party_size"]:
        db.conn().execute("UPDATE events SET party_size=? WHERE id=?", (size, ev["id"]))
    confirmed, _, _ = split_signups(ev, get_signups(ev["id"]))
    db.conn().execute("UPDATE signups SET party=NULL WHERE event_id=?", (ev["id"],))
    mapping = autosort(
        [{"user_id": s["user_id"], "role": s["role"], "power": s["power"]} for s in confirmed], size
    )
    for uid, party in mapping.items():
        db.conn().execute(
            "UPDATE signups SET party=? WHERE event_id=? AND user_id=?", (party, ev["id"], uid)
        )
    db.conn().commit()
    return len(mapping), (max(mapping.values()) if mapping else 0)


async def h_auto(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev or not can_manage(interaction, ev):
        await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
        return
    players, parties = run_autosort(ev)
    await interaction.response.send_message(
        f"Sorted {players} players into {parties} parties." if players else "Nobody is confirmed yet.",
        ephemeral=True,
    )
    await refresh_message(interaction.client, event_id)


async def h_clr(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev or not can_manage(interaction, ev):
        await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
        return
    db.conn().execute("UPDATE signups SET party=NULL WHERE event_id=?", (event_id,))
    db.conn().commit()
    await interaction.response.send_message("Parties cleared.", ephemeral=True)
    await refresh_message(interaction.client, event_id)


async def h_edit(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev or not can_manage(interaction, ev):
        await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
        return
    await interaction.response.send_modal(EditModal(ev, util.get_tz(interaction.user.id)))


async def h_cancel(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev or not can_manage(interaction, ev):
        await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
        return
    view = discord.ui.View(timeout=None)
    view.add_item(EvBtn("cancelok", event_id, "Yes, cancel this event", discord.ButtonStyle.danger))
    await interaction.response.send_message(
        f"Cancel **{event_title(ev)}**? Signups stay saved but the buttons are removed.", view=view, ephemeral=True
    )


async def h_cancelok(interaction, event_id):
    ev = _open_event(interaction, event_id)
    if not ev or not can_manage(interaction, ev):
        await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
        return
    db.conn().execute("UPDATE events SET status='cancelled' WHERE id=?", (event_id,))
    db.conn().commit()
    await interaction.response.edit_message(content="Event cancelled.", view=None)
    await refresh_message(interaction.client, event_id)


HANDLERS = {
    "open": h_open,
    "leave": h_leave,
    "alert": h_alert,
    "cp": h_cp,
    "manage": h_manage,
    "auto": h_auto,
    "clr": h_clr,
    "cancel": h_cancel,
    "cancelok": h_cancelok,
    "edit": h_edit,
}


class EvBtn(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ev:(?P<act>open|leave|alert|cp|manage|auto|clr|cancel|cancelok|edit):(?P<id>\d+)",
):
    def __init__(self, act, event_id, label="Event", style=discord.ButtonStyle.secondary, row=None):
        self.act, self.event_id = act, int(event_id)
        super().__init__(
            discord.ui.Button(label=label, style=style, custom_id=f"ev:{act}:{event_id}", row=row)
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"], match["id"])

    async def callback(self, interaction: discord.Interaction):
        await HANDLERS[self.act](interaction, self.event_id)


class EvSet(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ev:set:(?P<id>\d+):(?P<status>yes|maybe):(?P<role>tank|healer|dps)",
):
    def __init__(self, event_id, status, role, label="Role", style=discord.ButtonStyle.secondary, row=None):
        self.event_id, self.status, self.role = int(event_id), status, role
        super().__init__(
            discord.ui.Button(
                label=label, style=style, custom_id=f"ev:set:{event_id}:{status}:{role}", row=row
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["id"], match["status"], match["role"])

    async def callback(self, interaction: discord.Interaction):
        ev = _open_event(interaction, self.event_id)
        if not ev:
            await interaction.response.send_message("This event is closed.", ephemeral=True)
            return
        upsert_signup(self.event_id, interaction.user.id, self.status, self.role)
        await _send_or_edit_panel(interaction, ev)
        await refresh_message(interaction.client, self.event_id)


class EvClass(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ev:cls:(?P<id>\d+):(?P<cls>" + "|".join(CLASSES) + ")",
):
    """A class button on the sign-up panel. It sets the signup's class and remembers it for the member's
    next signup in this server; pressing the picked class again clears it. Before signing up it only
    remembers it, so the role button pressed next signs up with that class."""

    def __init__(self, event_id, char_class, label="Class", style=discord.ButtonStyle.secondary, row=None):
        self.event_id, self.char_class = int(event_id), char_class
        super().__init__(
            discord.ui.Button(label=label, style=style, custom_id=f"ev:cls:{event_id}:{char_class}", row=row)
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["id"], match["cls"])

    async def callback(self, interaction: discord.Interaction):
        ev = _open_event(interaction, self.event_id)
        if not ev:
            await interaction.response.send_message("This event is closed.", ephemeral=True)
            return
        su = get_signup(self.event_id, interaction.user.id)
        chosen = None if panel_class(ev, interaction.user.id, su) == self.char_class else self.char_class
        remember_class(ev["guild_id"], interaction.user.id, chosen)
        if su:
            db.conn().execute(
                "UPDATE signups SET char_class=? WHERE event_id=? AND user_id=?",
                (chosen, self.event_id, interaction.user.id),
            )
            db.conn().commit()
        await _send_or_edit_panel(interaction, ev)
        if su:
            await refresh_message(interaction.client, self.event_id)


def parse_stat(text: str, limit: int) -> Optional[int]:
    """A typed number like 12500, 12,500 or 12.5k. None when left empty; ValueError when it isn't a sensible number."""
    raw = text.strip().lower().replace(",", "").replace(" ", "")
    if not raw:
        return None
    mult = 1
    if raw.endswith("k"):
        mult, raw = 1000, raw[:-1]
    value = int(float(raw) * mult)   # float() raises ValueError on anything that isn't a number
    if not 0 < value <= limit:
        raise ValueError("out of range")
    return value


class CPModal(discord.ui.Modal, title="Combat power and gear score"):
    def __init__(self, event_id: int, current, gear_score=None):
        super().__init__(timeout=300)
        self.event_id = event_id
        self.cp = discord.ui.TextInput(
            label="Your combat power (leave empty to clear)",
            required=False,
            max_length=10,
            default=str(current) if current else None,
            placeholder="For example 12500",
        )
        self.gs = discord.ui.TextInput(
            label="Your gear score (leave empty to clear)",
            required=False,
            max_length=7,
            default=str(gear_score) if gear_score else None,
            placeholder="For example 2100",
        )
        self.add_item(self.cp)
        self.add_item(self.gs)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            power = parse_stat(self.cp.value, 99_999_999)
            gear_score = parse_stat(self.gs.value, 99_999)
        except (ValueError, OverflowError):
            await interaction.response.send_message(
                "Enter plain numbers, for example 12500 or 12.5k for combat power and 2100 for gear score.", ephemeral=True
            )
            return
        db.conn().execute(
            "UPDATE signups SET power=?, gear_score=? WHERE event_id=? AND user_id=?",
            (power, gear_score, self.event_id, interaction.user.id),
        )
        db.conn().commit()
        ev = get_event(self.event_id)
        if ev:
            await _send_or_edit_panel(interaction, ev)
            await refresh_message(interaction.client, self.event_id)


def apply_edit(ev, starts_at: Optional[int] = None, capacity: Optional[int] = None,
               name: Optional[str] = None, description: Optional[str] = None) -> list[str]:
    """Change an event; returns what changed ('time', 'size', 'name', 'notes'). A new time re-arms every
    signup's DM alert for it. Values equal to the current ones are not counted as changes."""
    c = db.conn()
    changed = []
    if starts_at is not None and starts_at != ev["starts_at"]:
        c.execute("UPDATE events SET starts_at=? WHERE id=?", (starts_at, ev["id"]))
        c.execute("UPDATE signups SET alerted=0 WHERE event_id=?", (ev["id"],))
        changed.append("time")
    if capacity is not None and capacity != ev["capacity"]:
        c.execute("UPDATE events SET capacity=? WHERE id=?", (capacity, ev["id"]))
        changed.append("size")
    if name is not None and name[:80] != ev["name"]:
        c.execute("UPDATE events SET name=? WHERE id=?", (name[:80], ev["id"]))
        changed.append("name")
    if description is not None and description[:500] != (ev["description"] or ""):
        c.execute("UPDATE events SET description=? WHERE id=?", (description[:500], ev["id"]))
        changed.append("notes")
    c.commit()
    return changed


def time_notice(ev, old_starts: int, here_channel_id: Optional[int] = None) -> str:
    """The channel message announcing a new start time (ev is the event as it is now)."""
    text = (
        f"\u23f0 **{event_title(ev)}** has a new start time: {util.ts(ev['starts_at'], 'F')} "
        f"({util.ts(ev['starts_at'], 'R')}). It was {util.ts(old_starts, 'F')}."
    )
    url = event_url(ev)
    if url and here_channel_id != ev["channel_id"]:
        text += f"\n{url}"
    return text


def _changed_text(changed: list[str]) -> str:
    return "Event updated: " + ", ".join(changed) + "." if changed else "Nothing changed."


class EditModal(discord.ui.Modal, title="Edit event"):
    """The organizer's edit form, opened from the Organizer panel: start time, name, size and notes,
    filled in with the current values. The time is shown and read in the organizer's own timezone."""

    def __init__(self, ev, tz):
        super().__init__(timeout=900)
        self.event_id, self.tz = ev["id"], tz
        self.when_default = datetime.fromtimestamp(ev["starts_at"], tz).strftime("%Y-%m-%d %H:%M")
        tz_name = getattr(tz, "key", None) or str(tz)
        self.when_input = discord.ui.TextInput(
            label=util.cut(f"Start time ({tz_name})", 45),
            default=self.when_default,
            max_length=40,
            placeholder="tomorrow 8pm, 10/26 7:30pm or in 2h",
        )
        self.name_input = discord.ui.TextInput(
            label="Name", default=ev["name"], max_length=80, required=ev["kind"] in NAMED_KINDS
        )
        self.size_input = discord.ui.TextInput(
            label="Total players (2 to 60)", default=str(ev["capacity"]), max_length=2
        )
        self.notes_input = discord.ui.TextInput(
            label="Notes (leave empty for none)",
            style=discord.TextStyle.paragraph,
            default=ev["description"] or None,
            required=False,
            max_length=500,
        )
        for item in (self.when_input, self.name_input, self.size_input, self.notes_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        ev = get_event(self.event_id)
        if not ev or ev["status"] != "open":
            await interaction.response.send_message("This event is closed.", ephemeral=True)
            return
        if not can_manage(interaction, ev):
            await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
            return
        problems = []
        starts = None
        when_text = (self.when_input.value or "").strip()
        if when_text and when_text != self.when_default:   # an untouched time is kept as it is
            try:
                starts = int(util.parse_when(when_text, self.tz).timestamp())
            except ValueError as e:
                problems.append(str(e))
        try:
            size = int((self.size_input.value or "").strip())
            if not 2 <= size <= 60:
                raise ValueError
        except ValueError:
            size = None
            problems.append("Total players must be a number from 2 to 60.")
        name = " ".join((self.name_input.value or "").split())
        if not name:
            if ev["kind"] in NAMED_KINDS:
                problems.append("This event needs a name.")
            name = KINDS.get(ev["kind"], KINDS["custom"])["label"]
        if name == " ".join(ev["name"].split()):
            name = None                                   # untouched
        notes = self.notes_input.value or ""
        if not notes.strip():
            notes = ""
        if notes.strip() == (ev["description"] or "").strip():
            notes = None                                  # untouched
        if problems:
            await interaction.response.send_message(
                "Nothing was changed.\n" + "\n".join(f"- {p}" for p in problems), ephemeral=True
            )
            return
        old_starts = ev["starts_at"]
        changed = apply_edit(ev, starts_at=starts, capacity=size, name=name, description=notes)
        await interaction.response.send_message(_changed_text(changed), ephemeral=True)
        if "time" in changed:
            await interaction.followup.send(
                time_notice(get_event(ev["id"]), old_starts, interaction.channel_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if changed:
            await refresh_message(interaction.client, ev["id"])


# ---------------------------------------------------------------- slash commands
async def _event_choices(interaction: discord.Interaction, current: str):
    if not interaction.guild_id:
        return []
    rows = db.conn().execute(
        """SELECT * FROM events WHERE guild_id=? AND status='open' AND starts_at > ?
           ORDER BY starts_at LIMIT 40""",
        (interaction.guild_id, int(time.time()) - 3 * 3600),
    ).fetchall()
    tz = util.get_tz(interaction.user.id)
    out = []
    for ev in rows:
        when = datetime.fromtimestamp(ev["starts_at"], tz).strftime("%a %b %d %I:%M %p")
        label = f"#{ev['id']} {event_title(ev)} · {when}"
        if current.lower() in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=str(ev["id"])))
    return out[:25]


async def _resolve_event(interaction: discord.Interaction, arg: str):
    """Find an open event in this guild from the autocomplete value. Replies on failure."""
    if not interaction.guild_id:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return None
    try:
        ev = get_event(int(str(arg).lstrip("#").split()[0]))
    except (ValueError, IndexError):
        ev = None
    if not ev or ev["guild_id"] != interaction.guild_id:
        await interaction.response.send_message("Pick an event from the list that appears as you type.", ephemeral=True)
        return None
    return ev


class Events(commands.GroupCog, group_name="event", group_description="Plan group events"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def cog_load(self):
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    # ---- background: DM alerts and cleanup
    @tasks.loop(seconds=30)
    async def tick(self):
        try:
            now = int(time.time())
            c = db.conn()
            due = c.execute(
                """SELECT s.event_id, s.user_id, e.*
                   FROM signups s JOIN events e ON e.id = s.event_id
                   WHERE e.status='open' AND s.alert=1 AND s.alerted=0
                     AND e.starts_at - e.alert_minutes*60 <= ? AND e.starts_at + 300 > ?""",
                (now, now),
            ).fetchall()
            for row in due:
                c.execute(
                    "UPDATE signups SET alerted=1 WHERE event_id=? AND user_id=?", (row["event_id"], row["user_id"])
                )
                c.commit()
                url = event_url(row)
                text = f"**{event_title(row)}** starts {util.ts(row['starts_at'], 'R')} ({util.ts(row['starts_at'], 't')})."
                if url:
                    text += f"\n{url}"
                try:
                    user = self.bot.get_user(row["user_id"]) or await self.bot.fetch_user(row["user_id"])
                    await user.send(text)
                except discord.HTTPException:
                    log.info("Could not DM user %s (DMs closed?)", row["user_id"])

            ended = c.execute(
                "SELECT id FROM events WHERE status='open' AND starts_at < ?", (now - 3 * 3600,)
            ).fetchall()
            for row in ended:
                c.execute("UPDATE events SET status='done' WHERE id=?", (row["id"],))
                c.commit()
                await refresh_message(self.bot, row["id"])
            old = c.execute("SELECT id FROM events WHERE starts_at < ?", (now - 7 * 86400,)).fetchall()
            for row in old:
                c.execute("DELETE FROM signups WHERE event_id=?", (row["id"],))
                c.execute("DELETE FROM events WHERE id=?", (row["id"],))
            if old:
                c.commit()
        except Exception:
            log.exception("Event tick error")

    @tick.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ---- /event create
    @app_commands.command(name="create", description="Plan a group event with signups")
    @app_commands.describe(
        kind="What kind of event",
        when="8pm, tomorrow 7:30pm, 10/4 8pm, 2026-10-04 20:00, in 2h",
        name="Boss or raid name (pick from the list) or your custom event name",
        size="Total players wanted. Leave empty for the default for this event type",
        party_size="Players per sub-party. Default is 5 (4 for arena)",
        description="Notes for the group",
        alert_minutes="Minutes before start to DM people who chose Alert me",
    )
    @app_commands.choices(kind=KIND_CHOICES)
    async def create(
        self,
        interaction: discord.Interaction,
        kind: str,
        when: str,
        name: Optional[str] = None,
        size: Optional[app_commands.Range[int, 2, 60]] = None,
        party_size: Optional[app_commands.Range[int, 2, 10]] = None,
        description: Optional[str] = None,
        alert_minutes: Optional[app_commands.Range[int, 0, 120]] = None,
    ):
        if not interaction.guild_id:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        meta = KINDS[kind]
        name = (name or "").strip()
        if kind in NAMED_KINDS and not name:
            await interaction.response.send_message(
                f"Give the event a `name` (for a {meta['label'].lower()}, pick one from the list).", ephemeral=True
            )
            return
        try:
            starts = util.parse_when(when, util.get_tz(interaction.user.id))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        name = (name or meta["label"])[:80]
        cur = db.conn().execute(
            """INSERT INTO events (guild_id, channel_id, organizer_id, kind, name, description,
                                   starts_at, capacity, party_size, alert_minutes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                kind,
                name,
                (description or "")[:500],
                int(starts.timestamp()),
                size or meta["size"],
                party_size or meta["party"],
                config.EVENT_ALERT_MINUTES if alert_minutes is None else alert_minutes,
                int(time.time()),
            ),
        )
        db.conn().commit()
        event_id = cur.lastrowid
        ev = get_event(event_id)
        await interaction.response.send_message(embed=render_event(ev, []), view=event_view(event_id))
        msg = await interaction.original_response()
        db.conn().execute("UPDATE events SET message_id=? WHERE id=?", (msg.id, event_id))
        db.conn().commit()

    @create.autocomplete("name")
    async def _name_ac(self, interaction: discord.Interaction, current: str):
        kind = getattr(interaction.namespace, "kind", None)
        if kind not in ("raid", "world_boss"):
            return []
        if not interaction.guild_id:
            return []
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in db.bosses(interaction.guild_id, kind, current)]

    # ---- /event list
    @app_commands.command(name="list", description="Show upcoming events in this server")
    async def list_events(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        rows = db.conn().execute(
            """SELECT * FROM events WHERE guild_id=? AND status='open' AND starts_at > ?
               ORDER BY starts_at LIMIT 15""",
            (interaction.guild_id, int(time.time()) - 3600),
        ).fetchall()
        if not rows:
            await interaction.response.send_message("No upcoming events. Start one with `/event create`.", ephemeral=True)
            return
        lines = []
        for ev in rows:
            confirmed, _, _ = split_signups(ev, get_signups(ev["id"]))
            url = event_url(ev)
            title = f"[{event_title(ev)}]({url})" if url else event_title(ev)
            lines.append(f"{title} · {util.ts(ev['starts_at'], 'F')} · {len(confirmed)}/{ev['capacity']}")
        await interaction.response.send_message(
            embed=discord.Embed(title="Upcoming events", description="\n".join(lines), color=COLOR_OPEN),
            ephemeral=True,
        )

    # ---- /event cancel
    @app_commands.command(name="cancel", description="Cancel an event you organize")
    @app_commands.describe(event="The event to cancel")
    async def cancel(self, interaction: discord.Interaction, event: str):
        ev = await _resolve_event(interaction, event)
        if not ev:
            return
        if not can_manage(interaction, ev):
            await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
            return
        db.conn().execute("UPDATE events SET status='cancelled' WHERE id=?", (ev["id"],))
        db.conn().commit()
        await interaction.response.send_message(f"Cancelled **{event_title(ev)}**.", ephemeral=True)
        await refresh_message(self.bot, ev["id"])

    # ---- /event edit
    @app_commands.command(name="edit", description="Change an event's time, size, name or notes")
    @app_commands.describe(
        event="The event to change",
        when="New time, for example tomorrow 8pm",
        size="New total player count",
        name="New name",
        description="New notes",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        event: str,
        when: Optional[str] = None,
        size: Optional[app_commands.Range[int, 2, 60]] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        ev = await _resolve_event(interaction, event)
        if not ev:
            return
        if not can_manage(interaction, ev):
            await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
            return
        starts = None
        if when:
            try:
                starts = int(util.parse_when(when, util.get_tz(interaction.user.id)).timestamp())
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
        old_starts = ev["starts_at"]
        changed = apply_edit(ev, starts_at=starts, capacity=size, name=(name or "").strip() or None,
                             description=description)
        await interaction.response.send_message(_changed_text(changed), ephemeral=True)
        if "time" in changed:
            await interaction.followup.send(
                time_notice(get_event(ev["id"]), old_starts, interaction.channel_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if changed:
            await refresh_message(self.bot, ev["id"])

    # ---- /event assign
    @app_commands.command(name="assign", description="Put a signed-up player into a sub-party")
    @app_commands.describe(event="The event", member="The player", party="Sub-party number (0 removes them from parties)")
    async def assign(
        self,
        interaction: discord.Interaction,
        event: str,
        member: discord.Member,
        party: app_commands.Range[int, 0, 10],
    ):
        ev = await _resolve_event(interaction, event)
        if not ev:
            return
        if not can_manage(interaction, ev):
            await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
            return
        if not get_signup(ev["id"], member.id):
            await interaction.response.send_message(f"{member.display_name} hasn't signed up for this event.", ephemeral=True)
            return
        db.conn().execute(
            "UPDATE signups SET party=? WHERE event_id=? AND user_id=?", (party or None, ev["id"], member.id)
        )
        db.conn().commit()
        full = db.conn().execute(
            "SELECT COUNT(*) AS n FROM signups WHERE event_id=? AND party=?", (ev["id"], party)
        ).fetchone()["n"]
        note = f" Party {party} now has {full}, over the size of {ev['party_size']}." if party and full > ev["party_size"] else ""
        await interaction.response.send_message(
            (f"Moved {member.display_name} to party {party}." if party else f"Removed {member.display_name} from parties.") + note,
            ephemeral=True,
        )
        await refresh_message(self.bot, ev["id"])

    # ---- /event autosort
    @app_commands.command(name="autosort", description="Auto-split confirmed players into balanced sub-parties")
    @app_commands.describe(event="The event", party_size="Players per party (default is the event's setting)")
    async def autosort_cmd(
        self,
        interaction: discord.Interaction,
        event: str,
        party_size: Optional[app_commands.Range[int, 2, 10]] = None,
    ):
        ev = await _resolve_event(interaction, event)
        if not ev:
            return
        if not can_manage(interaction, ev):
            await interaction.response.send_message("Only the organizer or a server manager can do that.", ephemeral=True)
            return
        players, parties = run_autosort(ev, party_size)
        await interaction.response.send_message(
            f"Sorted {players} players into {parties} parties." if players else "Nobody is confirmed yet.",
            ephemeral=True,
        )
        await refresh_message(self.bot, ev["id"])

    # ---- /event alert
    @app_commands.command(name="alert", description="Get a private DM when an event is about to start")
    @app_commands.describe(event="The event", enabled="Turn the alert on or off")
    async def alert(self, interaction: discord.Interaction, event: str, enabled: bool = True):
        ev = await _resolve_event(interaction, event)
        if not ev:
            return
        if not get_signup(ev["id"], interaction.user.id):
            await interaction.response.send_message(
                "Sign up first with the buttons on the event, then turn on the alert.", ephemeral=True
            )
            return
        db.conn().execute(
            "UPDATE signups SET alert=?, alerted=0 WHERE event_id=? AND user_id=?",
            (1 if enabled else 0, ev["id"], interaction.user.id),
        )
        db.conn().commit()
        await interaction.response.send_message(
            f"Alert on. You'll get a DM {ev['alert_minutes']} minutes before **{event_title(ev)}** starts. "
            "Make sure server members are allowed to DM you."
            if enabled
            else "Alert off.",
            ephemeral=True,
        )

    @cancel.autocomplete("event")
    async def _ac_cancel(self, interaction, current):
        return await _event_choices(interaction, current)

    @edit.autocomplete("event")
    async def _ac_edit(self, interaction, current):
        return await _event_choices(interaction, current)

    @assign.autocomplete("event")
    async def _ac_assign(self, interaction, current):
        return await _event_choices(interaction, current)

    @autosort_cmd.autocomplete("event")
    async def _ac_auto(self, interaction, current):
        return await _event_choices(interaction, current)

    @alert.autocomplete("event")
    async def _ac_alert(self, interaction, current):
        return await _event_choices(interaction, current)


# ---------------------------------------------------------------- timezone
COMMON_TZ = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "Europe/London",
    "Europe/Berlin", "Asia/Tokyo", "Australia/Sydney", "UTC",
]


class TimezoneCog(commands.Cog):
    group = app_commands.Group(name="timezone", description="Your timezone for event times")

    @group.command(name="set", description="Set the timezone used when you type event times")
    @app_commands.describe(name="For example America/Denver")
    async def tz_set(self, interaction: discord.Interaction, name: str):
        try:
            zoneinfo.ZoneInfo(name)
        except Exception:
            await interaction.response.send_message(
                "I don't know that timezone. Start typing and pick one from the list, for example `America/Denver`.",
                ephemeral=True,
            )
            return
        db.conn().execute(
            "INSERT INTO user_prefs (user_id, tz) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz",
            (interaction.user.id, name),
        )
        db.conn().commit()
        await interaction.response.send_message(f"Your timezone is now **{name}**.", ephemeral=True)

    @tz_set.autocomplete("name")
    async def _tz_ac(self, interaction: discord.Interaction, current: str):
        pool = COMMON_TZ if not current else sorted(zoneinfo.available_timezones())
        hits = [z for z in pool if current.lower() in z.lower()][:25]
        return [app_commands.Choice(name=z, value=z) for z in hits]

    @group.command(name="show", description="Show the timezone used for your event times")
    async def tz_show(self, interaction: discord.Interaction):
        tz = util.get_tz(interaction.user.id)
        now = datetime.now(timezone.utc).astimezone(tz)
        await interaction.response.send_message(
            f"Your timezone is **{tz.key}**. It is currently {now.strftime('%a %b %d, %I:%M %p')} there.",
            ephemeral=True,
        )


# ---------------------------------------------------------------- boss list admin
class BossAdmin(commands.Cog):
    group = app_commands.Group(
        name="eventlist",
        description="Admin: manage the raid and world boss lists",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )
    KIND = [
        app_commands.Choice(name="Raid", value="raid"),
        app_commands.Choice(name="World boss", value="world_boss"),
    ]

    @group.command(name="add", description="Add a raid or world boss to the list")
    @app_commands.choices(kind=KIND)
    @app_commands.describe(kind="Raid or world boss", name="Name to add")
    async def add(self, interaction: discord.Interaction, kind: str, name: str):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        name = name.strip()[:100]
        if not name:
            await interaction.response.send_message("Give me a name.", ephemeral=True)
            return
        db.ensure_guild(interaction.guild_id)
        db.conn().execute("INSERT OR IGNORE INTO guild_bosses (guild_id, kind, name) VALUES (?, ?, ?)", (interaction.guild_id, kind, name))
        db.conn().commit()
        await interaction.response.send_message(f"Added **{name}** to the {kind.replace('_', ' ')} list.", ephemeral=True)

    @group.command(name="remove", description="Remove a raid or world boss from the list")
    @app_commands.choices(kind=KIND)
    @app_commands.describe(kind="Raid or world boss", name="Name to remove")
    async def remove(self, interaction: discord.Interaction, kind: str, name: str):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        cur = db.conn().execute("DELETE FROM guild_bosses WHERE guild_id=? AND kind=? AND name=?", (interaction.guild_id, kind, name))
        db.conn().commit()
        await interaction.response.send_message(
            f"Removed **{name}**." if cur.rowcount else "That name isn't on the list.", ephemeral=True
        )

    @remove.autocomplete("name")
    async def _remove_ac(self, interaction: discord.Interaction, current: str):
        kind = getattr(interaction.namespace, "kind", None)
        return [
            app_commands.Choice(name=n[:100], value=n[:100])
            for n in db.bosses(interaction.guild_id, kind, current)
        ]

    @group.command(name="show", description="Show the raid and world boss lists")
    async def show(self, interaction: discord.Interaction):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        embed = discord.Embed(title="Event lists", color=COLOR_OPEN)
        for kind, label in (("raid", "Raids"), ("world_boss", "World bosses")):
            db.ensure_guild(interaction.guild_id)
            rows = db.conn().execute(
                "SELECT name FROM guild_bosses WHERE guild_id=? AND kind=? ORDER BY name", (interaction.guild_id, kind)
            ).fetchall()
            embed.add_field(name=label, value=util.cut("\n".join(r["name"] for r in rows) or "—"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(EvBtn, EvSet, EvClass)
    await bot.add_cog(Events(bot))
    await bot.add_cog(TimezoneCog())
    await bot.add_cog(BossAdmin())
