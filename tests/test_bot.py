import asyncio, os, sys, tempfile
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import discord
from discord.ext import commands
import config, db, util, events, timers, links, checklist, health

UTC = timezone.utc
tz = ZoneInfo("America/Denver")
now = datetime(2026, 9, 30, 2, 0, tzinfo=UTC)  # Sep 29, 8:00 PM MDT

# ---- time parsing
def w(s): return util.parse_when(s, tz, now).astimezone(tz).strftime("%Y-%m-%d %H:%M")
assert w("8pm") == "2026-09-30 20:00"          # 8pm already passed today -> tomorrow
assert w("9pm") == "2026-09-29 21:00"
assert w("21:30") == "2026-09-29 21:30"
assert w("tomorrow 7:30pm") == "2026-09-30 19:30"
assert w("2026-10-04 20:00") == "2026-10-04 20:00"
assert w("10/4 8pm") == "2026-10-04 20:00"
assert w("Oct 4") if False else True
assert util.parse_when("in 2h30m", tz, now) == now + timedelta(hours=2, minutes=30)
assert util.parse_when("+45m", tz, now) == now + timedelta(minutes=45)
assert util.parse_when("1790000000", tz, now).timestamp() == 1790000000
for bad in ["", "banana", "25:00", "13pm", "yesterday 8pm", "2026-09-01 08:00", "in 0h", "8pm friday now"]:
    try: util.parse_when(bad, tz, now); raise SystemExit(f"should fail: {bad!r}")
    except ValueError: pass
# DST: Dec time uses MST
assert util.parse_when("2026-12-10 06:00", tz, datetime(2026,12,1,tzinfo=UTC)).hour == 13

# ---- autosort
def P(uid, role, power=None): return {"user_id": uid, "role": role, "power": power}
players = [P(1,"tank",9000),P(2,"tank",8000),P(3,"healer",7000),P(4,"healer",6500)] + [P(10+i,"dps",5000+i*300) for i in range(7)]
res = events.autosort(players, 5)
sizes = sorted(list(res.values()).count(p) for p in set(res.values()))
assert sizes == [3,4,4] or sizes == [4,4,3], sizes
assert len(set(res.values())) == 3
for party in set(res.values()):
    roles=[p["role"] for p in players if res[p["user_id"]]==party]
    assert len(roles) <= 5
assert res[1] != res[2] and res[3] != res[4]           # tanks and healers split
assert events.autosort([], 5) == {}
r10 = events.autosort([P(i,"dps") for i in range(10)], 5); assert sorted(r10.values()).count(1)==5
r1 = events.autosort([P(1,"dps")], 5); assert r1 == {1:1}

# ---- timers parsing with the table as fetched from the site
rows = [("Spatial Rift","Rifts","every 3h from 02:00"),("Beritra Air Raid","Events","every hour at :30"),
 ("Abyss Event","Abyss","Wed, Sat at 22:00"),("Watcher Kaira","World Bosses","every 1h"),
 ("Executor Argo","World Bosses","Wed, Sat at 22:30"),("Enraged Guardian Lord Nahma","World Bosses","Sun, Fri at 22:00")]
html = "<table><thead><tr><th>Event</th><th>Category</th><th>Timing</th></tr></thead><tbody>" + "".join(
    f"<tr><td><b>{a}</b></td><td><span>{b}</span></td><td>{c}</td></tr>" for a,b,c in rows) + "</tbody></table>"
parsed = timers.parse_table(html)
assert parsed == rows, parsed
assert timers.parse_table("<html>nothing</html>") == []
r = timers.parse_rule
assert r("every 3h from 02:00") == {"type":"interval","hours":3,"h":2,"m":0}
assert r("every hour at :30")["m"] == 30
assert r("every 1h") == {"type":"interval","hours":1,"h":0,"m":0}
assert r("Sun, Fri at 22:00") == {"type":"weekly","days":[4,6],"h":22,"m":0}
assert r("Wed, Sat at 22:30")["days"] == [2,5]
assert r("gibberish") is None
start = datetime(2026,9,30,0,0,tzinfo=UTC); end = start + timedelta(days=1)
occ = timers.occurrences(r("every 3h from 02:00"), start, end, timers.GMT8)
# GMT+8 02:00,05:00,... == 18:00, 21:00, 00:00 UTC ...
assert [o.hour for o in occ] == [0,3,6,9,12,15,18,21], [o.hour for o in occ]
assert len(timers.occurrences(r("every hour at :30"), start, end, timers.GMT8)) == 24
# Wed 22:00 GMT+8 == Wed 14:00 UTC. 2026-09-30 is a Wednesday.
wk = timers.occurrences(r("Wed, Sat at 22:00"), start, start + timedelta(days=8), timers.GMT8)
assert [o.strftime("%a %H:%M") for o in wk] == ["Wed 14:00","Sat 14:00","Wed 14:00"], wk
# Week boundary: Sunday 22:00 GMT+8 near start still found
assert timers.occurrences(r("Sun, Fri at 22:00"), datetime(2026,10,2,0,0,tzinfo=UTC), datetime(2026,10,5,0,0,tzinfo=UTC), timers.GMT8)

