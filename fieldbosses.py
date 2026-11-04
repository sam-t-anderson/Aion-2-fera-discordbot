"""Field bosses from the published copy of the shared reference, refreshed daily.

The bot ships ``assets/field-bosses.json`` (the same file the Aion 2 tools client and Pages
site carry). Once a day it also reads the copy the Pages site publishes, validates it the same
way, keeps it in a cache file next to the database, and swaps the live tables in place, so a
field boss added upstream shows up in /timers without redeploying the bot. A copy that fails
validation, or a failed download, leaves the tables as they are: the cached copy on restart,
otherwise the bundled file, otherwise the built-in list. A copy checked before the bundled file
is not used either, cached or downloaded: the bot can ship newer values (a patch's respawns)
before the Pages copy has them, and that copy would put the old ones back."""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from discord.ext import commands, tasks

import config
import db
import gamedata
import heartbeat
import timers

log = logging.getLogger("aion2.fieldbosses")

REFRESH_SECONDS = 24 * 3600
MAX_BYTES = 1024 * 1024


def cache_path() -> Path:
    return Path(config.DB_PATH).resolve().parent / "field-bosses.live.json"


def _refresh_timers() -> None:
    timers.FIELD_RESPAWNS.clear()
    timers.FIELD_RESPAWNS.update(gamedata.field_boss_respawns())
    timers.FIELD_FACTIONS.clear()
    timers.FIELD_FACTIONS.update(gamedata.field_boss_factions())


def install(doc, source: str, fetched_at: Optional[int] = None) -> int:
    """Validate and apply a document to the live tables, then refresh timers' derived maps."""
    count = gamedata.apply(doc)
    _refresh_timers()
    db.meta_set("fieldbosses_source", source)
    db.meta_set("fieldbosses_count", str(count))
    db.meta_set("fieldbosses_fetched_at", str(int(fetched_at or time.time())))
    return count


def older_than_bundled(doc) -> bool:
    """Whether a published copy was checked before the bundled file."""
    published = gamedata.checked_at(doc)
    return bool(published and gamedata.BUNDLED_CHECKED_AT and published < gamedata.BUNDLED_CHECKED_AT)


def use_bundled(doc) -> int:
    """Put the bundled tables back instead of a published copy older than them."""
    count = gamedata.apply_bundled()
    _refresh_timers()
    db.meta_set("fieldbosses_source", "")
    log.info("Field bosses: the published copy (checked %s) is older than the bundled file (checked %s); "
             "using the bundled file", gamedata.checked_at(doc), gamedata.BUNDLED_CHECKED_AT)
    return count


def load_cache() -> int:
    """Apply the cached published copy (from an earlier run), if there is one and it isn't older than the
    bundled file. 0 when it isn't applied."""
    try:
        doc = json.loads(cache_path().read_text(encoding="utf-8"))
        if older_than_bundled(doc):
            use_bundled(doc)
            return 0
        fetched = int(db.meta_get("fieldbosses_fetched_at") or 0)
        return install(doc, "published copy (cached)", fetched or None)
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def describe() -> str:
    """One line for /setup show: where the live field-boss list comes from and how old it is."""
    source = db.meta_get("fieldbosses_source")
    count = len(gamedata.FIELD_BOSSES) + len(gamedata.ABYSS_BOSSES)
    if not source:
        return f"{count} bosses from the bundled file"
    when = db.meta_get("fieldbosses_fetched_at")
    stamp = datetime.fromtimestamp(int(when), timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if when and when.isdigit() else "unknown time"
    error = db.meta_get("fieldbosses_last_error")
    return f"{count} bosses from the {source}, fetched {stamp}" + (f" (last check failed: {error})" if error else "")


class FieldBosses(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        if load_cache():
            log.info("Field bosses: using the cached published copy (%s)", describe())
        self.session = aiohttp.ClientSession()
        self.refresh_loop.start()

    async def cog_unload(self):
        self.refresh_loop.cancel()
        if self.session:
            await self.session.close()

    async def fetch(self) -> int:
        """Read the published copy once; returns the number of bosses installed (0 when kept as is)."""
        if not config.FIELD_BOSSES_URL:
            return 0
        db.meta_set("fieldbosses_last_attempt", str(int(time.time())))
        headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
        try:
            async with self.session.get(config.FIELD_BOSSES_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                raw = await resp.read()
            if len(raw) > MAX_BYTES:
                raise ValueError("published copy is larger than 1 MiB")
            doc = json.loads(raw.decode("utf-8"))
            if older_than_bundled(doc):
                # The bot was updated before the Pages copy: keep its newer values until the copy catches up.
                use_bundled(doc)
                db.meta_set("fieldbosses_last_error", "")
                db.meta_set("fieldbosses_last_ok", str(int(time.time())))
                return 0
            count = install(doc, "published copy")
            cache_path().write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            db.meta_set("fieldbosses_last_error", f"{type(e).__name__}: {e}")
            log.info("Field bosses: published copy not used (%s); keeping %s", e, describe())
            return 0
        db.meta_set("fieldbosses_last_error", "")
        db.meta_set("fieldbosses_last_ok", str(int(time.time())))
        log.info("Field bosses: %d from the published copy", count)
        return count

    @tasks.loop(hours=1)
    async def refresh_loop(self):
        try:
            last = db.meta_get("fieldbosses_last_attempt")
            if not last or time.time() - int(last) >= REFRESH_SECONDS:
                await self.fetch()
            heartbeat.beat("fieldbosses.refresh", self.refresh_loop)
        except Exception:
            log.exception("Field boss refresh error")

    @refresh_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(FieldBosses(bot))
