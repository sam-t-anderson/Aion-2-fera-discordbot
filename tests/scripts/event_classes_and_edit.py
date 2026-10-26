"""Event signups: class buttons on the Sign up / edit panel (remembered per server), and the organizer's
Edit time / details form on the Organizer panel (and /event edit) announcing a new start time."""
import asyncio, os, sqlite3, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock
from zoneinfo import ZoneInfo
p = os.path.join(tempfile.mkdtemp(), "old.db")
os.environ["DB_PATH"] = p
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

# a database from before classes existed, with a signup in it
o = sqlite3.connect(p)
o.executescript("""
CREATE TABLE signups (event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL, role TEXT NOT NULL,
    power INTEGER, gear_score INTEGER, party INTEGER, alert INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0,
    joined_at REAL NOT NULL, PRIMARY KEY (event_id, user_id));
INSERT INTO signups (event_id, user_id, status, role, power, joined_at) VALUES (1, 7, 'yes', 'tank', 12500, 1);
""")
o.commit(); o.close()
import discord
import db, events, event_commands, event_panels

ORG, G, OTHER_G = 99, 1, 2
NOW = int(time.time())


def interaction(uid, guild=G, manager=False, channel=2):
    it = MagicMock()
    it.user.id = uid
    it.user.guild_permissions = SimpleNamespace(manage_guild=manager, manage_events=False, administrator=False)
    it.guild_id, it.channel_id, it.message = guild, channel, None
    it.response.send_message = AsyncMock(); it.response.edit_message = AsyncMock(); it.response.send_modal = AsyncMock()
    it.followup.send = AsyncMock()
    it.client = MagicMock()
    it.client.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
    return it


def labels(view):
    return [c.item.label for c in view.children]


def styles(view):
    return {c.item.label: c.item.style for c in view.children}


async def press_class(event_id, uid, char_class, guild=G):
    it = interaction(uid, guild)
    await event_panels.EvClass(event_id, char_class).callback(it)
    return it


async def press_role(event_id, uid, status, role, guild=G):
    it = interaction(uid, guild)
    await event_panels.EvSet(event_id, status, role).callback(it)
    return it


def new_event(kind="raid", name="Eternal Ludra", guild=G, starts=None, channel=2, message=3, description=""):
    cur = db.conn().execute(
        """INSERT INTO events (guild_id,channel_id,message_id,organizer_id,kind,name,description,starts_at,capacity,party_size,created_at)
           VALUES (?,?,?,?,?,?,?,?,10,5,?)""",
        (guild, channel, message, ORG, kind, name, description, starts or NOW + 3600, NOW))
    db.conn().commit()
    return cur.lastrowid


