"""Official news posted to a channel: /setup news.

Every 15 minutes the bot reads the log server's news feed (the same official notices and
updates the Aion 2 tools News page shows) and posts each article a server hasn't seen yet
as one short lead line plus the article's official URL on its own line, so Discord renders
its usual link preview. Nothing is scraped here: the log server does the reading and caching.

Per server: the channel /setup news was run in, and a region choice. "Follow /setup region"
(the default) means the Global English boards for every Global region (NA East, NA West, EU,
SA, JP); the Korean and Taiwanese boards are posted only when chosen, and "all" posts every
board. The first run after setup posts only the newest article, later runs post at most
NEWS_MAX_PER_CYCLE each, oldest first, and an article is never posted twice to a server."""

import json
import logging
import time
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

import config
import db
import heartbeat

log = logging.getLogger("aion2.news")

REGION_LABELS = {"global": "Global (English)", "kr": "Korea (Korean)", "tw": "Taiwan (Traditional Chinese)"}
# What the region choice stored per server means; None (not chosen) follows /setup region.
CHOICES = {"auto": "Follow /setup region", "global": REGION_LABELS["global"], "kr": REGION_LABELS["kr"],
           "tw": REGION_LABELS["tw"], "all": "All regions"}
KEEP_ROWS = 400       # posted-article memory per server (the feed shows at most 20 per region)


def region_choice(guild_id: int) -> str:
    """The stored choice: auto, global, kr, tw or all (auto when nothing was chosen)."""
    value = db.get_setting(guild_id, "news_region")
    return value if value in CHOICES else "auto"


def regions_for(guild_id: int) -> list[str]:
    """The feed regions a server receives. Every Global game region (the only ones /setup region
    offers) reads the Global English boards; kr and tw only when chosen; all means every board."""
    choice = region_choice(guild_id)
    if choice == "all":
        return list(REGION_LABELS)
    if choice in REGION_LABELS:
        return [choice]
    return ["global"]      # every /setup region choice (NA East, NA West, EU, SA, JP) plays on the Global boards


def describe(guild_id: int) -> str:
    choice = region_choice(guild_id)
    if choice == "auto":
        return f"{REGION_LABELS['global']}, following `/setup region`"
    return CHOICES[choice]


def clean_entries(data) -> list[dict]:
    """The feed's articles, oldest first, with only the fields the bot needs; junk is dropped."""
    rows = data.get("entries") if isinstance(data, dict) else None
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ident, title, url = row.get("id"), row.get("title"), row.get("url")
        epoch = row.get("published_epoch")
        if not (isinstance(ident, str) and ident and isinstance(title, str) and isinstance(url, str)
                and url.startswith("https://") and isinstance(epoch, (int, float))):
            continue
        out.append({"id": ident[:64], "title": " ".join(title.split())[:200], "url": url[:500],
                    "category": str(row.get("category") or "News")[:40], "region": str(row.get("region") or "global")[:8],
                    "published_epoch": float(epoch)})
    out.sort(key=lambda r: (r["published_epoch"], r["id"]))
    return out


def message_for(entry: dict, show_region: bool) -> str:
    """One lead line (board · title) and the official URL alone on the next line, which is what
    makes Discord attach its link preview."""
    where = f" ({REGION_LABELS.get(entry['region'], entry['region'])})" if show_region else ""
    return f"**{entry['category']}{where}** · {entry['title']}\n{entry['url']}"


def posted_ids(guild_id: int) -> set[str]:
    return {r["article_id"] for r in db.conn().execute("SELECT article_id FROM news_posted WHERE guild_id=?", (guild_id,)).fetchall()}


