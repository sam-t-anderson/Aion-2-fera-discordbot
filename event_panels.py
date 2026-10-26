"""The buttons on an event card and the panels and forms they open (sign-up, organizer tools, edit form)."""

import logging
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands

import db
import errors
import util
from events import (CLASSES, COLOR_OPEN, KINDS, NAMED_KINDS, ROLES, apply_edit, can_manage, changed_text, event_title,
                    get_event, get_signup, get_signups, remember_class, remembered_class, render_event, run_autosort,
                    split_signups, time_notice, upsert_signup)

log = logging.getLogger("aion2.events")


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

    @errors.guarded
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

    @errors.guarded
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

    @errors.guarded
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

    @errors.guarded
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

    @errors.guarded
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
        await interaction.response.send_message(changed_text(changed), ephemeral=True)
        if "time" in changed:
            await interaction.followup.send(
                time_notice(get_event(ev["id"]), old_starts, interaction.channel_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if changed:
            await refresh_message(interaction.client, ev["id"])
