"""Event timers: reading the gamers4.life schedule, storing it, and working out start times per server.

Also holds shared manual timers: corrections to scraped times and bosses the site doesn't list (field
bosses with their own respawns, for example). The /timers commands are in timer_commands.py and the timer
list embed and live board in timer_board.py.
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Optional
from zoneinfo import ZoneInfo

from discord import app_commands

import bosscatalog
import config
import db
import gamedata

log = logging.getLogger("aion2.timers")

# Every Global region runs the same schedule on its own local server clock. The page shows one
# region (and says which); the clock times in its table apply to each region's own timezone.
R_HEADING = re.compile(r"Full schedule\s*\(\s*server time(?:\s*[·|-]\s*([^)<]+?))?\s*\)(.*?)</", re.I | re.S)
R_CLOCK = re.compile(r"(?:UTC|GMT)\s*(?:([+-])\s*(\d{1,2})(?::(\d{2}))?)?", re.I)


def parse_page_meta(html: str) -> tuple[Optional[str], Optional[int]]:
    """Read the schedule heading, like "Full schedule (server time) · North America (East) · UTC-4".
    Returns (region name or None, that region's clock offset in minutes or None)."""
    m = R_HEADING.search(html)
    if not m:
        return None, None
    rest = re.sub(r"<!--.*?-->", "", m.group(2), flags=re.S)
    parts = [m.group(1) or ""] + re.sub(r"<[^>]+>", " ", rest).split("·")
    region, offset = [], None
    for part in (" ".join(p.split()).strip(" |-") for p in parts):
        tz = R_CLOCK.fullmatch(part)
        if tz:
            offset = 0
            if tz.group(1):
                offset = (int(tz.group(2)) * 60 + int(tz.group(3) or 0)) * (1 if tz.group(1) == "+" else -1)
        elif part:
            region.append(part)
    return " · ".join(region) or None, offset


def page_accepted(region: Optional[str]) -> bool:
    """Whether the page shows the schedule the bot wants (TIMERS_REGION, normally Global)."""
    want = config.TIMERS_REGION.strip().lower()
    if not want or not region:
        return True
    if want == "global":
        return region.lower() not in (r.lower() for r in gamedata.OTHER_REGIONS)
    return region.lower() == want


def region_key(guild_id: int) -> str:
    key = db.get_setting(guild_id, "timer_region") or config.TIMERS_DEFAULT_REGION
    return key if key in gamedata.REGIONS else "na"


def guild_tz(guild_id: int) -> ZoneInfo:
    """The server clock of the region this Discord server plays on (set with /setup region)."""
    return ZoneInfo(gamedata.REGIONS[region_key(guild_id)][2])


DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WINDOW_DAYS = 8
CATEGORY_CHOICES = [
    app_commands.Choice(name=n, value=n)
    for n in ("World Bosses", "Field Bosses", "Rifts", "Abyss", "Events", "Other")
]


# ---------------------------------------------------------------- parsing
class _Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_table(html: str) -> list[tuple[str, str, str]]:
    """Return (name, category, timing) for every schedule row on the page."""
    p = _Rows()
    p.feed(html)
    out, seen = [], set()
    for cells in p.rows:
        if len(cells) < 3:
            continue
        name, category, timing = cells[0], cells[1], cells[2]
        if category.lower() == "category" or not name or name in seen:
            continue
        seen.add(name)
        out.append((name, category, timing))
    return out


_DAY = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*"
R_FROM = re.compile(r"every\s+(\d+)\s*h(?:ours?)?\s+from\s+(\d{1,2}):(\d{2})", re.I)
R_HOURAT = re.compile(r"every\s+hour\s+at\s+:(\d{2})", re.I)
R_EVERY = re.compile(r"every\s+(\d+)\s*h(?:ours?)?$", re.I)
R_WEEKLY = re.compile(rf"^({_DAY}(?:\s*,\s*{_DAY})*)\s+at\s+(\d{{1,2}}):(\d{{2}})$", re.I)
R_DAILY = re.compile(r"^daily\s+at\s+(\d{1,2}:\d{2}(?:\s*,\s*\d{1,2}:\d{2})*)$", re.I)


def parse_rule(text: str) -> Optional[dict]:
    """Turn the timing text into a rule. Returns None when it isn't understood."""
    t = " ".join(text.split())
    m = R_FROM.search(t)
    if m:
        return {"type": "interval", "hours": int(m.group(1)), "h": int(m.group(2)), "m": int(m.group(3))}
    m = R_HOURAT.search(t)
    if m:
        return {"type": "interval", "hours": 1, "h": 0, "m": int(m.group(1))}
    m = R_EVERY.fullmatch(t)
    if m:
        return {"type": "interval", "hours": int(m.group(1)), "h": 0, "m": 0}
    m = R_WEEKLY.fullmatch(t)
    if m:
        days = sorted({DAYS[d.strip()[:3].lower()] for d in m.group(1).split(",")})
        return {"type": "weekly", "days": days, "h": int(m.group(2)), "m": int(m.group(3))}
    m = R_DAILY.fullmatch(t)
    if m:
        times = sorted({(int(h), int(mi)) for h, mi in (p.strip().split(":") for p in m.group(1).split(","))})
        return {"type": "daily", "times": [list(x) for x in times]}
    return None


def _day_times(rule: dict, weekday: int) -> list[tuple[int, int]]:
    """Clock times a rule starts at on one day of the server's calendar."""
    if rule["type"] == "daily":
        return [(h, m) for h, m in rule["times"]]
    if rule["type"] == "weekly":
        return [(rule["h"], rule["m"])] if weekday in rule["days"] else []
    step = rule["hours"] * 60
    return [divmod(x, 60) for x in range((rule["h"] * 60 + rule["m"]) % step, 1440, step)]


def occurrences(rule: dict, start: datetime, end: datetime, tz: timezone = timezone.utc) -> list[datetime]:
    """All start times (UTC) of a rule between start (inclusive) and end (exclusive).
    The rule's clock times are in tz, the timezone the page uses."""
    out: list[datetime] = []
    if rule["type"] == "interval" and 1440 % (rule["hours"] * 60):
        # doesn't fit a day evenly, so it can't follow the clock: count from a fixed start
        anchor = datetime(2026, 1, 5, rule["h"], rule["m"], tzinfo=tz)
        step = timedelta(hours=rule["hours"])
        k = max(0, -(-int((start - anchor).total_seconds()) // int(step.total_seconds())))
        t = anchor + k * step
        while t < end:
            out.append(t.astimezone(timezone.utc))
            t += step
        return out
    day = (start.astimezone(tz) - timedelta(days=1)).date()
    last = end.astimezone(tz).date()
    while day <= last:
        for h, m in _day_times(rule, day.weekday()):
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if start <= t < end:
                out.append(t.astimezone(timezone.utc))
        day += timedelta(days=1)
    return sorted(out)


# ---------------------------------------------------------------- storage: scraped timers
def tracked(category: str) -> bool:
    cats = [c.lower() for c in config.TIMER_CATEGORIES]
    return "*" in cats or category.lower() in cats


def store_rules(rows: list[tuple[str, str, str]], now_ts: Optional[int] = None):
    """Save the scraped schedule. Anything new to the catalog is remembered for good.

    Returns (tracked_count, newly_seen) where newly_seen is empty on the very first
    run, so the first fetch doesn't announce the whole schedule as "new".
    New world bosses are also added to every server's event planner boss list.
    """
    now_ts = now_ts or int(time.time())
    c = db.conn()
    first_run = c.execute("SELECT COUNT(*) AS n FROM timer_catalog").fetchone()["n"] == 0
    new = []
    for name, cat, timing in rows:
        existing = c.execute("SELECT 1 FROM timer_catalog WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if existing:
            c.execute(
                "UPDATE timer_catalog SET category=?, timing=?, last_seen=? WHERE name = ? COLLATE NOCASE",
                (cat, timing, now_ts, name),
            )
        else:
            c.execute(
                "INSERT INTO timer_catalog (name, category, timing, first_seen, last_seen) VALUES (?,?,?,?,?)",
                (name, cat, timing, now_ts, now_ts),
            )
            if "world boss" in cat.lower():
                for gid in db.guild_ids():
                    c.execute(
                        "INSERT OR IGNORE INTO guild_bosses (guild_id, kind, name) VALUES (?, 'world_boss', ?)", (gid, name)
                    )
            if not first_run:
                new.append((name, cat, timing))
    keep = [r for r in rows if tracked(r[1])]
    c.execute("DELETE FROM timer_rules")
    c.executemany("INSERT INTO timer_rules (name, category, timing) VALUES (?,?,?)", keep)
    c.commit()
    return len(keep), new


# ---------------------------------------------------------------- storage: manual timers (per server)
FIELD_RESPAWNS = gamedata.field_boss_respawns()  # built-in field bosses: timer name -> respawn minutes
FIELD_FACTIONS = gamedata.field_boss_factions()  # same timer names -> faction ("elyos"/"asmodian"/"both")
FIELD_CATEGORY, ABYSS_CATEGORY = "Field Bosses", "Abyss (contested)"


def field_respawn(name: str) -> Optional[int]:
    return next((mins for known, mins in FIELD_RESPAWNS.items() if known.lower() == name.lower()), None)


def canonical(guild_id: int, name: str):
    """Match a typed name to a known timer. Returns (name, category or None, scraped timing or None)."""
    c = db.conn()
    r = c.execute("SELECT name, category, timing FROM timer_rules WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if r:
        return r["name"], r["category"], r["timing"]
    m = c.execute(
        "SELECT name, category FROM guild_timer_manual WHERE guild_id=? AND name = ? COLLATE NOCASE", (guild_id, name)
    ).fetchone()
    if m:
        return m["name"], m["category"], None
    b = c.execute(
        "SELECT name FROM guild_bosses WHERE guild_id=? AND kind='world_boss' AND name = ? COLLATE NOCASE", (guild_id, name)
    ).fetchone()
    for known in list(FIELD_RESPAWNS) + gamedata.field_boss_names():
        if known.lower() == name.lower():
            return known, "Field Bosses", None
    row = bosscatalog.lookup(name)
    if row:
        return row["name"], bosscatalog.category(row), None
    if b:
        return b["name"], "World Bosses", None
    # Contested Abyss bosses aren't on the site schedule, the field-respawn list or the boss
    # catalog, so nothing above classifies them and the live kill feed couldn't time them. As a
    # fallback (it never overrides a classification above), match a fed name — which carries the
    # catalog spelling, e.g. "Argo, the Spirit King" — to the bundled Abyss list punctuation-
    # insensitively, so a fed kill can time an Abyss boss like a field boss timed from a kill.
    simple = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    for known, _zone, _sched in gamedata.ABYSS_BOSSES:
        if re.sub(r"[^a-z0-9]+", " ", known.lower()).strip() == simple:
            return known, ABYSS_CATEGORY, None
    return name, None, None


def scraped_next(timing: Optional[str], tz, now: Optional[datetime] = None) -> Optional[int]:
    """The next time the site's schedule would start this timer on a server clock."""
    rule = parse_rule(timing) if timing else None
    if not rule:
        return None
    now = now or datetime.now(timezone.utc)
    occ = occurrences(rule, now, now + timedelta(days=WINDOW_DAYS), tz)
    return int(occ[0].timestamp()) if occ else None


MAX_SHIFT_SEC = 12 * 3600


def nearest_site_time(timing: Optional[str], ts: int, tz) -> Optional[int]:
    """The site's start time (any day, past or future) closest to ts."""
    rule = parse_rule(timing) if timing else None
    if not rule:
        return None
    centre = datetime.fromtimestamp(ts, timezone.utc)
    occ = occurrences(rule, centre - timedelta(days=8), centre + timedelta(days=8), tz)
    return min((int(o.timestamp()) for o in occ), key=lambda x: abs(x - ts)) if occ else None


def save_manual(guild_id, name, category, next_at, interval_min, replaces_at, respawn_min, user_id, shift_sec=None) -> None:
    db.ensure_guild(guild_id)
    c = db.conn()
    c.execute(
        """INSERT INTO guild_timer_manual
               (guild_id, name, category, next_at, interval_min, replaces_at, respawn_min, set_by, set_at, shift_sec)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(guild_id, name) DO UPDATE SET
               category=excluded.category, next_at=excluded.next_at,
               interval_min=excluded.interval_min, replaces_at=excluded.replaces_at,
               respawn_min=COALESCE(excluded.respawn_min, guild_timer_manual.respawn_min),
               set_by=excluded.set_by, set_at=excluded.set_at, shift_sec=excluded.shift_sec""",
        (guild_id, name, category, next_at, interval_min, replaces_at, respawn_min, user_id, int(time.time()), shift_sec),
    )
    c.commit()


def clear_manual(guild_id: int, name: str) -> None:
    db.conn().execute("DELETE FROM guild_timer_manual WHERE guild_id=? AND name = ? COLLATE NOCASE", (guild_id, name))
    db.conn().commit()


def manual_times(m, now_ts: int, end_ts: int) -> list[int]:
    nxt, iv = m["next_at"], m["interval_min"]
    if not iv:
        return [nxt] if now_ts <= nxt < end_ts else []
    step = iv * 60
    t = nxt if nxt >= now_ts else nxt + -(-(now_ts - nxt) // step) * step
    out = []
    while t < end_ts:
        out.append(t)
        t += step
    return out


def events_between(guild_id: int, start_ts: int, end_ts: int) -> list[tuple[str, str, int]]:
    """Every (name, category, start) in the window for one server: the site's schedule on
    that server's region clock, with its manual timers layered on top.

    A manual timer can be a "shift": every start the site lists for that name is moved by
    the same amount, so the override survives restarts and refreshes of the site schedule."""
    c = db.conn()
    tz = guild_tz(guild_id)
    manual = {
        m["name"].lower(): m
        for m in c.execute("SELECT * FROM guild_timer_manual WHERE guild_id=?", (guild_id,)).fetchall()
    }
    out = []
    for r in c.execute("SELECT name, category, timing FROM timer_rules").fetchall():
        rule = parse_rule(r["timing"])
        m = manual.get(r["name"].lower())
        if not rule or (m and m["interval_min"]):
            continue
        sh = m["shift_sec"] if m and m["shift_sec"] is not None else 0
        a = datetime.fromtimestamp(start_ts - sh, timezone.utc)
        b = datetime.fromtimestamp(end_ts - sh, timezone.utc)
        for t in occurrences(rule, a, b, tz):
            ts = int(t.timestamp())
            if m and m["shift_sec"] is None and ts == m["replaces_at"]:
                continue
            out.append((r["name"], r["category"], ts + sh))
    for m in manual.values():
        if m["shift_sec"] is None:
            for t in manual_times(m, start_ts, end_ts):
                out.append((m["name"], m["category"], t))
    return out


def last_start(guild_id: int, name: str, now_ts: int, days: int) -> Optional[int]:
    """The most recent start of a timer at or before now, looking back this many days."""
    times = [t for n, _c, t in events_between(guild_id, now_ts - days * 86400, now_ts + 1) if n.lower() == name.lower()]
    return max(times) if times else None


def next_start(guild_id: int, name: str, now_ts: int) -> Optional[int]:
    times = [t for n, _c, t in events_between(guild_id, now_ts + 1, now_ts + WINDOW_DAYS * 86400) if n.lower() == name.lower()]
    return min(times) if times else None


def purge_old(now: Optional[datetime] = None) -> None:
    """Forget notification marks and one-off manual timers that are long past."""
    now = now or datetime.now(timezone.utc)
    c = db.conn()
    cutoff = int((now - timedelta(days=config.TIMER_RETENTION_DAYS)).timestamp())
    c.execute("DELETE FROM timer_notified WHERE starts_at < ?", (cutoff,))
    c.execute(
        "DELETE FROM guild_timer_manual WHERE interval_min IS NULL AND shift_sec IS NULL AND next_at < ?", (int(now.timestamp()) - 30 * 86400,)
    )
    c.commit()


def all_names(guild_id: int) -> list[str]:
    rows = db.conn().execute(
        "SELECT name FROM timer_rules UNION SELECT name FROM guild_timer_manual WHERE guild_id=? ORDER BY name", (guild_id,)
    ).fetchall()
    return [r["name"] for r in rows]


def all_categories(guild_id: int) -> list[str]:
    rows = db.conn().execute(
        """SELECT DISTINCT category FROM (SELECT category FROM timer_rules
           UNION SELECT category FROM guild_timer_manual WHERE guild_id=?) ORDER BY category""",
        (guild_id,),
    ).fetchall()
    # The built-in field-boss sections always exist even without a stored row for them.
    return sorted({r["category"] for r in rows} | {FIELD_CATEGORY, ABYSS_CATEGORY})
