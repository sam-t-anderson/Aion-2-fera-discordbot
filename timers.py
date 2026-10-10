"""Event timers scraped from gamers4.life, cached locally, shown per person.

Also holds shared manual timers: corrections to scraped times and bosses the site
doesn't list (field bosses with their own respawns, for example).
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import bosscatalog
import config
import db
import gamedata
import util

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
    return [r["category"] for r in rows]


def _add_chunked(embed: discord.Embed, name: str, lines: list[str]) -> None:
    chunk, size, first = [], 0, True
    for line in lines:
        if chunk and size + len(line) + 1 > 1000:
            embed.add_field(name=name if first else f"{name} (cont.)", value="\n".join(chunk), inline=False)
            first, chunk, size = False, [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        embed.add_field(name=name if first else f"{name} (cont.)", value="\n".join(chunk), inline=False)


def _fmt_minutes(mins: int) -> str:
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "") if p) or "0m"


def build_embed(guild_id: int, category: Optional[str] = None, max_chars: Optional[int] = None) -> Optional[discord.Embed]:
    """The timer list for one server as an embed, or None when there is nothing to show.

    max_chars trims the soonest-last lines so the embed fits Discord's 6000 character cap."""
    now = int(time.time())
    c = db.conn()
    upcoming: dict[str, int] = {}
    for name, _cat, start in events_between(guild_id, now, now + WINDOW_DAYS * 86400):
        key = name.lower()
        if key not in upcoming or start < upcoming[key]:
            upcoming[key] = start
    manual = {
        m["name"].lower(): m
        for m in c.execute("SELECT * FROM guild_timer_manual WHERE guild_id=?", (guild_id,)).fetchall()
    }
    rows = []
    for r in c.execute("SELECT name, category, timing FROM timer_rules").fetchall():
        rows.append({"name": r["name"], "category": r["category"], "timing": r["timing"]})
    known = {r["name"].lower() for r in rows}
    for key, m in manual.items():
        if key not in known:
            rows.append({"name": m["name"], "category": m["category"], "timing": None})
    for r in rows:
        m = manual.get(r["name"].lower())
        r["nxt"] = upcoming.get(r["name"].lower())
        r["set_by"], r["iv"], r["mnext"] = (m["set_by"], m["interval_min"], m["next_at"]) if m else (None, None, None)
    rows = [r for r in rows if not (r["timing"] is None and r["nxt"] is None)]  # hide finished manual timers
    if category:
        rows = [r for r in rows if r["category"].lower() == category.lower()]
    if not rows:
        return None
    last_ok = db.meta_get("timers_last_ok")
    embed = discord.Embed(title="AION 2 event timers", color=0xA9741A)
    embed.description = (
        f"Times show in your own timezone. {gamedata.REGIONS[region_key(guild_id)][0]} schedule from gamers4.life"
        + (f" · updated {util.ts(int(last_ok), 'R')}" if last_ok else "")
        + "\nWrong or missing? Fix it with `/timers set` or `/timers killed`. Wrong region? `/setup region` (admins)."
    )
    budget = (max_chars or 5900) - len(embed.title) - len(embed.description) - 120
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    hidden = 0
    for cat in sorted(by_cat):
        items = sorted(by_cat[cat], key=lambda r: (r["nxt"] is None, r["nxt"] or 0))
        lines = []
        for r in items:
            timing = r["timing"] or (f"every {_fmt_minutes(r['iv'])} (manual)" if r["iv"] else "set manually")
            edited = ""
            if r["set_by"] and r["mnext"] and (r["iv"] or r["mnext"] >= now):
                edited = f" · set by <@{r['set_by']}>"
            if r["nxt"]:
                line = f"**{r['name']}**: {util.ts(r['nxt'], 'R')} ({util.ts(r['nxt'], 't')}) · {timing}{edited}"
            else:
                line = f"**{r['name']}**: no upcoming time found · {timing}"
            line = util.cut(line, 300)
            if budget - len(line) - 1 < 0:
                hidden += 1
                continue
            budget -= len(line) + 1
            lines.append(line)
        if lines:
            budget -= len(cat) + 10
            _add_chunked(embed, cat, lines)
    if hidden:
        embed.set_footer(text=f"{hidden} more not shown. Use /timers show to see everything.")
    return embed


