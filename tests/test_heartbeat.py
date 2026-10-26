"""Each background loop reports its last good round, and the health endpoint can tell a stopped one."""
import ast
import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import discord
from discord.ext import commands

import heartbeat

ROOT = pathlib.Path(__file__).resolve().parent.parent


def loop(hours=None, minutes=None, seconds=None):
    return SimpleNamespace(hours=hours, minutes=minutes, seconds=seconds)


def setup_function():
    heartbeat.reset()


def test_the_interval_follows_the_loop():
    assert heartbeat.interval(loop(seconds=20)) == 20
    assert heartbeat.interval(loop(minutes=15, seconds=0, hours=0)) == 900
    assert heartbeat.interval(loop(hours=1)) == 3600


def test_a_loop_is_stale_after_three_missed_rounds_and_at_least_five_minutes():
    with patch("heartbeat.time.time", return_value=1000.0):
        heartbeat.beat("timers.notify", loop(seconds=20))
        heartbeat.beat("news", loop(minutes=15))
    fresh = heartbeat.snapshot(now=1000 + 299)
    assert fresh["stale"] == [] and fresh["loops"]["timers.notify"] == {"age_seconds": 299, "every_seconds": 20, "stale": False}
    later = heartbeat.snapshot(now=1000 + 301)               # 20 s loop: five minutes is the floor
    assert later["stale"] == ["timers.notify"]
    assert heartbeat.snapshot(now=1000 + 2701)["stale"] == ["news", "timers.notify"]


def test_a_new_beat_clears_stale():
    with patch("heartbeat.time.time", return_value=0.0):
        heartbeat.beat("dkp.board", loop(minutes=2))
    assert heartbeat.snapshot(now=10_000)["stale"] == ["dkp.board"]
    with patch("heartbeat.time.time", return_value=10_000.0):
        heartbeat.beat("dkp.board", loop(minutes=2))
    assert heartbeat.snapshot(now=10_001)["stale"] == []


def background_loops():
    """(module, method, the loop argument given to heartbeat.beat) for every @tasks.loop in the bot."""
    found = []
    for path in sorted(ROOT.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if not any(isinstance(d, ast.Call) and ast.unparse(d.func) == "tasks.loop" for d in node.decorator_list):
                continue
            beats = [c for c in ast.walk(node) if isinstance(c, ast.Call) and ast.unparse(c.func) == "heartbeat.beat"]
            found.append((path.stem, node.name, [ast.unparse(c.args[1]) for c in beats]))
    return found


def test_every_background_loop_beats_with_its_own_interval():
    loops = background_loops()
    assert len(loops) >= 12
    wrong = [(m, name, args) for m, name, args in loops if args != [f"self.{name}"]]
    assert not wrong, wrong


def test_a_round_beats_only_when_it_worked(tmp_path):
    import checklist
    import db
    db.init(str(tmp_path / "t.db"))

    async def run():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        cog = checklist.Checklist(bot)
        with patch("db.announce_channels", side_effect=RuntimeError("database is locked")):
            await cog.reset_loop.coro(cog)                       # the error is logged; no beat
        assert "checklist.reset" not in heartbeat.snapshot()["loops"]
        await cog.reset_loop.coro(cog)
        return heartbeat.snapshot()["loops"]["checklist.reset"]

    assert asyncio.run(run()) == {"age_seconds": 0, "every_seconds": 30, "stale": False}
