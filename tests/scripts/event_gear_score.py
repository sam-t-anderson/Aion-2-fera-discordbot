"""Event signups: gear score next to combat power, typed in one form."""
import asyncio, os, sqlite3, sys, tempfile, time
from unittest.mock import MagicMock, AsyncMock
p = os.path.join(tempfile.mkdtemp(), "old.db")
os.environ["DB_PATH"] = p
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

# a database from before gear score existed, with a signup in it
o = sqlite3.connect(p)
o.executescript("""
CREATE TABLE signups (event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL, role TEXT NOT NULL,
    power INTEGER, party INTEGER, alert INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0,
    joined_at REAL NOT NULL, PRIMARY KEY (event_id, user_id));
INSERT INTO signups (event_id, user_id, status, role, power, joined_at) VALUES (1, 7, 'yes', 'tank', 12500, 1);
""")
o.commit(); o.close()
import db, events, event_panels

P = event_panels.parse_stat
assert P("12500", 99_999_999) == 12500 and P(" 12,500 ", 99_999_999) == 12500 and P("12.5k", 99_999_999) == 12500
assert P("", 99_999) is None and P("   ", 99_999) is None and P("2100", 99_999) == 2100
for bad in ("abc", "0", "-5", "100000", "nan", "inf", "1e99"):
    try:
        P(bad, 99_999); raise SystemExit(f"should fail {bad!r}")
    except (ValueError, OverflowError):
        pass


async def main():
    db.init()
    c = db.conn()
    assert "gear_score" in [r[1] for r in c.execute("PRAGMA table_info(signups)")]
    c.execute("INSERT INTO events (guild_id,organizer_id,kind,name,starts_at,capacity,party_size,created_at) VALUES (1,99,'raid','Eternal Ludra',?,10,5,?)",
              (int(time.time()) + 3600, int(time.time())))
    c.commit()
    ev = events.get_event(1)
    old = events.get_signup(1, 7)
    assert old["power"] == 12500 and old["gear_score"] is None             # the old signup survived the upgrade
    assert "12,500 CP" in events._player_line(old) and "GS" not in events._player_line(old)

    async def submit(cp, gs, uid=7):
        modal = event_panels.CPModal(1, old["power"], old["gear_score"])
        modal.cp._value, modal.gs._value = cp, gs
        it = MagicMock(); it.user.id = uid; it.guild_id = 1; it.message = None
        it.response.send_message = AsyncMock(); it.response.edit_message = AsyncMock(); it.client = MagicMock()
        await modal.on_submit(it)
        return it

    modal = event_panels.CPModal(1, 12500, 2100)
    assert [i.label for i in modal.children] == ["Your combat power (leave empty to clear)", "Your gear score (leave empty to clear)"]
    assert modal.cp.default == "12500" and modal.gs.default == "2100" and len(modal.title) <= 45

    await submit("13k", "2,150")
    su = events.get_signup(1, 7)
    assert su["power"] == 13000 and su["gear_score"] == 2150
    assert events._player_line(su) == "<@7> · 13,000 CP · 2,150 GS"
    embed, view = event_panels.panel_parts(ev, 7)
    assert "Combat power: **13,000**" in embed.description and "Gear score: **2,150**" in embed.description
    assert "Set combat power / gear score" in [c.item.label for c in view.children]
    assert "2,150 GS" in str(events.render_event(ev, events.get_signups(1)).to_dict())

    # gear score alone, and clearing both
    await submit("", "2200")
    su = events.get_signup(1, 7); assert su["power"] is None and su["gear_score"] == 2200
    assert events._player_line(su) == "<@7> · 2,200 GS"
    await submit("", "")
    su = events.get_signup(1, 7); assert su["power"] is None and su["gear_score"] is None
    assert "Gear score: **not set**" in event_panels.panel_parts(ev, 7)[0].description

    # a bad number changes nothing and says why
    await submit("13000", "2100")
    it = await submit("13000", "lots")
    assert "plain numbers" in it.response.send_message.await_args.args[0]
    su = events.get_signup(1, 7); assert su["power"] == 13000 and su["gear_score"] == 2100
    # changing role keeps both numbers
    events.upsert_signup(1, 7, "maybe", "dps")
    su = events.get_signup(1, 7); assert su["role"] == "dps" and su["power"] == 13000 and su["gear_score"] == 2100
    print("GEAR SCORE OK")
asyncio.run(main())
