"""The health endpoint: online or not for Docker, plus the loops and outside sources for a monitor."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

import db
import health
import heartbeat


class Bot:
    latency = 0.05
    guilds = [1, 2]

    def __init__(self, ready=True):
        self.ready = ready
        if not ready:
            self.latency = float("nan")

    def is_ready(self):
        return self.ready

    def is_closed(self):
        return False


def get(bot):
    async def run():
        async with TestClient(TestServer(health.build_app(bot))) as client:
            response = await client.get("/health")
            return response.status, await response.json()
    return asyncio.run(run())


def test_loops_and_sources_are_reported(tmp_path):
    db.init(str(tmp_path / "t.db"))
    heartbeat.reset()
    now = int(time.time())
    db.meta_set("timers_last_ok", str(now - 120))
    db.meta_set("kills_last_ok", str(now - 30))
    heartbeat.beat("timers.notify", SimpleNamespace(hours=None, minutes=None, seconds=20))
    status, body = get(Bot())
    assert status == 200 and body["status"] == "ok" and body["guilds"] == 2
    assert body["loops"]["timers.notify"]["every_seconds"] == 20 and body["stale_loops"] == []
    ages = body["sources_last_ok_age_seconds"]
    assert 119 <= ages["timers"] <= 125 and 29 <= ages["kill_feed"] <= 35 and ages["news"] is None
    assert body["timers_last_ok_age_seconds"] == ages["timers"]            # the older field stays for monitors


def test_a_stale_loop_is_named_but_the_bot_stays_healthy(tmp_path):
    db.init(str(tmp_path / "t.db"))
    heartbeat.reset()
    with patch("heartbeat.time.time", return_value=time.time() - 3600):
        heartbeat.beat("dkp.board", SimpleNamespace(hours=None, minutes=2, seconds=None))
    status, body = get(Bot())
    assert status == 200 and body["stale_loops"] == ["dkp.board"]           # Docker restarts only for Discord


def test_offline_is_503(tmp_path):
    db.init(str(tmp_path / "t.db"))
    status, body = get(Bot(ready=False))
    assert status == 503 and body["status"] == "offline" and body["gateway_latency_ms"] is None


def test_the_phase_and_the_commands_are_reported(tmp_path):
    db.init(str(tmp_path / "t.db"))
    starting = Bot(ready=False)
    starting.phase, starting.commands_status = "connecting to Discord", "registering"
    status, body = get(starting)
    assert status == 503 and body["phase"] == "connecting to Discord" and body["commands"] == "registering"


def test_the_database_is_not_opened_by_a_health_check():
    """Before the bot opens the database the endpoint answers without it: a check must not run the migrations."""
    with patch.object(db, "_conn", None), patch.object(db, "init", side_effect=AssertionError("opened by /health")):
        status, body = get(Bot(ready=False))
    assert status == 503 and set(body["sources_last_ok_age_seconds"].values()) == {None}
