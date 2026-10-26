"""The open-world boss list read from the JSON feed: parsing, storing, lookups, failures."""
import asyncio, json, os, sys, tempfile, time
from unittest.mock import MagicMock, AsyncMock
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import db, timers, bosscatalog, timer_commands


def rec(name, level, spawns, boss=True):
    return {"name": name, "level": [level, level], "rank": "Hero", "boss": boss, "url": "https://example.com/" + name.replace(" ", "-"),
            "spawns": [{"map": m, "area": a, "points": 1} for m, a in spawns], "drops": [], "skills": []}

FEED = {"clientVersion": "9.9", "records": [
    rec("Vulcanarch Grymnir", 80, [("Morheim", "Lava Fields")]),
    rec("Silent Dartan", 48, [("Altgard", "Basfelt Ruins"), ("Verteron", "Southern Altamia Highland")]),
    rec("Argo, the Spirit King", 65, [("Chaotic Lower Reshanta", "Siel's Right Wing")]),
    rec("Watcher Kaira", 65, [("Chaotic Lower Reshanta", f"Island {i}") for i in range(6)]),
    rec("Dungeon Boss", 45, [("Fire Temple", "Hall")]),                 # not open world
    rec("Wandering Trash", 45, [("Verteron", "Field")], boss=False),    # not a boss
    rec("Vulcanarch Grymnir", 80, [("Morheim", "Elsewhere")]),          # a repeat is ignored
    {"name": 5, "boss": True}, "junk",
] + [rec(f"Filler Boss {i}", 45, [("Eltnen", "Somewhere")]) for i in range(20)]}

parsed = {b["name"]: b for b in bosscatalog.parse_feed(FEED)}
assert len(parsed) == 24 and "Dungeon Boss" not in parsed and "Wandering Trash" not in parsed
assert parsed["Vulcanarch Grymnir"] == {"name": "Vulcanarch Grymnir", "level": 80, "zones": "Morheim", "areas": "Lava Fields",
                                        "url": "https://example.com/Vulcanarch-Grymnir"}
assert parsed["Silent Dartan"]["zones"] == "Altgard, Verteron" and "Verteron: Southern Altamia Highland" in parsed["Silent Dartan"]["areas"]
assert parsed["Watcher Kaira"]["areas"].endswith("(+3 more)")
assert bosscatalog.parse_feed({}) == [] and bosscatalog.parse_feed({"records": None}) == []


class Resp:
    def __init__(self, body, status=200, headers=None): self.body, self.status, self.headers = body, status, headers or {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def text(self): return self.body
class Session:
    def __init__(self, *resps): self.resps, self.sent = list(resps), []
    def get(self, url, headers=None, **k):
        self.sent.append(headers or {})
        return self.resps.pop(0)


async def main():
    db.init(); db.ensure_guild(1)
    assert not db.bosses(1, "world_boss", "Grymnir")
    cog = bosscatalog.BossCatalog.__new__(bosscatalog.BossCatalog); cog.bot = None
    # a broken or too-short feed changes nothing
    cog.session = Session(Resp("not json"), Resp(json.dumps({"records": FEED["records"][:3]})), Resp("", 500))
    for _ in range(3):
        assert await cog.fetch_and_store() == "error"
    assert bosscatalog.names() == [] and db.meta_get("bosses_last_error")
    # a good read fills the list, the planner's boss lists and the name suggestions
    cog.session = Session(Resp(json.dumps(FEED), headers={"ETag": "v1"}), Resp("", 304))
    assert await cog.fetch_and_store() == "ok"
    assert len(bosscatalog.names()) == 24 and db.meta_get("bosses_version") == "9.9" and db.meta_get("bosses_last_error") == ""
    assert db.bosses(1, "world_boss", "Grymnir") == ["Vulcanarch Grymnir"]
    db.ensure_guild(2)                                       # a server that joins later gets them too
    assert db.bosses(2, "world_boss", "Spirit King") == ["Argo, the Spirit King"]
    assert await cog.fetch_and_store() == "unchanged" and cog.session.sent[-1]["If-None-Match"] == "v1"

    assert timers.canonical(1, "vulcanarch grymnir") == ("Vulcanarch Grymnir", "Field Bosses", None)
    assert timers.canonical(1, "ARGO, the spirit king") == ("Argo, the Spirit King", "World Bosses", None)
    assert timers.canonical(1, "Silent Dartan")[1] == "Field Bosses"
    assert "Vulcanarch Grymnir" in [c.name for c in timer_commands.Timers._name_choices(1, "grym")]
    assert bosscatalog.describe("Vulcanarch Grymnir") == "Lv 80, Morheim: Lava Fields"
    assert bosscatalog.describe("Silent Dartan (Altgard)").startswith("Lv 48, Altgard: Basfelt Ruins")
    assert bosscatalog.describe("Nobody") == ""

    # reporting a kill: a boss from the feed needs its respawn once, then it is remembered
    tc = timer_commands.Timers.__new__(timer_commands.Timers)
    async def killed(**kw):
        it = MagicMock(); it.guild_id = 1; it.user.id = 5; it.response.send_message = AsyncMock()
        await timer_commands.Timers.killed.callback(tc, it, **kw)
        return it.response.send_message.await_args.args[0]
    assert "respawn:4h" in await killed(name="vulcanarch grymnir")
    msg = await killed(name="vulcanarch grymnir", respawn="8h")
    assert "Vulcanarch Grymnir" in msg and "Lv 80, Morheim: Lava Fields" in msg
    row = db.conn().execute("SELECT * FROM guild_timer_manual WHERE name='Vulcanarch Grymnir'").fetchone()
    assert row["category"] == "Field Bosses" and row["respawn_min"] == 480 and abs(row["next_at"] - time.time() - 8 * 3600) < 5
    assert "8h from now" in await killed(name="Vulcanarch Grymnir")
    assert "different speed in each zone" in await killed(name="Silent Dartan")
    print("BOSS LIST OK")
asyncio.run(main())
