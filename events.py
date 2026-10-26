"""Group event planner: events and signups, sub-parties, and the event card.

The card's buttons and forms are in event_panels.py; /event, /timezone and /eventlist in event_commands.py."""

import logging
import time
from typing import Optional

import discord
from discord import app_commands

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


# ---------------------------------------------------------------- changes (from the card's buttons and from /event)
def can_manage(interaction: discord.Interaction, ev) -> bool:
    if interaction.user.id == ev["organizer_id"]:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.manage_events or perms.administrator))


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


def changed_text(changed: list[str]) -> str:
    return "Event updated: " + ", ".join(changed) + "." if changed else "Nothing changed."
