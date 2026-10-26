"""Game servers and the kills seen on them.

The server list comes from the official site once a day, so /setup server can offer the
servers of a Discord server's region. Field boss kills come from the log server's feed of
kills found in uploaded combat logs: each one starts that boss's respawn timer, the same
way /timers killed does, for every Discord server that follows the game server."""

import json
import logging
import time
from typing import Optional

import aiohttp
from discord.ext import commands, tasks

import config
import db
import gamedata
import heartbeat
import timers

log = logging.getLogger("aion2.servers")

RACES = {1: "Elyos", 2: "Asmodian"}
HOME_ZONE = {1: "Verteron", 2: "Altgard"}
REFRESH_SECONDS = 24 * 3600
AUTO = 0   # set_by of a timer that came from the kill feed


# ---------------------------------------------------------------- server list
def store_servers(region: str, rows: list) -> int:
    """Replace one region's servers with the official list. Returns how many were saved."""
    keep = []
    for r in rows or []:
        if isinstance(r, dict) and str(r.get("serverId", "")).isdigit() and isinstance(r.get("serverName"), str):
            keep.append((str(r["serverId"]), region, r["serverName"].strip()[:40], r.get("raceId") if r.get("raceId") in RACES else None))
    if not keep:
        return 0
    c = db.conn()
    c.execute("DELETE FROM game_servers WHERE region=?", (region,))
    c.executemany("INSERT OR REPLACE INTO game_servers (server_id, region, name, race) VALUES (?,?,?,?)", keep)
    c.commit()
    return len(keep)


def servers(region: str) -> list:
    return db.conn().execute("SELECT * FROM game_servers WHERE region=? ORDER BY race, name", (region,)).fetchall()


def server(server_id) -> Optional[dict]:
    return db.conn().execute("SELECT * FROM game_servers WHERE server_id=?", (str(server_id or ""),)).fetchone()


def label(row) -> str:
    return f"{row['name']} ({RACES[row['race']]})" if row["race"] in RACES else row["name"]


def guild_server(guild_id: int):
    """The game server a Discord server follows, or None. A server from another region doesn't count."""
    row = server(db.get_setting(guild_id, "game_server"))
    return row if row and row["region"] == timers.region_key(guild_id) else None


# ---------------------------------------------------------------- kills from combat logs
def timer_name(boss: str, race) -> str:
    """The bot's timer for a boss. Bosses that respawn differently per zone are filed under the
    home zone of the server's faction."""
    zoned = f"{boss} ({HOME_ZONE.get(race, '')})"
    return next((n for n in timers.FIELD_RESPAWNS if n.lower() == zoned.lower()), boss)


def apply_kill(guild_id: int, kill: dict, race, now: Optional[int] = None) -> bool:
    """Start the respawn timer for one kill from the feed. True when a timer was set or moved."""
    now = now or int(time.time())
    boss, killed_at = kill.get("boss"), kill.get("killed_at")
    if not isinstance(boss, str) or not isinstance(killed_at, (int, float)) or killed_at > now + 300:
        return False
    if int(kill.get("reports") or 0) < config.BOSS_KILLS_MIN_REPORTS:
        return False
    name, category, timing = timers.canonical(guild_id, timer_name(" ".join(boss.split())[:60], race))
    if timing or not category:
        return False                      # on the site's schedule, or not a boss the bot knows
    c = db.conn()
    row = c.execute("SELECT * FROM guild_timer_manual WHERE guild_id=? AND name = ?", (guild_id, name)).fetchone()
    if row and (row["interval_min"] or row["set_by"] != AUTO and row["set_at"] >= killed_at):
        return False                      # someone here reported it after this kill, or gave it its own cycle
    # Shortest wins: the feed's figure is an upper bound measured from kills, the others are what people told us.
    lengths = [s for s in (
        kill.get("respawn_seconds"),
        (timers.field_respawn(name) or 0) * 60,
        (row["respawn_min"] or 0) * 60 if row else 0,
    ) if isinstance(s, (int, float)) and s >= 60]
    if not lengths:
        return False
    next_at = int(killed_at + min(lengths))
    if next_at <= now or row and row["next_at"] == next_at:
        return False
    timers.save_manual(guild_id, name, category, next_at, None, None, None, AUTO)
    return True


class GameServers(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self.list_loop.start()
        self.kills_loop.change_interval(minutes=config.BOSS_KILLS_MINUTES)
        self.kills_loop.start()

    async def cog_unload(self):
        self.list_loop.cancel()
        self.kills_loop.cancel()
        if self.session:
            await self.session.close()

    async def _get_json(self, url: str, params: Optional[dict] = None):
        headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json", "Referer": "https://aion2.plaync.com/"}
        async with self.session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return json.loads(await resp.text())

    async def fetch_servers(self) -> int:
        """Read every region's server list. Returns how many regions were read."""
        if not config.SERVER_LIST_URL:
            return 0
        db.meta_set("servers_last_attempt", str(int(time.time())))
        done = 0
        for region, code in gamedata.OFFICIAL_REGIONS.items():
            try:
                data = await self._get_json(config.SERVER_LIST_URL, {"region": code})
                if store_servers(region, data.get("serverList") if isinstance(data, dict) else None):
                    done += 1
            except Exception as e:
                log.warning("Could not read the %s server list, keeping the one I have: %s", region, e)
        if done:
            db.meta_set("servers_last_ok", str(int(time.time())))
        log.info("Server lists ok for %d of %d regions", done, len(gamedata.OFFICIAL_REGIONS))
        return done

    async def fetch_kills(self) -> int:
        """Read the kill feed for every game server a Discord server follows. Returns how many timers were set."""
        if not config.BOSS_KILLS_URL:
            return 0
        follows: dict[str, list] = {}
        for gid in db.guild_ids():
            row = guild_server(gid)
            if row:
                follows.setdefault(row["server_id"], []).append((gid, row["race"]))
        changed = 0
        for server_id, guilds in follows.items():
            try:
                data = await self._get_json(config.BOSS_KILLS_URL, {"server": server_id, "window": "86400"})
            except Exception as e:
                db.meta_set("kills_last_error", f"{type(e).__name__}: {e}")
                log.info("Kill feed not available for server %s: %s", server_id, e)
                continue
            db.meta_set("kills_last_ok", str(int(time.time())))
            db.meta_set("kills_last_error", "")
            for kill in (data.get("kills") if isinstance(data, dict) else None) or []:
                if isinstance(kill, dict) and str(kill.get("server")) == server_id:
                    changed += sum(apply_kill(gid, kill, race) for gid, race in guilds)
        return changed

    @tasks.loop(hours=1)
    async def list_loop(self):
        try:
            last = db.meta_get("servers_last_attempt")
            if not last or time.time() - int(last) >= REFRESH_SECONDS:
                await self.fetch_servers()
            heartbeat.beat("gameservers.list", self.list_loop)
        except Exception:
            log.exception("Server list refresh error")

    @tasks.loop(minutes=1)
    async def kills_loop(self):
        try:
            await self.fetch_kills()
            heartbeat.beat("gameservers.kills", self.kills_loop)
        except Exception:
            log.exception("Kill feed error")

    @list_loop.before_loop
    @kills_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(GameServers(bot))
