# migration from the single-server layout
import os, sys, tempfile, sqlite3
path = os.path.join(tempfile.mkdtemp(), "old.db")
os.environ["DB_PATH"] = path; os.environ["GUILD_ID"] = "999"; os.environ["ANNOUNCE_CHANNEL_ID"] = "555"
o = sqlite3.connect(path)
o.executescript("""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE boss_list (kind TEXT, name TEXT, PRIMARY KEY(kind,name));
CREATE TABLE custom_links (name TEXT PRIMARY KEY, label TEXT, url TEXT);
CREATE TABLE timer_manual (name TEXT PRIMARY KEY COLLATE NOCASE, category TEXT, next_at INTEGER, interval_min INTEGER, replaces_at INTEGER, respawn_min INTEGER, set_by INTEGER, set_at INTEGER);
CREATE TABLE timer_subs (user_id INTEGER, kind TEXT, target TEXT, lead INTEGER, PRIMARY KEY(user_id,kind,target));
INSERT INTO meta VALUES ('timer_board_channel','77'),('timer_board_message','88');
INSERT INTO boss_list VALUES ('raid','My Custom Raid'),('world_boss','Kallon the Broken');
INSERT INTO custom_links VALUES ('guide','Guide','https://example.com');
INSERT INTO timer_manual VALUES ('Reshanta Boss','Field Bosses',4102444800,NULL,NULL,240,7,1);
INSERT INTO timer_subs VALUES (5,'name','Reshanta Boss',5);
""")
o.commit(); o.close()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import db
db.init(); c = db.conn()
assert db.bosses(999, "raid") == ["My Custom Raid"], db.bosses(999, "raid")       # their list kept as it was
assert c.execute("SELECT url FROM guild_links WHERE guild_id=999").fetchone()[0] == "https://example.com"
assert c.execute("SELECT respawn_min FROM guild_timer_manual WHERE guild_id=999").fetchone()[0] == 240
assert c.execute("SELECT lead FROM guild_timer_subs WHERE guild_id=999 AND user_id=5").fetchone()[0] == 5
assert db.announce_channels() == [(999, 555)]
assert (db.get_setting(999, "board_channel"), db.get_setting(999, "board_message")) == (77, 88)
db.init(path)                                     # second start: nothing duplicated or reset
assert c is not db.conn() and db.bosses(999, "raid") == ["My Custom Raid"]
assert len(db.bosses(1000, "raid")) == 4
print("MIGRATION OK")
