"""/setup news: official news posted to the channel the command was run in.

The bot reads the log server's news feed (what the Aion 2 tools News page shows) every few
minutes and posts each article a server hasn't seen as a lead line plus the official URL on
its own line (Discord's link preview). First run posts only the newest article; later runs
post at most NEWS_MAX_PER_CYCLE, oldest first; nothing is posted twice; the region follows
/setup region (Global boards) unless kr, tw or all was chosen."""
import asyncio, json, os, sys, tempfile
from unittest.mock import MagicMock, AsyncMock
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import config, db, guilds, news


def entry(i, region="global", category="Notices", epoch=None):
    return {"id": f"{i:024x}", "title": f"Article {i}", "category": category, "region": region,
            "published_epoch": epoch if epoch is not None else 1_760_000_000 + i * 3600,
            "url": f"https://aion2.plaync.com/en-us/board/notice/view?articleId={i:024x}", "preview": "x"}


class Resp:
    def __init__(self, body, status=200): self.body, self.status = body, status
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def text(self): return json.dumps(self.body)


class Session:
    def __init__(self): self.feeds, self.asked = {}, []
    def get(self, url, params=None, **k):
        self.asked.append((url, dict(params or {})))
        region = params.get("region")
        return Resp({"entries": self.feeds[region]}) if region in self.feeds else Resp({}, 503)


class Channel:
    def __init__(self, cid=77): self.id, self.sent = cid, []
    async def send(self, text): self.sent.append(text)


def interaction(gid, channel, admin=True):
    it = MagicMock(); it.guild_id = gid; it.user.id = 5; it.response.send_message = AsyncMock()
    it.user.guild_permissions.manage_guild = admin; it.user.guild_permissions.administrator = False
    it.channel = channel; it.channel.mention = f"<#{channel.id}>"
    perms = MagicMock(); perms.view_channel = perms.send_messages = perms.embed_links = True
    it.channel.permissions_for = lambda member: perms
    it.client.get_cog.return_value = None
    return it, perms


def said(it): return it.response.send_message.await_args.args[0]


