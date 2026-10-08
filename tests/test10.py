import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import db, timers
UTC = timezone.utc
G = 1
db.init(); c = db.conn(); db.ensure_guild(G)
now = datetime.now(UTC).replace(second=0, microsecond=0)
c.execute("INSERT INTO timer_rules (name, category, timing) VALUES ('Spacetime Rift','Rifts','every 3h from 02:00')")
c.execute("INSERT INTO timer_rules (name, category, timing) VALUES ('Executor Argo','World Bosses','Mon, Thu, Sat at 21:30')")
c.commit(); timers.rebuild_occurrences(now)
def times(name, days=7):
    a = int(now.timestamp()); return sorted(t for n, _c, t in timers.events_between(G, a, a + days * 86400) if n == name)

site_rift = times("Spacetime Rift"); site_argo = times("Executor Argo")
assert len(site_rift) >= 50 and len(site_argo) >= 2

# 1) override a weekly boss by +15 min: EVERY start moves, not just the next one
nxt = site_argo[0] + 15 * 60
site = timers.nearest_site_time("Mon, Thu, Sat at 21:30", nxt); assert site == site_argo[0]
timers.save_manual(G, "Executor Argo", "World Bosses", nxt, None, site, None, 9, nxt - site)
got = times("Executor Argo"); assert got == [t + 900 for t in site_argo], (got, site_argo)

# 2) survives the site refresh cadence and a restart (fresh connection to the same file)
timers.rebuild_occurrences(now + timedelta(minutes=15)); timers.rebuild_occurrences(now + timedelta(hours=30))
assert times("Executor Argo", 7)[0] == site_argo[0] + 900
db._conn = None; db.init(); db.ensure_guild(G)
assert times("Executor Argo")[:2] == [t + 900 for t in site_argo[:2]]

# 3) the 30-day purge no longer deletes a shift once its first time is old
c = db.conn(); c.execute("UPDATE guild_timer_manual SET next_at=? WHERE name='Executor Argo'", (int(now.timestamp()) - 40 * 86400,)); c.commit()
timers.rebuild_occurrences(now); assert c.execute("SELECT COUNT(*) FROM guild_timer_manual").fetchone()[0] == 1

# 4) interval timer: -20 min shift applies to every 3h start
r = site_rift[3] - 20 * 60
timers.save_manual(G, "Spacetime Rift", "Rifts", r, None, site_rift[3], None, 9, -20 * 60)
assert times("Spacetime Rift") == [t - 1200 for t in site_rift]

# 5) only_this_time (no shift): only the nearest site start changes
timers.save_manual(G, "Spacetime Rift", "Rifts", site_rift[3] + 600, None, timers.nearest_site_time("every 3h from 02:00", site_rift[3] + 600), None, 9, None)
got = times("Spacetime Rift"); assert site_rift[3] not in got and site_rift[3] + 600 in got and len(got) == len(site_rift)
assert site_rift[2] in got and site_rift[4] in got

# 6) clear goes back to the site
timers.clear_manual(G, "Spacetime Rift"); assert times("Spacetime Rift") == site_rift
# 7) another server is unaffected
db.ensure_guild(2)
a = int(now.timestamp()); assert sorted(t for n, _c, t in timers.events_between(2, a, a + 7 * 86400) if n == "Executor Argo") == site_argo
# 8) old database without the column gets migrated
import sqlite3
p = os.path.join(tempfile.mkdtemp(), "old.db"); o = sqlite3.connect(p)
o.executescript("CREATE TABLE guild_timer_manual (guild_id INTEGER NOT NULL, name TEXT NOT NULL COLLATE NOCASE, category TEXT NOT NULL, next_at INTEGER NOT NULL, interval_min INTEGER, replaces_at INTEGER, respawn_min INTEGER, set_by INTEGER NOT NULL, set_at INTEGER NOT NULL, PRIMARY KEY (guild_id, name)); INSERT INTO guild_timer_manual VALUES (1,'X','Rifts',5,NULL,NULL,NULL,1,1);")
o.commit(); o.close()
os.environ["DB_PATH"] = p; import importlib, config; importlib.reload(config); importlib.reload(db); db.init()
cols = [r[1] for r in db.conn().execute("PRAGMA table_info(guild_timer_manual)")]; assert "shift_sec" in cols
print("TIMER OVERRIDES OK")