# ---------------------------------------------------------------- cog
class Timers(commands.GroupCog, group_name="timers", group_description="AION 2 event timers"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        super().__init__()

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self.refresh_loop.change_interval(minutes=config.TIMER_REFRESH_MINUTES)
        self.refresh_loop.start()
        self.notify_loop.start()

    async def cog_unload(self):
        self.refresh_loop.cancel()
        self.notify_loop.cancel()
        if self.session:
            await self.session.close()

    # ---- scraping
    async def fetch_and_store(self) -> str:
        db.meta_set("timers_last_attempt", str(int(time.time())))
        headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html"}
        etag = db.meta_get("timers_etag")
        if etag:
            headers["If-None-Match"] = etag
        try:
            async with self.session.get(
                config.TIMERS_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status == 304:
                    status, body = "unchanged", None
                elif resp.status == 200:
                    status, body = "ok", await resp.text()
                    if resp.headers.get("ETag"):
                        db.meta_set("timers_etag", resp.headers["ETag"])
                else:
                    raise RuntimeError(f"HTTP {resp.status}")
        except Exception as e:
            db.meta_set("timers_last_error", f"{type(e).__name__}: {e}")
            log.warning("Timer fetch failed: %s", e)
            return "error"

        new: list = []
        if body is not None:
            rows = parse_table(body)
            region, _offset = parse_page_meta(body)
            if not page_accepted(region):
                db.meta_delete("timers_etag")
                db.meta_set(
                    "timers_last_error",
                    f"The page is showing the {region} schedule, not {config.TIMERS_REGION}. Kept the last good schedule.",
                )
                log.warning("Timers page shows region %r, wanted %r; ignoring it", region, config.TIMERS_REGION)
                return "error"
            region_label = config.TIMERS_REGION
            region_changed = db.meta_get("timers_region") != region_label
            kept = 0
            if rows:
                kept, new = store_rules(rows)
                db.meta_set("timers_region", region_label)
                if region_changed:
                    new = []  # first read of this region: don't announce its whole schedule as new
            if not rows or not kept:
                db.meta_set(
                    "timers_last_error",
                    "The events page loaded but no schedule rows were found. The site layout may have changed.",
                )
                log.warning("Timer page parsed but no usable rows (%d raw rows)", len(rows))
                return "error"
        purge_old()
        db.meta_set("timers_last_ok", str(int(time.time())))
        db.meta_set("timers_last_error", "")
        n_rules = db.conn().execute("SELECT COUNT(*) AS n FROM timer_rules").fetchone()["n"]
        log.info("Timers %s: %d scheduled timers tracked", status, n_rules)
        if new:
            await self._announce_new(new)
        return status

    async def _announce_new(self, new: list) -> None:
        lines = [f"**{n}** ({c}) · {t}" for n, c, t in new]
        log.info("New timers spotted: %s", "; ".join(f"{n} ({c})" for n, c, _ in new))
        db.meta_set("timers_last_new", f"{int(time.time())}|" + ", ".join(n for n, _, _ in new))
        text = util.cut("New on the AION 2 event schedule, now tracked:\n" + "\n".join(lines), 1900)
        for gid, cid in db.announce_channels():
            try:
                channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                await channel.send(text)
            except discord.HTTPException:
                log.info("Could not announce new timers in server %s", gid)

    @tasks.loop(minutes=15)
    async def refresh_loop(self):
        try:
            await self.fetch_and_store()
        except Exception:
            log.exception("Timer refresh error")

    @refresh_loop.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    # ---- notifications
    @tasks.loop(seconds=20)
    async def notify_loop(self):
        try:
            now = int(time.time())
            c = db.conn()
            by_guild: dict[int, list] = {}
            for s in c.execute("SELECT * FROM guild_timer_subs").fetchall():
                by_guild.setdefault(s["guild_id"], []).append(s)
            for gid, subs in by_guild.items():
                window = events_between(gid, now - 60, now + 11 * 60)
                for s in subs:
                    for name, cat, start in window:
                        hit = (
                            name.lower() == s["target"].lower()
                            if s["kind"] == "name"
                            else cat.lower() == s["target"].lower()
                        )
                        if not hit or not (start - s["lead"] * 60 <= now < start + 60):
                            continue
                        done = c.execute(
                            "SELECT 1 FROM timer_notified WHERE user_id=? AND name=? AND starts_at=?",
                            (s["user_id"], name, start),
                        ).fetchone()
                        if done:
                            continue
                        c.execute(
                            "INSERT OR IGNORE INTO timer_notified (user_id, name, starts_at) VALUES (?,?,?)",
                            (s["user_id"], name, start),
                        )
                        c.commit()
                        text = f"**{name}** ({cat}) starts {util.ts(start, 'R')} ({util.ts(start, 't')})."
                        try:
                            user = self.bot.get_user(s["user_id"]) or await self.bot.fetch_user(s["user_id"])
                            await user.send(text)
                        except discord.HTTPException:
                            log.info("Could not DM user %s (DMs closed?)", s["user_id"])
        except Exception:
            log.exception("Timer notify error")

    @notify_loop.before_loop
    async def _before_notify(self):
        await self.bot.wait_until_ready()

    # ---- commands: viewing
    @app_commands.command(name="show", description="When the next rifts, bosses and events start (your time)")
    @app_commands.describe(category="Only show one category")
    async def show(self, interaction: discord.Interaction, category: Optional[str] = None):
        embed = build_embed(interaction.guild_id, category)
        if embed is None:
            msg = "No timer data yet. The first fetch happens shortly after the bot starts, so try again in a minute."
            if category:
                msg = f"No timers found for **{category}**."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @show.autocomplete("category")
    async def _show_ac(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=c, value=c) for c in all_categories(interaction.guild_id) if current.lower() in c.lower()][:25]

    # ---- commands: manual timers
    @staticmethod
    def _name_choices(guild_id: int, current: str):
        db.ensure_guild(guild_id)
        names = all_names(guild_id) + sorted(FIELD_RESPAWNS) + bosscatalog.names()   # this server's timers first, then every known boss
        names += db.bosses(guild_id, "world_boss", current)
        seen, out = set(), []
        for n in names:
            if current.lower() in n.lower() and n.lower() not in seen:
                seen.add(n.lower())
                out.append(app_commands.Choice(name=n[:100], value=n[:100]))
        return out[:25]

    @app_commands.command(name="set", description="Set or fix when a boss or event starts (shared with everyone)")
    @app_commands.describe(
        name="Boss or event name",
        when="Next start: 8:45pm, tomorrow 6am, in 2h",
        repeat="Optional: it repeats every this long, like 4h or 90m",
        category="Category if this is a new timer",
        only_this_time="Only change this one start and go back to the site's times after it",
    )
    @app_commands.choices(category=CATEGORY_CHOICES)
    async def set_timer(
        self,
        interaction: discord.Interaction,
        name: str,
        when: str,
        repeat: Optional[str] = None,
        category: Optional[str] = None,
        only_this_time: bool = False,
    ):
        name = " ".join(name.split())[:60]
        if not name:
            await interaction.response.send_message("Give the timer a name.", ephemeral=True)
            return
        try:
            starts = util.parse_when(when, util.get_tz(interaction.user.id))
            iv = None
            if repeat:
                iv = int(util.parse_duration(repeat).total_seconds() // 60)
                if not 5 <= iv <= 43200:
                    raise ValueError("Repeat must be between 5 minutes and 30 days.")
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        canon, cat, timing = canonical(interaction.guild_id, name)
        ts = int(starts.timestamp())
        site = nearest_site_time(timing, ts, guild_tz(interaction.guild_id))
        shift = None
        if site is not None and not iv and not only_this_time and abs(ts - site) <= MAX_SHIFT_SEC:
            shift = ts - site                      # keep the site's pattern, just moved by this much
        replaces = None if iv else site
        save_manual(interaction.guild_id, canon, cat or category or "Field Bosses", ts, iv, replaces, None, interaction.user.id, shift)
        if shift is not None:
            how = "exactly on the site's time" if shift == 0 else f"{_fmt_minutes(abs(shift) // 60) if abs(shift) >= 60 else 'seconds'} {'later' if shift > 0 else 'earlier'} than the site"
            msg = (
                f"Saved. **{canon}** now starts {util.ts(starts, 'F')} ({util.ts(starts, 'R')}), {how}. "
                "Every other start moves by the same amount and stays that way through restarts and site refreshes. "
                f"Use `/timers clear` to go back to the site's times, or add `only_this_time:True` to change just one start."
            )
        else:
            more = f" and every {_fmt_minutes(iv)} after that" if iv else ""
            note = ""
            if site is not None and not iv and not only_this_time:
                note = " That's more than 12 hours from the site's time, so I changed only this one start."
            msg = (
                f"Saved. **{canon}** is set for {util.ts(starts, 'F')} ({util.ts(starts, 'R')}){more}.{note} "
                "Everyone sees it in `/timers show`, and people who used `/timers notify` get their DM."
            )
        await interaction.response.send_message(msg, ephemeral=True)

    @set_timer.autocomplete("name")
    async def _set_ac(self, interaction: discord.Interaction, current: str):
        return self._name_choices(interaction.guild_id, current)

    @app_commands.command(name="killed", description="A boss just died: start its respawn timer from now")
    @app_commands.describe(name="Boss name", respawn="Respawn time, like 4h or 90m. Remembered; known field bosses don't need it")
    async def killed(self, interaction: discord.Interaction, name: str, respawn: Optional[str] = None):
        name = " ".join(name.split())[:60]
        canon, cat, timing = canonical(interaction.guild_id, name)
        row = db.conn().execute(
            "SELECT respawn_min FROM guild_timer_manual WHERE guild_id=? AND name = ? COLLATE NOCASE",
            (interaction.guild_id, canon),
        ).fetchone()
        try:
            if respawn:
                mins = int(util.parse_duration(respawn).total_seconds() // 60)
            elif row and row["respawn_min"]:
                mins = row["respawn_min"]
            elif field_respawn(canon):
                mins = field_respawn(canon)
            else:
                zones = (bosscatalog.lookup(canon) or {"zones": ""})["zones"] or ""
                hint = " It respawns at a different speed in each zone, or pick the one with your zone in its name." if "," in zones and any(k.startswith(canon + " (") for k in FIELD_RESPAWNS) else ""
                raise ValueError(f"How long does it take to respawn? Add it like `respawn:4h`. I'll remember it for next time.{hint}")
            if not 1 <= mins <= 43200:
                raise ValueError("Respawn must be between 1 minute and 30 days.")
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        starts = int(time.time()) + mins * 60
        save_manual(interaction.guild_id, canon, cat or "Field Bosses", starts, None, nearest_site_time(timing, starts, guild_tz(interaction.guild_id)), mins, interaction.user.id)
        where = bosscatalog.describe(canon)
        await interaction.response.send_message(
            f"**{canon}** respawns {util.ts(starts, 'R')} ({util.ts(starts, 't')}), {_fmt_minutes(mins)} from now."
            + (f"\n{where}" if where else ""),
            ephemeral=True,
        )

    @killed.autocomplete("name")
    async def _killed_ac(self, interaction: discord.Interaction, current: str):
        return self._name_choices(interaction.guild_id, current)

    @app_commands.command(name="clear", description="Remove a manual time and go back to the site's schedule")
    @app_commands.describe(name="The timer to reset")
    async def clear(self, interaction: discord.Interaction, name: str):
        row = db.conn().execute(
            "SELECT * FROM guild_timer_manual WHERE guild_id=? AND name = ? COLLATE NOCASE",
            (interaction.guild_id, name.strip()),
        ).fetchone()
        if not row:
            await interaction.response.send_message("There's no manual time saved for that name.", ephemeral=True)
            return
        if row["set_by"] != interaction.user.id and not util.is_admin(interaction):
            await interaction.response.send_message(
                f"Only <@{row['set_by']}> or a server manager can clear that. You can overwrite it with `/timers set`.",
                ephemeral=True,
            )
            return
        clear_manual(interaction.guild_id, row["name"])
        await interaction.response.send_message(f"Cleared **{row['name']}**.", ephemeral=True)

    @clear.autocomplete("name")
    async def _clear_ac(self, interaction: discord.Interaction, current: str):
        rows = db.conn().execute(
            "SELECT name FROM guild_timer_manual WHERE guild_id=? AND name LIKE ? ORDER BY name LIMIT 25",
            (interaction.guild_id, f"%{current}%"),
        ).fetchall()
        return [app_commands.Choice(name=r["name"][:100], value=r["name"][:100]) for r in rows]

    # ---- commands: notifications
    @app_commands.command(name="notify", description="DM me when a timer starts")
    @app_commands.describe(target="An event or a whole category", lead="How early to notify you")
    @app_commands.choices(
        lead=[
            app_commands.Choice(name="When it starts", value=0),
            app_commands.Choice(name="1 minute before", value=1),
            app_commands.Choice(name="3 minutes before", value=3),
            app_commands.Choice(name="5 minutes before", value=5),
            app_commands.Choice(name="10 minutes before", value=10),
        ]
    )
    async def notify(self, interaction: discord.Interaction, target: str, lead: int = 5):
        cats = {c.lower(): c for c in all_categories(interaction.guild_id)}
        names = {n.lower(): n for n in all_names(interaction.guild_id)}
        key = target.strip().lower()
        if key in cats:
            kind, value = "category", cats[key]
        elif key in names:
            kind, value = "name", names[key]
        else:
            await interaction.response.send_message(
                "I don't know that timer. Start typing and pick one from the list.", ephemeral=True
            )
            return
        db.conn().execute(
            """INSERT INTO guild_timer_subs (guild_id, user_id, kind, target, lead) VALUES (?,?,?,?,?)
               ON CONFLICT(guild_id, user_id, kind, target) DO UPDATE SET lead=excluded.lead""",
            (interaction.guild_id, interaction.user.id, kind, value, lead),
        )
        db.conn().commit()
        when = "when it starts" if lead == 0 else f"{lead} minute{'s' if lead != 1 else ''} before"
        extra = " Some categories start every hour, so this can send a lot of DMs." if kind == "category" else ""
        await interaction.response.send_message(
            f"I'll DM you {when} for **{value}**.{extra} Make sure server members are allowed to DM you.",
            ephemeral=True,
        )

    @notify.autocomplete("target")
    async def _target_ac(self, interaction: discord.Interaction, current: str):
        out = [
            app_commands.Choice(name=f"All {c}", value=c)
            for c in all_categories(interaction.guild_id)
            if current.lower() in c.lower() or current.lower() in f"all {c}".lower()
        ]
        out += [
            app_commands.Choice(name=n[:100], value=n[:100]) for n in all_names(interaction.guild_id) if current.lower() in n.lower()
        ]
        return out[:25]

    @app_commands.command(name="unnotify", description="Stop DMs for a timer")
    @app_commands.describe(target="The event or category to stop")
    async def unnotify(self, interaction: discord.Interaction, target: str):
        cur = db.conn().execute(
            "DELETE FROM guild_timer_subs WHERE guild_id=? AND user_id=? AND lower(target)=lower(?)",
            (interaction.guild_id, interaction.user.id, target.strip()),
        )
        db.conn().commit()
        await interaction.response.send_message(
            "Stopped." if cur.rowcount else "You weren't subscribed to that.", ephemeral=True
        )

    @unnotify.autocomplete("target")
    async def _unnotify_ac(self, interaction: discord.Interaction, current: str):
        rows = db.conn().execute(
            "SELECT target FROM guild_timer_subs WHERE guild_id=? AND user_id=? AND target LIKE ? LIMIT 25",
            (interaction.guild_id, interaction.user.id, f"%{current}%"),
        ).fetchall()
        return [app_commands.Choice(name=r["target"][:100], value=r["target"][:100]) for r in rows]

    @app_commands.command(name="mine", description="List the timer DMs you signed up for")
    async def mine(self, interaction: discord.Interaction):
        rows = db.conn().execute(
            "SELECT kind, target, lead FROM guild_timer_subs WHERE guild_id=? AND user_id=? ORDER BY target",
            (interaction.guild_id, interaction.user.id),
        ).fetchall()
        if not rows:
            await interaction.response.send_message("No timer DMs set up. Use `/timers notify`.", ephemeral=True)
            return
        lines = [
            f"**{r['target']}**{' (all)' if r['kind'] == 'category' else ''}: "
            + ("when it starts" if r["lead"] == 0 else f"{r['lead']} min before")
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="status", description="Show when the timers were last refreshed")
    async def status(self, interaction: discord.Interaction):
        ok = db.meta_get("timers_last_ok")
        attempt = db.meta_get("timers_last_attempt")
        err = db.meta_get("timers_last_error")
        new = db.meta_get("timers_last_new")
        c = db.conn()
        n_rules = c.execute("SELECT COUNT(*) AS n FROM timer_rules").fetchone()["n"]
        n_catalog = c.execute("SELECT COUNT(*) AS n FROM timer_catalog").fetchone()["n"]
        n_manual = c.execute(
            "SELECT COUNT(*) AS n FROM guild_timer_manual WHERE guild_id=?", (interaction.guild_id,)
        ).fetchone()["n"]
        lines = [
            f"Last successful refresh: {util.ts(int(ok), 'R') if ok else 'never'}",
            f"Last attempt: {util.ts(int(attempt), 'R') if attempt else 'never'}",
            f"Tracking {n_rules} scheduled timers ({n_catalog} ever seen on the site) and {n_manual} manual timers.",
            f"Refreshes every {config.TIMER_REFRESH_MINUTES} minutes from gamers4.life. Anything new on the site is tracked automatically.",
            f"This server follows the {gamedata.REGIONS[region_key(interaction.guild_id)][1]} server clock"
            " (`/setup region` changes it, admins).",
        ]
        n_bosses = c.execute("SELECT COUNT(*) AS n FROM boss_catalog").fetchone()["n"]
        b_ok, b_err = db.meta_get("bosses_last_ok"), db.meta_get("bosses_last_error")
        if n_bosses:
            lines.append(
                f"Boss list: {n_bosses} open-world bosses from [aion2data.com](<https://aion2data.com/bosses>)"
                + (f" (client {db.meta_get('bosses_version')})" if db.meta_get("bosses_version") else "")
                + (f", checked {util.ts(int(b_ok), 'R')}" if b_ok else "")
                + ". Respawn times aren't in the game files, so they come from kill reports."
            )
        if b_err:
            lines.append(f"Boss list problem: {b_err}")
        if new and "|" in new:
            stamp, names = new.split("|", 1)
            lines.append(f"Last new timer spotted {util.ts(int(stamp), 'R')}: {util.cut(names, 300)}")
        if err:
            lines.append(f"Last problem: {err}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------- live board (one per server)
class TimerBoard(commands.Cog):
    """/timer-setup posts one timer board per server that the bot keeps editing."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_payload: dict[int, str] = {}

    async def cog_load(self):
        self.board_loop.change_interval(minutes=config.TIMER_BOARD_MINUTES)
        self.board_loop.start()

    async def cog_unload(self):
        self.board_loop.cancel()

    def _board_embed(self, guild_id: int) -> discord.Embed:
        embed = build_embed(guild_id, max_chars=5900)
        if embed is None:
            embed = discord.Embed(
                title="AION 2 event timers",
                description="No timer data yet. It appears after the first fetch, usually within a minute.",
                color=0xA9741A,
            )
        embed.set_footer(
            text=(embed.footer.text + " · " if embed.footer and embed.footer.text else "")
            + f"Board refreshes every {config.TIMER_BOARD_MINUTES} min"
        )
        return embed

    async def _message(self, guild_id: int):
        cid, mid = db.get_setting(guild_id, "board_channel"), db.get_setting(guild_id, "board_message")
        if not cid or not mid:
            return None
        channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
        return channel.get_partial_message(mid)

    def _forget(self, guild_id: int):
        db.set_settings(guild_id, board_channel=None, board_message=None)
        self._last_payload.pop(guild_id, None)

    @tasks.loop(minutes=2)
    async def board_loop(self):
        rows = db.conn().execute(
            "SELECT guild_id FROM guild_settings WHERE board_channel IS NOT NULL AND board_message IS NOT NULL"
        ).fetchall()
        for row in rows:
            gid = row["guild_id"]
            try:
                msg = await self._message(gid)
                if msg is None:
                    continue
                embed = self._board_embed(gid)
                payload = repr(embed.to_dict())
                if payload == self._last_payload.get(gid):
                    continue  # nothing changed, skip the edit
                await msg.edit(embed=embed)
                self._last_payload[gid] = payload
            except (discord.NotFound, discord.Forbidden):
                log.warning("Timer board in server %s is gone or not allowed; stopped updating it.", gid)
                self._forget(gid)
            except discord.HTTPException:
                log.exception("Timer board edit failed in server %s, will retry", gid)
            except Exception:
                log.exception("Timer board error in server %s", gid)

    @board_loop.before_loop
    async def _before_board(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="timer-setup", description="Post a live timer board in this channel (admins)")
    @app_commands.describe(stop="Remove the board and stop updating it")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def timer_setup(self, interaction: discord.Interaction, stop: bool = False):
        if not util.is_admin(interaction):
            await interaction.response.send_message("Only server managers can do that.", ephemeral=True)
            return
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)
        try:
            old = await self._message(gid)
        except discord.HTTPException:
            old = None
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        if stop:
            self._forget(gid)
            await interaction.followup.send("Timer board removed.", ephemeral=True)
            return
        try:
            message = await interaction.channel.send(embed=self._board_embed(gid))
        except discord.HTTPException:
            await interaction.followup.send(
                "I couldn't post here. Give me View Channel, Send Messages and Embed Links in this channel.",
                ephemeral=True,
            )
            return
        db.set_settings(gid, board_channel=message.channel.id, board_message=message.id)
        self._last_payload.pop(gid, None)
        await interaction.followup.send(
            f"Board posted. It updates every {config.TIMER_BOARD_MINUTES} minutes (the countdowns themselves tick live). "
            "Run `/timer-setup` in another channel to move it, or `/timer-setup stop:True` to remove it.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Timers(bot))
    await bot.add_cog(TimerBoard(bot))
