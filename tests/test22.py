"""Field bosses refresh daily from the published copy of the shared reference: a valid copy
swaps the live tables in place (timers see it), a bad or unreachable copy changes nothing, the
copy is cached for restarts, and /setup show says where the list comes from."""
import asyncio, copy, json, os, sys, tempfile
from unittest.mock import MagicMock, AsyncMock
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import db, gamedata, timers, fieldbosses, guilds


class Resp:
    def __init__(self, body, status=200): self.body, self.status = body, status
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def read(self): return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


class Session:
    def __init__(self, resp): self.resp, self.calls = resp, 0
    def get(self, url, **k): self.calls += 1; return self.resp


def interaction(gid):
    it = MagicMock(); it.guild_id = gid; it.user.id = 5; it.response.send_message = AsyncMock()
    it.user.guild_permissions.manage_guild = True; it.user.guild_permissions.administrator = False
    return it


async def main():
    db.init(); db.ensure_guild(1)
    bundled = json.loads(gamedata._FIELD_BOSSES_JSON.read_text(encoding="utf-8"))
    before = list(gamedata.FIELD_BOSSES)
    assert "Soul Ruler Kashapa" in timers.FIELD_RESPAWNS and fieldbosses.describe().endswith("from the bundled file")

    # A published copy with a new boss replaces the tables in place; timers and the kill feed see it.
    doc = copy.deepcopy(bundled)
    doc["bosses"].append({"name": "Newly Added Tyrant", "zone": "Verteron · Somewhere", "faction": "elyos", "respawn_minutes": 240})
    cog = fieldbosses.FieldBosses.__new__(fieldbosses.FieldBosses); cog.bot = None; cog.session = Session(Resp(doc))
    assert await cog.fetch() == len(doc["bosses"])
    assert timers.FIELD_RESPAWNS["Newly Added Tyrant"] == 240 and timers.FIELD_FACTIONS["Newly Added Tyrant"] == "elyos"
    assert timers.field_respawn("newly added tyrant") == 240 and "Newly Added Tyrant" in gamedata.field_boss_names()
    assert timers.canonical(1, "Newly Added Tyrant") == ("Newly Added Tyrant", "Field Bosses", None)
    assert "Soul Ruler Kashapa" in timers.FIELD_RESPAWNS                      # the rest is unchanged
    assert fieldbosses.cache_path().exists() and db.meta_get("fieldbosses_last_error") == ""
    assert "from the published copy, fetched" in fieldbosses.describe()

    # Bad copies change nothing: too few bosses, a broken row, an HTTP error, oversized junk.
    for bad in (Resp({"bosses": doc["bosses"][:3]}), Resp({"bosses": doc["bosses"] + [{"name": "", "respawn_minutes": 5}]}),
                Resp({"bosses": doc["bosses"] + [{"name": "Weird", "respawn_minutes": -5}]}), Resp({}, 503), Resp(b"x" * (fieldbosses.MAX_BYTES + 1))):
        cog.session = Session(bad)
        assert await cog.fetch() == 0, bad.body if not isinstance(bad.body, bytes) else "oversized"
        assert timers.FIELD_RESPAWNS["Newly Added Tyrant"] == 240 and db.meta_get("fieldbosses_last_error")
    assert "last check failed" in fieldbosses.describe()

    # The cached copy is applied on the next start (before any download) even after the bundled tables are back.
    gamedata.apply(bundled)
    assert "Newly Added Tyrant" not in {n for n, _z, _m in gamedata.FIELD_BOSSES}
    assert fieldbosses.load_cache() == len(doc["bosses"]) and timers.FIELD_RESPAWNS["Newly Added Tyrant"] == 240
    assert "published copy (cached)" in fieldbosses.describe()

    # gamedata.apply validates strictly and leaves the tables alone on failure.
    for junk in ("x", {"bosses": "no"}, {"bosses": [{"name": "A", "respawn_minutes": 10}] * 19}):
        try:
            gamedata.apply(junk); raise SystemExit("should have failed")
        except ValueError:
            pass
    assert timers.FIELD_RESPAWNS["Newly Added Tyrant"] == 240 and len(gamedata.FIELD_BOSSES) == len(before) + 1

    # /setup show names the source.
    setup = guilds.ServerSetup()
    it = interaction(1); await setup.show.callback(setup, it)
    assert "Field bosses: " in it.response.send_message.await_args.args[0]
    print("FIELD BOSSES REFRESH OK")


asyncio.run(main())
