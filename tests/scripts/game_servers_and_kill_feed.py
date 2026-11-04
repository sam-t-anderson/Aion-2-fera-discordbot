"""Game servers: the official list, /setup server, and respawn timers started by the kill feed."""
import asyncio, json, os, sys, tempfile, time
from unittest.mock import MagicMock, AsyncMock
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import config, db, guilds, gameservers, timer_board, timer_commands

NAE = {"serverList": [{"raceId": 1, "serverId": 1101, "serverName": "Siel", "serverShortName": "SIE"},
                      {"raceId": 2, "serverId": 2101, "serverName": "Israphel", "serverShortName": "ISR"},
                      {"raceId": 2, "serverId": 2102, "serverName": "Zikel", "serverShortName": "ZIK"},
                      {"serverId": "x"}, "junk"]}
EU = {"serverList": [{"raceId": 1, "serverId": 1301, "serverName": "Siel"}, {"raceId": 2, "serverId": 2302, "serverName": "Zikel"}]}


class Resp:
    def __init__(self, body, status=200): self.body, self.status, self.headers = body, status, {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def text(self): return json.dumps(self.body)
class Session:
    """Answers by the request's region or server."""
    def __init__(self): self.lists, self.kills, self.asked = {}, {}, []
    def get(self, url, params=None, **k):
        self.asked.append((url, dict(params or {})))
        if "region" in params:
            return Resp(self.lists[params["region"]]) if params["region"] in self.lists else Resp({}, 500)
        return Resp({"kills": self.kills.get(params["server"], [])})


def interaction(gid, admin=True):
    it = MagicMock(); it.guild_id = gid; it.user.id = 5; it.response.send_message = AsyncMock()
    it.user.guild_permissions.manage_guild = admin; it.user.guild_permissions.administrator = False
    return it

def said(it): return it.response.send_message.await_args.args[0]
def timer(gid, name): return db.conn().execute("SELECT * FROM guild_timer_manual WHERE guild_id=? AND name = ?", (gid, name)).fetchone()


async def main():
    db.init()
    A, B, C = 1, 2, 3
    for g in (A, B, C): db.ensure_guild(g)
    db.set_settings(C, timer_region="eu")
    setup = guilds.ServerSetup()
    cog = gameservers.GameServers.__new__(gameservers.GameServers); cog.bot = None; cog.session = Session()

    # before the list is read, the command says so
    it = interaction(A); await setup.server.callback(setup, it, server="Zikel")
    assert "don't have the server list yet" in said(it)

    # the official list: one region fails and keeps what it had, junk rows are dropped
    cog.session.lists = {"nae": NAE, "eu": EU}
    assert await cog.fetch_servers() == 2
    assert [r["name"] for r in gameservers.servers("na")] == ["Siel", "Israphel", "Zikel"]
    assert gameservers.label(gameservers.server("2102")) == "Zikel (Asmodian)" and gameservers.servers("naw") == []
    cog.session.lists = {"eu": EU}
    assert await cog.fetch_servers() == 1 and len(gameservers.servers("na")) == 3

    # /setup server: suggestions follow the region, picking saves it, other regions' servers are refused
    picks = await setup._server_ac(interaction(A), "zik")
    assert [(c.name, c.value) for c in picks] == [("Zikel (Asmodian)", "2102")]
    assert [c.value for c in await setup._server_ac(interaction(C), "")] == ["1301", "2302"]
    it = interaction(A, admin=False); await setup.server.callback(setup, it, server="2102")
    assert "Manage Server" in said(it) and gameservers.guild_server(A) is None
    for typed in ("2102", "zikel", "Zikel (Asmodian)"):
        db.set_settings(A, game_server=None)
        it = interaction(A); await setup.server.callback(setup, it, server=typed)
        assert "Zikel (Asmodian)" in said(it) and gameservers.guild_server(A)["server_id"] == "2102", typed
    it = interaction(A); await setup.server.callback(setup, it, server="2302")       # the EU Zikel
    assert "Pick a server from the list" in said(it) and gameservers.guild_server(A)["server_id"] == "2102"
    it = interaction(B); await setup.server.callback(setup, it, server="1101")
    it = interaction(C); await setup.server.callback(setup, it, server="2302")
    it = interaction(A); await setup.show.callback(setup, it)
    assert "Game server: Zikel (Asmodian)" in said(it)
    # changing region drops a server that is no longer in it
    it = interaction(C); await setup.region.callback(setup, it, region="na")
    assert "pick it again" in said(it) and db.get_setting(C, "game_server") is None
    it = interaction(C); await setup.server.callback(setup, it, server=None)
    assert "cleared" in said(it)

    # the kill feed: one request per followed game server, timers only for that server's Discord servers
    now = int(time.time())
    kashapa = {"npc_id": 2101122, "boss": "Soul Ruler Kashapa", "server": "2102", "killed_at": now - 3600, "reports": 2,
               "respawn_seconds": None}
    cog.session.kills = {"2102": [
        kashapa,
        {"boss": "Silent Dartan", "server": "2102", "killed_at": now - 600, "reports": 1, "respawn_seconds": None},
        {"boss": "Eternal Gartua", "server": "2102", "killed_at": now - 12 * 3600, "reports": 1},         # back up (11 h)
        {"boss": "Vulcanarch Grymnir", "server": "2102", "killed_at": now - 60, "reports": 1},           # respawn unknown
        {"boss": "Some Dungeon Boss", "server": "2102", "killed_at": now - 60, "reports": 1, "respawn_seconds": 3600},
        {"boss": "Kernon of the West", "server": "9999", "killed_at": now - 60, "reports": 1},           # not this server
        {"boss": "Rotten Kutar", "server": "2102", "killed_at": now + 9999, "reports": 1}, "junk",
    ]}
    cog.session.asked.clear()
    assert await cog.fetch_kills() == 2
    assert sorted(p["server"] for _u, p in cog.session.asked) == ["1101", "2102"]
    row = timer(A, "Soul Ruler Kashapa")
    assert row["next_at"] == now - 3600 + 6 * 3600 and row["set_by"] == 0 and row["category"] == "Field Bosses"
    assert timer(A, "Silent Dartan (Altgard)")["next_at"] == now - 600 + 6 * 3600        # Asmodian server: the Altgard one
    assert sorted(r["name"] for r in db.conn().execute("SELECT name FROM guild_timer_manual WHERE guild_id=?", (A,))) ==         ["Silent Dartan (Altgard)", "Soul Ruler Kashapa"]
    assert not db.conn().execute("SELECT 1 FROM guild_timer_manual WHERE guild_id IN (?,?)", (B, C)).fetchall()
    embed = str(timer_board.build_embed(A).to_dict())
    assert "Soul Ruler Kashapa" in embed and "from combat logs" in embed and "<@0>" not in embed
    assert await cog.fetch_kills() == 0                                                  # nothing new: no rewrite

    # a measured respawn shorter than the built-in one wins; a later kill moves the timer
    cog.session.kills["2102"] = [{**kashapa, "killed_at": now - 1800, "respawn_seconds": 5 * 3600}]
    assert await cog.fetch_kills() == 1 and timer(A, "Soul Ruler Kashapa")["next_at"] == now - 1800 + 5 * 3600
    # a boss the bot has no respawn for uses the feed's, or the one this Discord server taught it
    cog.session.kills["2102"] = [{"boss": "Vulcanarch Grymnir", "server": "2102", "killed_at": now - 60, "reports": 1, "respawn_seconds": 7200}]
    db.conn().execute("INSERT INTO boss_catalog (name, level, zones, areas, last_seen) VALUES ('Vulcanarch Grymnir', 80, 'Morheim', 'Lava', 1)"); db.conn().commit()
    assert await cog.fetch_kills() == 1 and timer(A, "Vulcanarch Grymnir")["next_at"] == now - 60 + 7200

    # someone here reporting the kill afterwards wins over the feed; an older report does not
    tc = timer_commands.Timers.__new__(timer_commands.Timers)
    it = interaction(A); await timer_commands.Timers.killed.callback(tc, it, name="Soul Ruler Kashapa", respawn="4h")
    mine = timer(A, "Soul Ruler Kashapa")["next_at"]
    cog.session.kills["2102"] = [{**kashapa, "killed_at": now - 900, "respawn_seconds": None}]
    assert await cog.fetch_kills() == 0 and timer(A, "Soul Ruler Kashapa")["next_at"] == mine
    cog.session.kills["2102"] = [{**kashapa, "killed_at": now + 120, "respawn_seconds": None}]   # a newer kill (clock a little ahead)
    assert await cog.fetch_kills() == 1
    row = timer(A, "Soul Ruler Kashapa")
    assert row["set_by"] == 0 and row["next_at"] == now + 120 + 4 * 3600 and row["respawn_min"] == 240   # the taught 4h is the shortest
    # anyone may clear a timer that came from the feed
    it = interaction(A, admin=False); await timer_commands.Timers.clear.callback(tc, it, name="Soul Ruler Kashapa")
    assert "Cleared" in said(it)

    # a stricter server owner can ask for two uploaders
    config.BOSS_KILLS_MIN_REPORTS = 2
    cog.session.kills["2102"] = [{"boss": "Kernon of the West", "server": "2102", "killed_at": now - 60, "reports": 1}]
    assert await cog.fetch_kills() == 0
    cog.session.kills["2102"][0]["reports"] = 2
    assert await cog.fetch_kills() == 1 and timer(A, "Kernon of the West")["next_at"] == now - 60 + 30 * 60
    # the feed being down changes nothing
    config.BOSS_KILLS_MIN_REPORTS = 1
    class Down(Session):
        def get(self, *a, **k): return Resp({}, 502)
    cog.session = Down()
    assert await cog.fetch_kills() == 0 and "502" in db.meta_get("kills_last_error")
    print("GAME SERVERS OK")
asyncio.run(main())