async def main():
    db.init()
    c = db.conn()
    assert "char_class" in [r[1] for r in c.execute("PRAGMA table_info(signups)")]
    assert c.execute("SELECT name FROM sqlite_master WHERE name='member_classes'").fetchone()
    ev1 = new_event()                                                     # event 1: the old signup's event
    old = events.get_signup(ev1, 7)
    assert old["char_class"] is None and events._player_line(old) == "<@7> · 12,500 CP"   # survived the upgrade

    # the buttons' custom ids
    pat = event_panels.EvClass.__discord_ui_compiled_template__
    assert pat.fullmatch("ev:cls:12:spiritmaster") and pat.fullmatch("ev:cls:1:gladiator")
    assert not pat.fullmatch("ev:cls:12:elementalist") and not pat.fullmatch("ev:set:1:yes:tank")
    assert event_panels.EvBtn.__discord_ui_compiled_template__.fullmatch("ev:edit:4")
    assert event_panels.EvBtn.__discord_ui_compiled_template__.fullmatch("ev:open:4")          # old cards keep working

    # the panel: Yes/Maybe roles, then the eight classes in two rows of four, then the tools
    ev = events.get_event(ev1)
    embed, view = event_panels.panel_parts(ev, 7)
    names = labels(view)
    assert names[6:14] == ["Gladiator", "Templar", "Assassin", "Ranger", "Sorcerer", "Spiritmaster", "Cleric", "Chanter"], names
    assert [c.item.row for c in view.children][6:14] == [2, 2, 2, 2, 3, 3, 3, 3]
    assert {c.item.row for c in view.children} == {0, 1, 2, 3, 4} and len(view.children) == 17
    assert "Class: **not set (pick one below)**" in embed.description
    assert all(s == discord.ButtonStyle.secondary for n, s in styles(view).items() if n in events.CLASSES.values())

    # picking a class while signed up: on the signup, on the card, remembered for next time
    it = await press_class(ev1, 7, "templar")
    assert events.get_signup(ev1, 7)["char_class"] == "templar" and events.remembered_class(G, 7) == "templar"
    sent_embed = it.response.send_message.await_args.kwargs["embed"]
    assert "Class: **Templar**" in sent_embed.description
    assert styles(it.response.send_message.await_args.kwargs["view"])["Templar"] == discord.ButtonStyle.success
    it.client.get_partial_messageable.return_value.get_partial_message.return_value.edit.assert_awaited()   # the card updated
    card = events.render_event(events.get_event(ev1), events.get_signups(ev1)).to_dict()
    assert "<@7> · Templar · 12,500 CP" in str(card) and "**Classes:** Templar 1" in card["description"]

    # a different class replaces it; pressing the picked one again clears it (and the memory)
    await press_class(ev1, 7, "gladiator")
    assert events.get_signup(ev1, 7)["char_class"] == "gladiator" and events.remembered_class(G, 7) == "gladiator"
    await press_class(ev1, 7, "gladiator")
    assert events.get_signup(ev1, 7)["char_class"] is None and events.remembered_class(G, 7) is None
    assert "Classes:" not in events.render_event(events.get_event(ev1), events.get_signups(ev1)).to_dict()["description"]

    # class first, then role: the panel shows it before signing up, and the signup starts with it
    it = await press_class(ev1, 8, "cleric")
    assert events.get_signup(ev1, 8) is None and events.remembered_class(G, 8) == "cleric"
    assert "Class: **Cleric**. Your signup will start with it." in it.response.send_message.await_args.kwargs["embed"].description
    it.client.get_partial_messageable.return_value.get_partial_message.return_value.edit.assert_not_awaited()   # no card change yet
    await press_role(ev1, 8, "yes", "healer")
    su = events.get_signup(ev1, 8)
    assert (su["status"], su["role"], su["char_class"]) == ("yes", "healer", "cleric")
    # changing role or status keeps the class
    events.upsert_signup(ev1, 8, "maybe", "dps"); assert events.get_signup(ev1, 8)["char_class"] == "cleric"
    events.upsert_signup(ev1, 8, "yes", "healer")

    # the next event in this server starts with the remembered class; another server does not
    ev2 = new_event(name="Fusion Reactor")
    await press_role(ev2, 8, "yes", "healer")
    assert events.get_signup(ev2, 8)["char_class"] == "cleric"
    ev3 = new_event(guild=OTHER_G)
    await press_role(ev3, 8, "yes", "healer", guild=OTHER_G)
    assert events.get_signup(ev3, 8)["char_class"] is None
    # changing the class on one event leaves the other event's signup alone
    await press_class(ev2, 8, "chanter")
    assert events.get_signup(ev2, 8)["char_class"] == "chanter" and events.get_signup(ev1, 8)["char_class"] == "cleric"
    assert events.remembered_class(G, 8) == "chanter"

    # the card's class line counts confirmed players only, in the game's class order
    for uid, cls in [(20, "sorcerer"), (21, "sorcerer"), (22, "templar")]:
        await press_class(ev1, uid, cls); await press_role(ev1, uid, "yes", "dps")
    await press_class(ev1, 23, "ranger"); await press_role(ev1, 23, "maybe", "dps")
    desc = events.render_event(events.get_event(ev1), events.get_signups(ev1)).to_dict()["description"]
    assert "**Classes:** Templar 1 · Sorcerer 2 · Cleric 1" in desc, desc

    # a full 60-player raid sorted into parties still fits Discord's 6000-character embed limit
    big = new_event(name="Eternal Ludra")
    c.execute("UPDATE events SET capacity=60 WHERE id=?", (big,))
    roles = ["tank"] * 6 + ["healer"] * 12 + ["dps"] * 42
    for i, role in enumerate(roles):
        uid = 900000000000000000 + i
        c.execute("INSERT INTO signups (event_id,user_id,status,role,power,gear_score,char_class,joined_at) VALUES (?,?,'yes',?,?,?,?,?)",
                  (big, uid, role, 12_345_678, 2_150, "spiritmaster", NOW + i))
    c.commit()
    events.run_autosort(events.get_event(big))
    card = events.render_event(events.get_event(big), events.get_signups(big))
    assert len(card) <= 6000, len(card)
    party_values = [f["value"] for f in card.to_dict()["fields"] if f["name"].startswith("Party ")]
    assert party_values and "Spiritmaster" in party_values[0] and "CP" not in party_values[0]   # compact party lists

    # a closed event refuses class changes
    c.execute("UPDATE events SET status='cancelled' WHERE id=?", (ev2,)); c.commit()
    it = await press_class(ev2, 8, "cleric")
    assert it.response.send_message.await_args.args[0] == "This event is closed."
    assert events.get_signup(ev2, 8)["char_class"] == "chanter"

    # leaving the server forgets the remembered classes there
    db.forget_guild(G)
    assert events.remembered_class(G, 8) is None
    db.ensure_guild(G)

    # ---- the organizer's Edit time / details
    c.execute("INSERT OR REPLACE INTO user_prefs (user_id, tz) VALUES (?, 'Europe/Berlin')", (ORG,)); c.commit()
    tz = ZoneInfo("Europe/Berlin")
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(second=0, microsecond=0)
    ev4 = new_event(kind="raid", name="Eternal Ludra", starts=int(start.timestamp()), description="Bring potions")
    c.execute("INSERT INTO signups (event_id,user_id,status,role,alert,alerted,joined_at) VALUES (?,7,'yes','dps',1,1,?)", (ev4, NOW)); c.commit()

    it = interaction(ORG)
    await event_panels.h_manage(it, ev4)
    org_view = it.response.send_message.await_args.kwargs["view"]
    assert labels(org_view)[0] == "Edit time / details" and "Starts <t:" in it.response.send_message.await_args.kwargs["embed"].description

    stranger = interaction(55)
    await event_panels.h_edit(stranger, ev4)
    stranger.response.send_modal.assert_not_awaited()
    assert "Only the organizer" in stranger.response.send_message.await_args.args[0]

    it = interaction(ORG)
    await event_panels.h_edit(it, ev4)
    modal = it.response.send_modal.await_args.args[0]
    assert isinstance(modal, event_panels.EditModal) and len(modal.title) <= 45
    assert modal.when_input.default == start.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    assert modal.when_input.label == "Start time (Europe/Berlin)" and all(len(i.label) <= 45 for i in modal.children)
    assert (modal.name_input.default, modal.size_input.default, modal.notes_input.default) == ("Eternal Ludra", "10", "Bring potions")
    assert modal.name_input.required                                    # a raid needs a name

    async def submit(modal, uid=ORG, when=None, name=None, size=None, notes=None, channel=2, manager=False):
        modal.when_input._value = modal.when_input.default if when is None else when
        modal.name_input._value = modal.name_input.default if name is None else name
        modal.size_input._value = modal.size_input.default if size is None else size
        modal.notes_input._value = (modal.notes_input.default or "") if notes is None else notes
        it = interaction(uid, channel=channel, manager=manager)
        await modal.on_submit(it)
        return it

    # untouched form: nothing changes, nothing is announced (stray spaces in the name or notes count as untouched)
    it = await submit(modal)
    assert it.response.send_message.await_args.args[0] == "Nothing changed."
    it.followup.send.assert_not_awaited()
    it = await submit(event_panels.EditModal(events.get_event(ev4), tz), name=" Eternal  Ludra", notes="Bring potions  ")
    assert it.response.send_message.await_args.args[0] == "Nothing changed."

    # a new time, typed in the organizer's timezone: saved, alerts re-armed, announced in the channel
    new_local = (start + timedelta(hours=26)).astimezone(tz)
    it = await submit(modal, when=new_local.strftime("%Y-%m-%d %H:%M"))
    moved = events.get_event(ev4)
    assert moved["starts_at"] == int(new_local.timestamp()) and it.response.send_message.await_args.args[0] == "Event updated: time."
    assert events.get_signup(ev4, 7)["alerted"] == 0
    notice = it.followup.send.await_args.args[0]
    assert f"<t:{moved['starts_at']}:F>" in notice and f"It was <t:{int(start.timestamp())}:F>" in notice
    assert "discord.com/channels" not in notice                         # posted in the event's own channel: no link needed
    assert it.followup.send.await_args.kwargs["allowed_mentions"].everyone is False
    it.client.get_partial_messageable.return_value.get_partial_message.return_value.edit.assert_awaited()

    # relative times work, and from another channel the notice links to the card
    modal = event_panels.EditModal(events.get_event(ev4), tz)
    it = await submit(modal, when="in 3h", channel=777)
    assert abs(events.get_event(ev4)["starts_at"] - (time.time() + 3 * 3600)) < 120
    assert "discord.com/channels/1/2/3" in it.followup.send.await_args.args[0]

    # name, size and notes together; empty notes clear them
    modal = event_panels.EditModal(events.get_event(ev4), tz)
    it = await submit(modal, name="  Immortal   Ludra ", size="12", notes="")
    ev = events.get_event(ev4)
    assert (ev["name"], ev["capacity"], ev["description"]) == ("Immortal Ludra", 12, "")
    assert it.response.send_message.await_args.args[0] == "Event updated: size, name, notes."
    it.followup.send.assert_not_awaited()                              # the time did not change

    # bad input changes nothing and says why
    before = dict(events.get_event(ev4))
    modal = event_panels.EditModal(events.get_event(ev4), tz)
    it = await submit(modal, when="sometime soon", size="99", name=" ")
    msg = it.response.send_message.await_args.args[0]
    assert msg.startswith("Nothing was changed.") and "Couldn't read that time" in msg
    assert "from 2 to 60" in msg and "needs a name" in msg
    assert dict(events.get_event(ev4)) == before
    it = await submit(event_panels.EditModal(events.get_event(ev4), tz), when=(datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M"))
    assert "already passed" in it.response.send_message.await_args.args[0] and dict(events.get_event(ev4)) == before

    # an event whose start has just passed can still be renamed: the untouched time is not read again
    c.execute("UPDATE events SET starts_at=? WHERE id=?", (NOW - 600, ev4)); c.commit()
    it = await submit(event_panels.EditModal(events.get_event(ev4), tz), name="Late Ludra")
    assert it.response.send_message.await_args.args[0] == "Event updated: name." and events.get_event(ev4)["starts_at"] == NOW - 600

    # an arena's name may be emptied (it falls back to the event type, as at creation); a custom event's may not
    ev5 = new_event(kind="arena", name="Arena practice")
    form = event_panels.EditModal(events.get_event(ev5), tz)
    assert not form.name_input.required
    await submit(form, name="")
    assert events.get_event(ev5)["name"] == "Arena"
    ev6 = new_event(kind="custom", name="Guild night")
    form = event_panels.EditModal(events.get_event(ev6), tz)
    assert form.name_input.required
    it = await submit(form, name=" ")
    assert "needs a name" in it.response.send_message.await_args.args[0] and events.get_event(ev6)["name"] == "Guild night"
    it = await submit(event_panels.EditModal(events.get_event(ev5), tz), uid=55, size="20")
    assert "Only the organizer" in it.response.send_message.await_args.args[0] and events.get_event(ev5)["capacity"] == 10
    it = await submit(event_panels.EditModal(events.get_event(ev5), tz), uid=56, size="20", manager=True)   # a server manager may
    assert events.get_event(ev5)["capacity"] == 20

    # /event edit goes through the same change and announces a new time the same way
    cog = event_commands.Events(MagicMock())
    cog.bot.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
    it = interaction(ORG)
    await event_commands.Events.edit.callback(cog, it, event=str(ev5), when="in 5h", size=None, name=None, description=None)
    assert abs(events.get_event(ev5)["starts_at"] - (time.time() + 5 * 3600)) < 120
    assert it.response.send_message.await_args.args[0] == "Event updated: time."
    assert "has a new start time" in it.followup.send.await_args.args[0]
    it = interaction(ORG)
    await event_commands.Events.edit.callback(cog, it, event=str(ev5), when=None, size=None, name="Raid night", description="Voice on")
    ev = events.get_event(ev5)
    assert (ev["name"], ev["description"]) == ("Raid night", "Voice on") and it.followup.send.await_count == 0
    print("EVENT CLASSES AND EDIT OK")


asyncio.run(main())
