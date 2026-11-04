"""SQLite storage shared by every feature."""

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import config
import gamedata

log = logging.getLogger("aion2.db")

_conn: sqlite3.Connection | None = None

# The layout init() leaves a database in, kept in SQLite's PRAGMA user_version. Raise it with each change
# to the tables, so a database written by a newer version can be recognised.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    item TEXT NOT NULL,
    count INTEGER NOT NULL,
    period TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, item));

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS user_prefs (user_id INTEGER PRIMARY KEY, tz TEXT);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    announce_channel INTEGER,
    board_channel INTEGER,
    board_message INTEGER,
    timer_region TEXT,
    game_server TEXT,
    faction TEXT,
    news_channel INTEGER,
    news_region TEXT,
    joined_at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS news_posted (
    guild_id INTEGER NOT NULL,
    article_id TEXT NOT NULL,
    posted_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, article_id));

CREATE TABLE IF NOT EXISTS game_servers (
    server_id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    name TEXT NOT NULL,
    race INTEGER);

CREATE TABLE IF NOT EXISTS guild_bosses (
    guild_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (guild_id, kind, name));

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    organizer_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    starts_at INTEGER NOT NULL,
    capacity INTEGER NOT NULL,
    party_size INTEGER NOT NULL,
    alert_minutes INTEGER NOT NULL DEFAULT 15,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS signups (
    event_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    role TEXT NOT NULL,
    power INTEGER,
    gear_score INTEGER,
    char_class TEXT,
    party INTEGER,
    alert INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0,
    joined_at REAL NOT NULL,
    PRIMARY KEY (event_id, user_id));

-- The class each member last picked on an event in this server, so their next signup starts with it.
CREATE TABLE IF NOT EXISTS member_classes (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    char_class TEXT NOT NULL,
    set_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id));

CREATE TABLE IF NOT EXISTS guild_links (
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    label TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (guild_id, name));

CREATE TABLE IF NOT EXISTS timer_rules (
    name TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    timing TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS timer_catalog (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    category TEXT NOT NULL,
    timing TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS boss_catalog (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    level INTEGER,
    zones TEXT,
    areas TEXT,
    url TEXT,
    last_seen INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS guild_timer_manual (
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    category TEXT NOT NULL,
    next_at INTEGER NOT NULL,
    interval_min INTEGER,
    replaces_at INTEGER,
    shift_sec INTEGER,
    respawn_min INTEGER,
    set_by INTEGER NOT NULL,
    set_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, name));

CREATE TABLE IF NOT EXISTS guild_timer_subs (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    lead INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, kind, target));

CREATE TABLE IF NOT EXISTS dkp_config (
    guild_id INTEGER PRIMARY KEY,
    season INTEGER NOT NULL DEFAULT 1,
    max_reservations INTEGER NOT NULL DEFAULT 3,
    allow_negative INTEGER NOT NULL DEFAULT 0,
    officer_role INTEGER);

CREATE TABLE IF NOT EXISTS dkp_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    by_user INTEGER,
    season INTEGER NOT NULL DEFAULT 1,
    undone INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    batch_id INTEGER);
CREATE INDEX IF NOT EXISTS dkp_ledger_user ON dkp_ledger (guild_id, season, user_id);

CREATE TABLE IF NOT EXISTS dkp_rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    name TEXT NOT NULL,
    points INTEGER NOT NULL,
    closes_at INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS dkp_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    points INTEGER NOT NULL,
    include_maybe INTEGER NOT NULL DEFAULT 0,
    channel_id INTEGER,
    message_id INTEGER,
    created_by INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready',
    paid_count INTEGER NOT NULL DEFAULT 0,
    acted_by INTEGER,
    acted_at INTEGER);

CREATE TABLE IF NOT EXISTS dkp_boards (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_ids TEXT NOT NULL DEFAULT '[]');

CREATE TABLE IF NOT EXISTS dkp_claims (
    reward_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (reward_id, user_id));

CREATE TABLE IF NOT EXISTS loot_items (
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    cost INTEGER NOT NULL,
    slot TEXT,
    source TEXT,
    PRIMARY KEY (guild_id, name));

CREATE TABLE IF NOT EXISTS loot_reservations (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    item TEXT NOT NULL COLLATE NOCASE,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, item));