def remember(guild_id: int, ids, now: Optional[int] = None) -> None:
    now = now or int(time.time())
    c = db.conn()
    c.executemany("INSERT OR IGNORE INTO news_posted (guild_id, article_id, posted_at) VALUES (?,?,?)",
                  [(guild_id, i, now) for i in ids])
    c.execute("DELETE FROM news_posted WHERE guild_id=? AND article_id NOT IN "
              "(SELECT article_id FROM news_posted WHERE guild_id=? ORDER BY posted_at DESC, article_id DESC LIMIT ?)",
              (guild_id, guild_id, KEEP_ROWS))
    c.commit()


def forget(guild_id: int) -> None:
    db.conn().execute("DELETE FROM news_posted WHERE guild_id=?", (guild_id,))
    db.conn().commit()


def select(guild_id: int, entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Which of the feed's articles to post now and which ids to remember without posting.
    A server that has never posted gets only the newest article (the rest is marked seen)."""
    seen = posted_ids(guild_id)
    fresh = [e for e in entries if e["id"] not in seen]
    if not fresh:
        return [], []
    if not seen:
        newest = max(fresh, key=lambda e: (e["published_epoch"], e["id"]))
        return [newest], [e["id"] for e in fresh if e["id"] != newest["id"]]
    return fresh[:config.NEWS_MAX_PER_CYCLE], []


class News(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self.news_loop.change_interval(minutes=config.NEWS_MINUTES)
        self.news_loop.start()

    async def cog_unload(self):
        self.news_loop.cancel()
        if self.session:
            await self.session.close()

    async def fetch(self, region: str) -> Optional[list[dict]]:
        """The feed for one region, oldest first, or None when it can't be read right now."""
        if not config.NEWS_URL:
            return None
        headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
        try:
            async with self.session.get(config.NEWS_URL, params={"region": region}, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = json.loads(await resp.text())
        except Exception as e:
            db.meta_set("news_last_error", f"{type(e).__name__}: {e}")
            log.info("News feed not available for %s: %s", region, e)
            return None
        db.meta_set("news_last_ok", str(int(time.time())))
        db.meta_set("news_last_error", "")
        return clean_entries(data)

    async def channel_for(self, guild_id: int):
        channel_id = db.get_setting(guild_id, "news_channel")
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id) if self.bot else None
        if channel is None and self.bot:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                db.set_settings(guild_id, news_channel=None)     # the channel was deleted: news is off
                log.info("News channel %s of server %s is gone; news turned off there", channel_id, guild_id)
                return None
            except discord.HTTPException:
                return None
        return channel

    async def publish(self, guild_id: int, feeds: Optional[dict] = None) -> int:
        """Post this server's unseen articles. Returns how many were posted."""
        channel = await self.channel_for(guild_id)
        if channel is None:
            return 0
        feeds = feeds if feeds is not None else {}
        regions = regions_for(guild_id)
        entries = []
        for region in regions:
            if region not in feeds:
                feeds[region] = await self.fetch(region)
            entries.extend(feeds[region] or [])
        if not entries:
            return 0
        entries.sort(key=lambda r: (r["published_epoch"], r["id"]))
        post, skip = select(guild_id, entries)
        if skip:
            remember(guild_id, skip)
        posted = 0
        for entry in post:
            try:
                await channel.send(message_for(entry, show_region=len(regions) > 1 or entry["region"] != "global"))
            except discord.Forbidden:
                log.info("Can't post news in channel %s of server %s (missing permission)", channel.id, guild_id)
                break
            except discord.HTTPException as e:
                log.info("Posting news in server %s failed: %s", guild_id, e)
                break
            remember(guild_id, [entry["id"]])
            posted += 1
        return posted

    async def publish_all(self) -> int:
        feeds: dict = {}
        total = 0
        for gid in db.news_guilds():
            try:
                total += await self.publish(gid, feeds)
            except Exception:
                log.exception("News error in server %s", gid)
        return total

    @tasks.loop(minutes=15)
    async def news_loop(self):
        try:
            await self.publish_all()
            heartbeat.beat("news", self.news_loop)
        except Exception:
            log.exception("News feed error")

    @news_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(News(bot))
