"""/event, /timezone and /eventlist, and the loop that sends alerts and closes past events."""

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
import heartbeat
import util
from event_panels import EvBtn, EvClass, EvSet, event_view, refresh_message
from events import (COLOR_OPEN, KIND_CHOICES, KINDS, NAMED_KINDS, apply_edit, can_manage, changed_text, event_title,
                    event_url, get_event, get_signup, get_signups, render_event, run_autosort, split_signups,
                    time_notice)

log = logging.getLogger("aion2.events")


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
            heartbeat.beat("events.tick", self.tick)
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
        await interaction.response.send_message(changed_text(changed), ephemeral=True)
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