CREATE TABLE IF NOT EXISTS timer_notified (
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    starts_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, name, starts_at));
"""

PREBUILT_BOSSES = {
    "raid": [
        "Central Abyss Fusion Reactor",
        "Isolation Specialist Rahu",
        "Abyssal Wings Ketu",
        "Eternal Ludra",
    ],
    "world_boss": gamedata.field_boss_names(),
}
# Names earlier versions shipped that aren't in the game, and what replaces them.
_OLD_RAID_NAMES = {"The Abyssal Reactor (Fusion Reactor)": "Central Abyss Fusion Reactor"}
_OLD_WORLD_BOSSES = (
    "Kallon the Broken", "Gorgon of the Ridge", "队长 (Captain) Maras",
    "The Morheim Dreadborn", "The Balaur Commander", "Anomalous Shard",
)


def conn() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


def is_open() -> bool:
    """Whether the database has been opened (``init``) yet; the health endpoint answers before it is."""
    return _conn is not None


def init(path: str | None = None) -> None:
    """Open the database, create tables and move data from the single-server layout once."""
    global _conn
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # timeout: wait up to ten seconds for a lock someone else holds (SQLite's busy timeout) instead of failing.
    _conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    _conn.row_factory = sqlite3.Row
    # Write-ahead logging: a backup or the sqlite3 tool reading the file never holds up the bot. The
    # database is then checklist.db with its -wal and -shm files; copy the whole data folder to back it up.
    _conn.execute("PRAGMA journal_mode=WAL")
    version = _conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        log.warning("The database was last used by a newer version of the bot (layout %d, this one knows %d). "
                    "If anything looks wrong, restore the data backup taken before that upgrade.", version, SCHEMA_VERSION)
    _conn.executescript(SCHEMA)
    if not any(r["name"] == "shift_sec" for r in _conn.execute("PRAGMA table_info(guild_timer_manual)").fetchall()):
        _conn.execute("ALTER TABLE guild_timer_manual ADD COLUMN shift_sec INTEGER")
    if not any(r["name"] == "batch_id" for r in _conn.execute("PRAGMA table_info(dkp_ledger)").fetchall()):
        _conn.execute("ALTER TABLE dkp_ledger ADD COLUMN batch_id INTEGER")
    if not any(r["name"] == "timer_region" for r in _conn.execute("PRAGMA table_info(guild_settings)").fetchall()):
        _conn.execute("ALTER TABLE guild_settings ADD COLUMN timer_region TEXT")
    if not any(r["name"] == "game_server" for r in _conn.execute("PRAGMA table_info(guild_settings)").fetchall()):
        _conn.execute("ALTER TABLE guild_settings ADD COLUMN game_server TEXT")
    if not any(r["name"] == "faction" for r in _conn.execute("PRAGMA table_info(guild_settings)").fetchall()):
        _conn.execute("ALTER TABLE guild_settings ADD COLUMN faction TEXT")
    for column in ("news_channel INTEGER", "news_region TEXT"):
        if not any(r["name"] == column.split()[0] for r in _conn.execute("PRAGMA table_info(guild_settings)").fetchall()):
            _conn.execute(f"ALTER TABLE guild_settings ADD COLUMN {column}")
    if not any(r["name"] == "gear_score" for r in _conn.execute("PRAGMA table_info(signups)").fetchall()):
        _conn.execute("ALTER TABLE signups ADD COLUMN gear_score INTEGER")
    if not any(r["name"] == "char_class" for r in _conn.execute("PRAGMA table_info(signups)").fetchall()):
        _conn.execute("ALTER TABLE signups ADD COLUMN char_class TEXT")
    _conn.execute("DROP TABLE IF EXISTS timer_events")  # start times are worked out per server region now
    _conn.commit()
    _migrate_single_server()
    _migrate_regions()
    _migrate_boss_names()
    _migrate_progress_stamps()
    if version < SCHEMA_VERSION:
        _conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _conn.commit()


def _migrate_regions() -> None:
    """The schedule used to be read as UTC; it is each region's own server clock. Corrections
    that moved a site timer were made against the wrong clock, so they are dropped (runs once)."""
    if meta_get("timer_regions_v1"):
        return
    _conn.execute("DELETE FROM guild_timer_manual WHERE shift_sec IS NOT NULL")
    _conn.commit()
    meta_delete("timers_tz_min")
    meta_set("timer_regions_v1", str(int(time.time())))


def _migrate_boss_names() -> None:
    """Swap the placeholder boss names of earlier versions for the real ones (runs once)."""
    if meta_get("boss_names_v2"):
        return
    c = _conn
    for gid in [r["guild_id"] for r in c.execute("SELECT guild_id FROM guild_settings").fetchall()]:
        for old, new in _OLD_RAID_NAMES.items():
            c.execute("UPDATE OR IGNORE guild_bosses SET name=? WHERE guild_id=? AND kind='raid' AND name=?", (new, gid, old))
            c.execute("DELETE FROM guild_bosses WHERE guild_id=? AND kind='raid' AND name=?", (gid, old))
        for old in _OLD_WORLD_BOSSES:
            c.execute("DELETE FROM guild_bosses WHERE guild_id=? AND kind='world_boss' AND name=?", (gid, old))
        for name in PREBUILT_BOSSES["world_boss"]:
            c.execute("INSERT OR IGNORE INTO guild_bosses (guild_id, kind, name) VALUES (?, 'world_boss', ?)", (gid, name))
    c.commit()
    meta_set("boss_names_v2", str(int(time.time())))


def _migrate_progress_stamps() -> None:
    """Checklist progress used to be tagged with its period's date; it is now tagged with
    when it was last ticked. Progress from the period in force is kept (runs once)."""
    if meta_get("progress_stamps_v1"):
        return
    c = _conn
    now = datetime.now(timezone.utc)
    day = (now - timedelta(hours=config.RESET_UTC_HOUR)).date()
    current = {
        "daily": day.isoformat(),
        "weekly": (day - timedelta(days=(day.weekday() - config.WEEKLY_RESET_WEEKDAY) % 7)).isoformat(),
    }
    for kind, period in current.items():
        c.execute("UPDATE progress SET period=? WHERE kind=? AND period=?", (str(int(now.timestamp())), kind, period))
    c.execute("DELETE FROM progress WHERE period LIKE '%-%'")
    c.commit()
    meta_set("progress_stamps_v1", str(int(time.time())))


def meta_get(key: str) -> str | None:
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_delete(key: str) -> None:
    conn().execute("DELETE FROM meta WHERE key=?", (key,))
    conn().commit()


def meta_set(key: str, value: str) -> None:
    conn().execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn().commit()


# ---------------------------------------------------------------- servers
def _has_table(name: str) -> bool:
    return _conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _migrate_single_server() -> None:
    """Earlier versions served one server. Give that server its old data (runs once)."""
    if meta_get("multiguild_v1") or not config.GUILD_ID:
        return
    gid = int(config.GUILD_ID)
    c = _conn
    ensure_guild(gid, seed=False)
    if _has_table("boss_list"):
        for r in c.execute("SELECT kind, name FROM boss_list").fetchall():
            c.execute("INSERT OR IGNORE INTO guild_bosses (guild_id, kind, name) VALUES (?,?,?)", (gid, r["kind"], r["name"]))
    if not c.execute("SELECT 1 FROM guild_bosses WHERE guild_id=?", (gid,)).fetchone():
        seed_bosses(gid)
    if _has_table("custom_links"):
        for r in c.execute("SELECT name, label, url FROM custom_links").fetchall():
            c.execute(
                "INSERT OR IGNORE INTO guild_links (guild_id, name, label, url) VALUES (?,?,?,?)",
                (gid, r["name"], r["label"], r["url"]),
            )
    if _has_table("timer_manual"):
        for r in c.execute("SELECT * FROM timer_manual").fetchall():
            c.execute(
                """INSERT OR IGNORE INTO guild_timer_manual
                   (guild_id, name, category, next_at, interval_min, replaces_at, respawn_min, set_by, set_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (gid, r["name"], r["category"], r["next_at"], r["interval_min"], r["replaces_at"],
                 r["respawn_min"], r["set_by"], r["set_at"]),
            )
    if _has_table("timer_subs"):
        for r in c.execute("SELECT user_id, kind, target, lead FROM timer_subs").fetchall():
            c.execute(
                "INSERT OR IGNORE INTO guild_timer_subs (guild_id, user_id, kind, target, lead) VALUES (?,?,?,?,?)",
                (gid, r["user_id"], r["kind"], r["target"], r["lead"]),
            )
    announce = config.ANNOUNCE_CHANNEL_ID
    board_c, board_m = meta_get("timer_board_channel"), meta_get("timer_board_message")
    c.execute(
        "UPDATE guild_settings SET announce_channel=COALESCE(?, announce_channel), board_channel=?, board_message=? WHERE guild_id=?",
        (int(announce) if announce.isdigit() else None,
         int(board_c) if board_c and board_c.isdigit() else None,
         int(board_m) if board_m and board_m.isdigit() else None, gid),
    )
    c.commit()
    meta_set("multiguild_v1", str(int(time.time())))


