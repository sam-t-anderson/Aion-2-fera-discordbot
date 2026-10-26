"""Open-world boss names, levels and locations, read daily from aion2data.com's JSON feed
(built from the Global client's files). The built-in list in gamedata.py is the fallback.

The feed has no respawn times: the game client doesn't contain them."""

import json
import logging
import time
from typing import Optional

import aiohttp
from discord.ext import commands, tasks

import config
import db
import heartbeat

log = logging.getLogger("aion2.bosses")

# Maps whose bosses stand in the open world (everything else in the feed is a dungeon or raid).
OPEN_WORLD_MAPS = (
    "Verteron", "Altgard", "Eltnen", "Morheim", "Chaotic Lower Reshanta", "Chaotic Middle Reshanta",
)
REFRESH_SECONDS = 24 * 3600


def parse_feed(data: dict) -> list[dict]:
    """The open-world bosses in the feed as {name, level, zones, areas, url}."""
    out, seen = [], set()
    for r in data.get("records") or []:
        if not isinstance(r, dict) or not r.get("boss") or not isinstance(r.get("name"), str):
            continue
        spawns = [s for s in r.get("spawns") or [] if isinstance(s, dict) and s.get("map") in OPEN_WORLD_MAPS]
        name = " ".join(r["name"].split())[:60]
        if not spawns or not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        level = r.get("level")
        zones = sorted({s["map"] for s in spawns})
        areas = [f"{s['map']}: {s['area']}" if len(zones) > 1 else str(s["area"]) for s in spawns if s.get("area")]
        out.append({
            "name": name,
            "level": max(level) if isinstance(level, list) and level else None,
            "zones": ", ".join(zones),
            "areas": "; ".join(areas[:3]) + (f" (+{len(areas) - 3} more)" if len(areas) > 3 else ""),
            "url": r.get("url") if isinstance(r.get("url"), str) else None,
        })
    return out


def store(bosses: list[dict], now_ts: Optional[int] = None) -> list[str]:
    """Save the catalog. New names also join every server's event planner boss list. Returns the new names."""
    now_ts = now_ts or int(time.time())
    c = db.conn()
    new = []
    for b in bosses:
        if not c.execute("SELECT 1 FROM boss_catalog WHERE name = ?", (b["name"],)).fetchone():
            new.append(b["name"])
            for gid in db.guild_ids():
                c.execute("INSERT OR IGNORE INTO guild_bosses (guild_id, kind, name) VALUES (?, 'world_boss', ?)", (gid, b["name"]))
        c.execute(
            """INSERT INTO boss_catalog (name, level, zones, areas, url, last_seen) VALUES (?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET level=excluded.level, zones=excluded.zones, areas=excluded.areas,
                   url=excluded.url, last_seen=excluded.last_seen""",
            (b["name"], b["level"], b["zones"], b["areas"], b["url"], now_ts),
        )
    c.commit()
    return new


def lookup(name: str):
    """The catalog row for a boss name (any capitalisation), or None."""
    return db.conn().execute("SELECT * FROM boss_catalog WHERE name = ?", (" ".join(name.split()),)).fetchone()


def names() -> list[str]:
    return [r["name"] for r in db.conn().execute("SELECT name FROM boss_catalog ORDER BY name").fetchall()]


def category(row) -> str:
    return "World Bosses" if "Reshanta" in (row["zones"] or "") else "Field Bosses"


def describe(name: str) -> str:
    """A short "Lv 48, Verteron: Red Forest" for a boss, or an empty string."""
    row = lookup(name.split(" (")[0]) or lookup(name)
    if not row:
        return ""
    parts = [f"Lv {row['level']}" if row["level"] else "", row["areas"] if ":" in (row["areas"] or "") else
             ": ".join(p for p in (row["zones"], row["areas"]) if p)]
    return ", ".join(p for p in parts if p)


class BossCatalog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self.refresh_loop.start()

    async def cog_unload(self):
        self.refresh_loop.cancel()
        if self.session:
            await self.session.close()

    async def fetch_and_store(self) -> str:
        if not config.BOSS_DATA_URL:
            return "off"
        headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
        etag = db.meta_get("bosses_etag")
        if etag:
            headers["If-None-Match"] = etag
        db.meta_set("bosses_last_attempt", str(int(time.time())))
        try:
            async with self.session.get(
                config.BOSS_DATA_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 304:
                    db.meta_set("bosses_last_ok", str(int(time.time())))
                    return "unchanged"
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = json.loads(await resp.text())
                new_etag = resp.headers.get("ETag")
            bosses = parse_feed(data) if isinstance(data, dict) else []
            if len(bosses) < 20:
                raise RuntimeError(f"only {len(bosses)} bosses found; the feed layout may have changed")
        except Exception as e:
            db.meta_set("bosses_last_error", f"{type(e).__name__}: {e}")
            log.warning("Boss list fetch failed, keeping the list I have: %s", e)
            return "error"
        new = store(bosses)
        if new_etag:
            db.meta_set("bosses_etag", new_etag)
        db.meta_set("bosses_version", str(data.get("clientVersion") or ""))
        db.meta_set("bosses_last_ok", str(int(time.time())))
        db.meta_set("bosses_last_error", "")
        log.info("Boss list ok: %d open-world bosses (%d new)", len(bosses), len(new))
        return "ok"

    @tasks.loop(hours=1)
    async def refresh_loop(self):
        try:
            last = db.meta_get("bosses_last_attempt")
            if not last or time.time() - int(last) >= REFRESH_SECONDS:
                await self.fetch_and_store()
            heartbeat.beat("bosscatalog.refresh", self.refresh_loop)
        except Exception:
            log.exception("Boss list refresh error")

    @refresh_loop.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(BossCatalog(bot))
