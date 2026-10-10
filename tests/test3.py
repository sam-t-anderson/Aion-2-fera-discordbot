import asyncio, os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import discord
from discord.ext import commands
import config, db, util, timers, links, guilds

UTC = timezone.utc
D = util.parse_duration
assert D("4h") == timedelta(hours=4) and D("90m") == timedelta(minutes=90)
assert D("1d2h") == timedelta(days=1, hours=2) and D("in 4h30m") == timedelta(hours=4, minutes=30)
assert D("2 hours") == timedelta(hours=2) and D("45 min") == timedelta(minutes=45)
for bad in ["", "soon", "0h", "4", "h4"]:
    try: D(bad); raise SystemExit(f"should fail {bad!r}")
    except ValueError: pass

A, B = 111, 222
def sel(sql, *a): return db.conn().execute(sql, a).fetchall()
def times(g, name, start=None, span=8*86400):
    start = start or int(time.time())
    return sorted(t for n, c, t in timers.events_between(g, start, start + span) if n.lower() == name.lower())

async def main():
    db.init(); c = db.conn()
    assert db.ensure_guild(A) and not db.ensure_guild(A) and db.ensure_guild(B)
    assert len(db.bosses(A, "raid")) == 4 and "Eternal Gartua" in db.bosses(B, "world_boss", "gartua")
    rows = [("Spatial Rift","Rifts","every 3h from 02:00"),("Beritra Air Raid","Events","every hour at :30"),
            ("Watcher Kaira","World Bosses","every 1h")]
    n, new = timers.store_rules(rows)
    assert n == 3 and new == []
    assert "Watcher Kaira" in db.bosses(A, "world_boss", "watcher") and "Watcher Kaira" in db.bosses(B, "world_boss", "watcher")
    n, new = timers.store_rules(rows + [("Brand New Boss","World Bosses","Wed, Sat at 22:00"), ("Strange Thing","Mystery","every 2h")])
    assert [x[0] for x in new] == ["Brand New Boss","Strange Thing"]
    for g in (A, B): assert "Brand New Boss" in db.bosses(g, "world_boss")
    db.ensure_guild(333)                                     # a server that joins later gets them too
    assert "Brand New Boss" in db.bosses(333, "world_boss")
    assert not db.bosses(A, "world_boss", "Strange")
    assert timers.store_rules(rows + [("Brand New Boss","World Bosses","Wed, Sat at 22:00"), ("Strange Thing","Mystery","every 2h")])[1] == []
    timers.store_rules(rows)

    timers.purge_old()
    base = times(A, "Spatial Rift"); assert len(base) > 10 and times(B, "Spatial Rift") == base
    nxt = timers.scraped_next("every 3h from 02:00", timers.guild_tz(A))
    mine = int(time.time()) + 3600 + 7
    timers.save_manual(A, "Spatial Rift", "Rifts", mine, None, nxt, None, 42)
    ts = times(A, "Spatial Rift")
    assert mine in ts and nxt not in ts and len(ts) == len(base)
    assert times(B, "Spatial Rift") == base                   # server B is untouched
    timers.purge_old()                              # a refetch keeps the manual time
    assert mine in times(A, "Spatial Rift")
    timers.clear_manual(A, "spatial rift"); assert times(A, "Spatial Rift") == base

    timers.save_manual(A, "Watcher Kaira", "World Bosses", int(time.time()) + 600, 240, None, None, 42) if False else \
        timers.save_manual(A, "Watcher Kaira", "World Bosses", int(time.time()) + 600, 240, None, 42 and None, 42)
    ts = times(A, "Watcher Kaira"); assert len(ts) >= 40 and all(b - a == 240*60 for a, b in zip(ts, ts[1:]))
    assert len(times(B, "Watcher Kaira")) > 100               # B still has the hourly schedule
    timers.clear_manual(A, "Watcher Kaira")

    timers.save_manual(A, "Reshanta Boss", "Field Bosses", int(time.time()) + 7200, None, None, 240, 7)
    timers.save_manual(A, "Reshanta Boss", "Field Bosses", int(time.time()) + 14400, None, None, None, 8)
    r = sel("SELECT respawn_min, set_by FROM guild_timer_manual WHERE guild_id=? AND name='reshanta boss'", A)[0]
    assert (r["respawn_min"], r["set_by"]) == (240, 8)
    assert timers.canonical(A, "RESHANTA boss")[0] == "Reshanta Boss"
    assert timers.canonical(B, "Reshanta Boss") == ("Reshanta Boss", None, None)
    assert "Reshanta Boss" in timers.all_names(A) and "Reshanta Boss" not in timers.all_names(B)
    assert "Field Bosses" in timers.all_categories(A) and "Field Bosses" not in timers.all_categories(B)
    ea, eb = timers.build_embed(A), timers.build_embed(B)
    assert "Reshanta Boss" in str(ea.to_dict()) and "Reshanta Boss" not in str(eb.to_dict())
    assert timers.build_embed(A, "Nothing") is None

    # notifications use the subscriber's server
    now = int(time.time())
    c.execute("DELETE FROM timer_rules"); c.commit()
    for g in (A, B): timers.save_manual(g, "Watcher Kaira", "World Bosses", now + 120, None, None, None, 1)
    c.execute("INSERT INTO guild_timer_subs VALUES (?,5,'category','world bosses',5)", (A,))
    c.execute("INSERT INTO guild_timer_subs VALUES (?,6,'name','Watcher Kaira',1)", (B,)); c.commit()
    sent = []
    class U:
        def __init__(s, i): s.i = i
        async def send(s, text): sent.append((s.i, text))
    class FB:
        def get_user(s, i): return U(i)
        async def fetch_user(s, i): return U(i)
    cog = timers.Timers.__new__(timers.Timers); cog.bot = FB()
    await timers.Timers.notify_loop.coro(cog)
    assert [i for i, _ in sent] == [5], sent                  # user 6 wants 1 min before
    await timers.Timers.notify_loop.coro(cog); assert len(sent) == 1   # not twice
    # a manual one-shot in server B moves the time for B's subscriber only
    timers.save_manual(B, "Watcher Kaira", "World Bosses", now + 30, None, None, None, 1)
    c.execute("DELETE FROM timer_notified"); c.commit(); sent.clear()
    await timers.Timers.notify_loop.coro(cog)
    assert sorted(i for i, _ in sent) == [5, 6], sent

    # links and boss lists are per server
    db.conn().execute("INSERT INTO guild_links VALUES (?, 'guide','Guide','https://example.com')", (A,)); c.commit()
    assert "guide" in links.all_links(A) and "guide" not in links.all_links(B)

    # settings + leaving
    db.set_settings(A, announce_channel=5, board_channel=6, board_message=7)
    assert db.announce_channels() == [(A, 5)] and db.get_setting(A, "board_message") == 7
    assert db.get_setting(B, "announce_channel") is None
    db.forget_guild(A)
    assert db.get_setting(A, "announce_channel") is None and not sel("SELECT 1 FROM guild_links WHERE guild_id=?", A)
    assert not sel("SELECT 1 FROM guild_timer_manual WHERE guild_id=?", A)
    assert db.ensure_guild(A)                                 # re-adding starts fresh with the default lists
    assert len(db.bosses(A, "raid")) == 4

    # huge list stays within Discord limits
    timers.store_rules([(f"Boss number {i} with a fairly long name", "World Bosses", "every 2h") for i in range(150)])
    timers.purge_old()
    d = timers.build_embed(B, max_chars=5900).to_dict()
    total = len(d["title"]) + len(d["description"]) + len(d.get("footer", {}).get("text", "")) + sum(len(f["name"])+len(f["value"]) for f in d["fields"])
    assert total <= 6000 and len(d["fields"]) <= 25 and "more not shown" in d["footer"]["text"], total

    # commands register, including the new /setup
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
    await timers.setup(bot); await guilds.setup(bot)
    names = sorted(x.name for x in bot.tree.get_commands()); print(names)
    assert {"timer-setup", "timers", "setup"} <= set(names)
    for cmd in bot.tree.get_commands():
        p = cmd.to_dict(bot.tree); assert len(p["description"]) <= 100 and p["default_member_permissions"] if cmd.name in ("setup", "timer-setup") else True
        def walk(o):
            assert len(o["description"]) <= 100 and len(o["name"]) <= 32
            for s in o.get("options", []): walk(s)
        walk(p)
    print("setup:", sorted(o.name for o in bot.tree.get_command("setup").commands))
    # board
    board = bot.get_cog("TimerBoard")
    assert await board._message(B) is None
    db.set_settings(B, board_channel=5, board_message=6); board._forget(B); assert db.get_setting(B, "board_message") is None
    assert board._board_embed(B).footer.text
    for cog in list(bot.cogs.values()): await bot.remove_cog(cog.qualified_name)

    # welcome text fits Discord's limits
    assert len(guilds.WELCOME) <= 4096
    print("ALL OK")
asyncio.run(main())