def seed_bosses(guild_id: int) -> None:
    """Prebuilt raids and world bosses, plus any world boss the site has listed."""
    c = conn()
    for kind, names in PREBUILT_BOSSES.items():
        for name in names:
            c.execute("INSERT OR IGNORE INTO guild_bosses (guild_id, kind, name) VALUES (?,?,?)", (guild_id, kind, name))
    for r in c.execute(
        "SELECT name FROM timer_catalog WHERE lower(category) LIKE '%world boss%' UNION SELECT name FROM boss_catalog"
    ).fetchall():
        c.execute("INSERT OR IGNORE INTO guild_bosses (guild_id, kind, name) VALUES (?,'world_boss',?)", (guild_id, r["name"]))
    c.commit()


def ensure_guild(guild_id: int, seed: bool = True) -> bool:
    """Make sure a server has its settings row and starting lists. True when it was just created."""
    c = conn()
    if c.execute("SELECT 1 FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone():
        return False
    c.execute("INSERT INTO guild_settings (guild_id, joined_at) VALUES (?,?)", (guild_id, int(time.time())))
    c.commit()
    if seed:
        seed_bosses(guild_id)
    return True


def forget_guild(guild_id: int) -> None:
    """The bot was removed from a server: drop that server's settings and lists."""
    c = conn()
    for table in ("guild_settings", "guild_bosses", "guild_links", "guild_timer_manual", "guild_timer_subs", "news_posted",
                  "dkp_config", "dkp_ledger", "dkp_rewards", "dkp_batches", "dkp_boards", "loot_items", "loot_reservations",
                  "member_classes"):
        c.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
    c.commit()


def guild_ids() -> list[int]:
    return [r["guild_id"] for r in conn().execute("SELECT guild_id FROM guild_settings").fetchall()]


SETTING_COLUMNS = ("announce_channel", "board_channel", "board_message", "timer_region", "game_server", "faction",
                   "news_channel", "news_region")


def get_setting(guild_id: int, column: str):
    assert column in SETTING_COLUMNS
    row = conn().execute(f"SELECT {column} FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
    return row[column] if row else None


def set_settings(guild_id: int, **values) -> None:
    ensure_guild(guild_id)
    for column in values:
        assert column in SETTING_COLUMNS
    sets = ", ".join(f"{k}=?" for k in values)
    conn().execute(f"UPDATE guild_settings SET {sets} WHERE guild_id=?", (*values.values(), guild_id))
    conn().commit()


def news_guilds() -> list[int]:
    """Servers that chose a news channel with /setup news."""
    return [r["guild_id"] for r in conn().execute("SELECT guild_id FROM guild_settings WHERE news_channel IS NOT NULL").fetchall()]


def announce_channels() -> list[tuple[int, int]]:
    rows = conn().execute("SELECT guild_id, announce_channel FROM guild_settings WHERE announce_channel IS NOT NULL").fetchall()
    return [(r["guild_id"], r["announce_channel"]) for r in rows]


def bosses(guild_id: int, kind: str, like: str = "") -> list[str]:
    ensure_guild(guild_id)
    rows = conn().execute(
        "SELECT name FROM guild_bosses WHERE guild_id=? AND kind=? AND name LIKE ? ORDER BY name LIMIT 25",
        (guild_id, kind, f"%{like}%"),
    ).fetchall()
    return [r["name"] for r in rows]
