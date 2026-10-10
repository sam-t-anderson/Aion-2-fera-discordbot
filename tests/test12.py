"""Upgrading a database from before regions: boss names, checklist progress, timer corrections."""
import asyncio, os, sqlite3, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock
p = os.path.join(tempfile.mkdtemp(), "old.db")
os.environ["DB_PATH"] = p
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import config, db, timers, checklist

now = datetime.now(timezone.utc)
day = (now - timedelta(hours=config.RESET_UTC_HOUR)).date()
o = sqlite3.connect(p)
o.executescript("""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, announce_channel INTEGER, board_channel INTEGER, board_message INTEGER, joined_at INTEGER NOT NULL);
CREATE TABLE guild_bosses (guild_id INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (guild_id, kind, name));
CREATE TABLE progress (user_id INTEGER NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL, count INTEGER NOT NULL, period TEXT NOT NULL, PRIMARY KEY (user_id, kind, item));
CREATE TABLE timer_events (name TEXT NOT NULL, category TEXT NOT NULL, starts_at INTEGER NOT NULL, PRIMARY KEY (name, starts_at));
CREATE TABLE guild_timer_manual (guild_id INTEGER NOT NULL, name TEXT NOT NULL COLLATE NOCASE, category TEXT NOT NULL, next_at INTEGER NOT NULL,
    interval_min INTEGER, replaces_at INTEGER, shift_sec INTEGER, respawn_min INTEGER, set_by INTEGER NOT NULL, set_at INTEGER NOT NULL, PRIMARY KEY (guild_id, name));
INSERT INTO meta VALUES ('timers_tz_min', '0');
INSERT INTO guild_settings VALUES (1, NULL, NULL, NULL, 1);
INSERT INTO guild_bosses VALUES (1, 'raid', 'The Abyssal Reactor (Fusion Reactor)'), (1, 'raid', 'Eternal Ludra'),
    (1, 'world_boss', 'Kallon the Broken'), (1, 'world_boss', 'Anomalous Shard'), (1, 'world_boss', 'Our Own Boss');
INSERT INTO guild_timer_manual VALUES (1, 'Weekly Reset', 'Events', 5, NULL, 1, 14400, NULL, 9, 1), (1, 'Our Own Boss', 'Field Bosses', 5, 240, NULL, NULL, NULL, 9, 1);
""")
o.execute("INSERT INTO progress VALUES (42, 'daily', 'nightmare', 2, ?), (42, 'daily', 'odal', 1, '2020-01-01')", (day.isoformat(),))
o.commit(); o.close()

db.init()
c = db.conn()
names = [r["name"] for r in c.execute("SELECT name FROM guild_bosses WHERE guild_id=1 AND kind='raid' ORDER BY name")]
assert names == ["Central Abyss Fusion Reactor", "Eternal Ludra"], names
world = {r["name"] for r in c.execute("SELECT name FROM guild_bosses WHERE guild_id=1 AND kind='world_boss'")}
assert "Our Own Boss" in world and "Kallon the Broken" not in world and "Anomalous Shard" not in world
assert {"Eternal Gartua", "Immortal Gartua", "High Commander Lagta", "Soul Ruler Kashapa", "Silent Dartan"} <= world
left = [r["name"] for r in c.execute("SELECT name FROM guild_timer_manual")]
assert left == ["Our Own Boss"], left                      # the correction against the UTC reading is gone
assert timers.region_key(1) == "na" and db.get_setting(1, "timer_region") is None
assert checklist.get_counts(42, "daily", 1) == {"nightmare": 2}   # today's ticks survive the upgrade
c.execute("DELETE FROM guild_bosses WHERE name='Eternal Gartua'"); c.commit()
db._conn = None; db.init()                                  # runs once only
assert not db.conn().execute("SELECT 1 FROM guild_bosses WHERE name='Eternal Gartua'").fetchone()


async def main():
    cog = timers.Timers.__new__(timers.Timers)
    it = MagicMock(); it.guild_id = 1; it.user.id = 5; it.response.send_message = AsyncMock()
    await timers.Timers.killed.callback(cog, it, name="eternal gartua")
    row = db.conn().execute("SELECT * FROM guild_timer_manual WHERE name='Eternal Gartua'").fetchone()
    assert row["category"] == "Field Bosses" and row["respawn_min"] == 360
    assert abs(row["next_at"] - (time.time() + 6 * 3600)) < 5
    assert "Eternal Gartua" in str(timers.build_embed(1).to_dict())
    it.response.send_message.reset_mock()
    await timers.Timers.killed.callback(cog, it, name="Silent Dartan")   # two zones, two respawns: asks
    assert "respawn" in it.response.send_message.await_args.args[0].lower()
    print("UPGRADE OK")
asyncio.run(main())