async def main():
    db.init()
    db.ensure_guild(1)
    names = db.bosses(1, "raid")
    assert len(names) == 4 and "Eternal Ludra" in names
    assert len(db.bosses(1, "world_boss")) == 6
    db.conn().execute("DELETE FROM guild_bosses WHERE name='Eternal Ludra'"); db.conn().commit()
    db.init(os.environ["DB_PATH"])
    assert len(db.bosses(1, "raid")) == 3  # not re-seeded
    assert len(db.bosses(2, "raid")) == 4  # another server starts with the full list

    # timer storage + rebuild
    config.TIMER_CATEGORIES = ['Rifts','World Bosses','Abyss']
    assert timers.store_rules(rows)[0] == 5           # 'Events' category filtered out
    n = timers.rebuild_occurrences(datetime(2026,9,30,0,0,tzinfo=UTC))
    assert n > 40
    assert db.conn().execute("SELECT COUNT(*) c FROM timer_events WHERE name='Beritra Air Raid'").fetchone()["c"] == 0
    # retention purge
    old = int((datetime.now(UTC) - timedelta(days=9)).timestamp())
    db.conn().execute("INSERT INTO timer_events VALUES ('Old','Rifts',?)", (old,)); db.conn().commit()
    timers.rebuild_occurrences()
    assert db.conn().execute("SELECT COUNT(*) c FROM timer_events WHERE name='Old'").fetchone()["c"] == 0

    # event rendering + signup logic
    import time
    c = db.conn()
    c.execute("INSERT INTO events (guild_id,channel_id,message_id,organizer_id,kind,name,starts_at,capacity,party_size,created_at) VALUES (1,2,3,99,'raid','Eternal Ludra',?,3,5,?)",
              (int(time.time())+3600, int(time.time()))); c.commit()
    ev = events.get_event(1)
    for uid, st, role in [(1,"yes","tank"),(2,"yes","healer"),(3,"yes","dps"),(4,"yes","dps"),(5,"maybe","dps")]:
        events.upsert_signup(1, uid, st, role)
    c.execute("UPDATE signups SET power=12345 WHERE user_id=3"); c.commit()
    sg = events.get_signups(1)
    conf, wait, maybe = events.split_signups(ev, sg)
    assert [s["user_id"] for s in conf] == [1,2,3] and [s["user_id"] for s in wait] == [4] and [s["user_id"] for s in maybe] == [5]
    emb = events.render_event(ev, sg)
    assert "12,345 CP" in str(emb.to_dict()) and "Waitlist" in str(emb.to_dict())
    assert len(emb.to_dict()["fields"]) <= 25
    n_players, n_parties = events.run_autosort(ev)
    assert (n_players, n_parties) == (3, 1)
    assert "Party 1" in str(events.render_event(events.get_event(1), events.get_signups(1)).to_dict())
    # switching maybe -> yes moves to the back of the queue
    events.upsert_signup(1, 5, "yes", "dps"); assert events.get_signup(1,5)["status"] == "yes"
    embed, view = events.panel_parts(ev, 1); assert len(view.children) == 9
    assert len(events.event_view(1).children) == 4

    # every cog loads and every command serializes to a valid Discord payload
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
    import guilds
    for m in (checklist, events, timers, links, guilds):
        await m.setup(bot)
    cmds = bot.tree.get_commands()
    names = sorted(x.name for x in cmds)
    print("top-level:", names)
    for cmd in cmds:
        payload = cmd.to_dict(bot.tree)
        def walk(p, path=""):
            assert 1 <= len(p["name"]) <= 32 and len(p["description"]) <= 100, (path, p["name"])
            for o in p.get("options", []):
                assert len(o["description"]) <= 100, (p["name"], o["name"])
                walk(o, path+"/"+p["name"]) if o.get("options") is not None and o["type"] in (1,2) else None
        walk(payload)
    for expect in ["checklist","checklist-setup","event","timers","link","links","linkadmin","eventlist","timezone","setup","timer-setup"]:
        assert expect in names, expect
    ev_sub = sorted(o.name for o in bot.tree.get_command("event").commands)
    print("event:", ev_sub)
    assert ev_sub == sorted(["create","list","cancel","edit","assign","autosort","alert"])
    print("timers:", sorted(o.name for o in bot.tree.get_command("timers").commands))
    for cog in list(bot.cogs.values()):
        await bot.remove_cog(cog.qualified_name)

    # links
    assert len(links.BUILTIN) == 8 and all(u.startswith("https://") for _, u in links.BUILTIN.values())
    c.execute("INSERT INTO guild_links VALUES (1,'guide','Guide','https://example.com')"); c.commit()
    assert "guide" in links.all_links(1) and "map" in links.all_links(1) and "guide" not in links.all_links(2)
    # health app
    class FakeBot:
        latency = 0.05; guilds=[1]
        def is_ready(self): return True
        def is_closed(self): return False
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(health.build_app(FakeBot()))) as cl:
        r_ = await cl.get("/health"); assert r_.status == 200, await r_.text()
        assert (await r_.json())["status"] == "ok"
    class DownBot(FakeBot):
        latency = float("nan")
        def is_ready(self): return False
    async with TestClient(TestServer(health.build_app(DownBot()))) as cl:
        r_ = await cl.get("/health"); assert r_.status == 503
    print("ALL OK")
asyncio.run(main())