async def main():
    db.init()
    A, B = 1, 2
    for g in (A, B): db.ensure_guild(g)
    # The settings columns and the posted-articles memory exist, and get/set accept them.
    assert db.get_setting(A, "news_channel") is None and db.get_setting(A, "news_region") is None
    assert "news_channel" in db.SETTING_COLUMNS and "news_region" in db.SETTING_COLUMNS

    setup = guilds.ServerSetup()
    cog = news.News.__new__(news.News); cog.session = Session()
    channels = {77: Channel(77), 88: Channel(88)}
    bot = MagicMock(); bot.get_channel = lambda cid: channels.get(cid); bot.fetch_channel = AsyncMock(side_effect=lambda cid: channels[cid])
    cog.bot = bot

    # Not an admin: refused. Missing Embed Links: refused with the reason.
    it, _ = interaction(A, channels[77], admin=False); await setup.news_feed.callback(setup, it)
    assert "Manage Server" in said(it)
    it, perms = interaction(A, channels[77]); perms.embed_links = False; await setup.news_feed.callback(setup, it)
    assert "Embed Links" in said(it) and db.get_setting(A, "news_channel") is None

    # /setup news in #77: saved for that channel, following /setup region (Global boards).
    it, _ = interaction(A, channels[77]); await setup.news_feed.callback(setup, it)
    assert db.get_setting(A, "news_channel") == 77 and db.get_setting(A, "news_region") is None
    assert "<#77>" in said(it) and "Global (English)" in said(it) and "stop:True" in said(it)
    assert news.regions_for(A) == ["global"] and db.news_guilds() == [A]

    # Feed down: nothing posted, nothing remembered, the error is recorded.
    assert await cog.publish(A) == 0 and channels[77].sent == [] and db.meta_get("news_last_error")

    # First run posts only the newest article; the older ones are remembered as seen.
    cog.session.feeds["global"] = [entry(3), entry(1), entry(2, category="Updates")]
    assert await cog.publish(A) == 1
    assert channels[77].sent == [f"**Notices** · Article 3\n{entry(3)['url']}"]      # lead line, then the bare URL
    assert news.posted_ids(A) == {entry(1)["id"], entry(2)["id"], entry(3)["id"]}
    assert db.meta_get("news_last_error") == ""

    # Nothing new: nothing posted (no duplicates). Then two new articles arrive: both posted, oldest first.
    assert await cog.publish(A) == 0 and len(channels[77].sent) == 1
    cog.session.feeds["global"] = [entry(5, category="Updates"), entry(4)] + cog.session.feeds["global"]
    assert await cog.publish(A) == 2
    assert channels[77].sent[1:] == [f"**Notices** · Article 4\n{entry(4)['url']}", f"**Updates** · Article 5\n{entry(5)['url']}"]

    # A burst is capped per cycle (oldest first); the rest follows next time.
    cog.session.feeds["global"] = [entry(i) for i in range(20, 5, -1)]
    assert await cog.publish(A) == config.NEWS_MAX_PER_CYCLE
    assert channels[77].sent[-1] == f"**Notices** · Article {6 + config.NEWS_MAX_PER_CYCLE - 1}\n{entry(6 + config.NEWS_MAX_PER_CYCLE - 1)['url']}"
    assert await cog.publish(A) == config.NEWS_MAX_PER_CYCLE

    # Junk in the feed is dropped; a feed with no usable entry posts nothing.
    assert news.clean_entries({"entries": [{"id": 5}, "x", {"id": "a", "title": "t", "url": "http://insecure", "published_epoch": 1}]}) == []

    # Region choices: kr only, all three boards (labelled per region), and a region change is honoured next cycle.
    it, _ = interaction(B, channels[88]); await setup.news_feed.callback(setup, it, region="kr")
    assert db.get_setting(B, "news_region") == "kr" and news.regions_for(B) == ["kr"] and "Korea" in said(it)
    cog.session.feeds["kr"] = [entry(100, region="kr", category="Notices")]
    assert await cog.publish(B) == 1 and channels[88].sent == [f"**Notices (Korea (Korean))** · Article 100\n{entry(100, region='kr')['url']}"]
    it, _ = interaction(B, channels[88]); await setup.news_feed.callback(setup, it, region="all")
    assert news.regions_for(B) == ["global", "kr", "tw"]
    cog.session.feeds["tw"] = [entry(200, region="tw", category="Updates", epoch=1_760_000_000 + 300 * 3600)]
    assert await cog.publish(B) == config.NEWS_MAX_PER_CYCLE       # global articles not seen by B yet, capped
    assert all("(Global (English))" in m for m in channels[88].sent[1:])
    total = 0
    while n := await cog.publish(B):            # the remaining global articles, capped per cycle, then the newer Taiwanese one
        total += n
    assert total == 10 + 1 and channels[88].sent[-1].startswith("**Updates (Taiwan (Traditional Chinese))** · Article 200")

    # One cycle serves every server and reads each region's feed once (shared across servers).
    cog.session.asked.clear()
    assert await cog.publish_all() == config.NEWS_MAX_PER_CYCLE        # A's remaining global articles; B is caught up
    assert sorted(p["region"] for _u, p in cog.session.asked) == ["global", "kr", "tw"]

    # /setup show names the channel and the choice; stop turns it off; leaving the server forgets the memory.
    it, _ = interaction(B, channels[88]); await setup.show.callback(setup, it)
    assert "News: <#88> — All regions" in said(it)
    it, _ = interaction(B, channels[88]); await setup.news_feed.callback(setup, it, stop=True)
    assert db.get_setting(B, "news_channel") is None and db.news_guilds() == [A] and "off" in said(it)
    it, _ = interaction(A, channels[77]); await setup.show.callback(setup, it)
    assert "News: <#77> — Global (English), following `/setup region`" in said(it)
    db.forget_guild(A)
    assert news.posted_ids(A) == set()

    # A deleted channel turns news off for that server instead of failing every cycle.
    db.ensure_guild(3); db.set_settings(3, news_channel=99)
    import discord
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    assert await cog.publish(3) == 0 and db.get_setting(3, "news_channel") is None
    print("NEWS OK")


asyncio.run(main())
