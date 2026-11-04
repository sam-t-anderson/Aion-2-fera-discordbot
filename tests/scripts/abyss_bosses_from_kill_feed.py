"""A contested Abyss boss the site's schedule doesn't time is timed from the live combat-log kill feed, like a field
boss: a fed kill that carries a measured respawn starts an AUTO timer that supersedes the static reference line.
Argo, the Spirit King was that boss until the 4 November 2026 patch notes gave it fixed times (10:30 and 22:30 server
time): once the schedule is stored it is timed from those, and a fed kill no longer moves it."""
import asyncio, os, sys, tempfile, time
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import db, timers, gameservers, timer_board


def _text(embed):
    return "\n".join(f.value for f in embed.fields)


async def main():
    db.init(); db.ensure_guild(1)
    now = int(time.time())

    # Before the site's schedule is read and before any kill: Argo shows the reference text, no live time.
    before = _text(timer_board.build_embed(1))
    assert "Argo, the Spirit King" in before and "10:30 and 22:30" in before, before

    # Names match without punctuation ("Argo the Spirit King", as the reference used to spell it).
    name, category, timing = timers.canonical(1, "Argo the Spirit King")
    assert (name, category, timing) == ("Argo, the Spirit King", timers.ABYSS_CATEGORY, None), (name, category, timing)

    kill = {"boss": "Argo, the Spirit King", "killed_at": now - 120, "reports": 2,
            "respawn_seconds": 3 * 3600, "server": "100"}
    assert gameservers.apply_kill(1, kill, race=None, now=now) is True

    # Now the board times Argo from the kill (superseding the reference text).
    after = _text(timer_board.build_embed(1))
    argo_lines = [ln for ln in after.splitlines() if "Argo, the Spirit King" in ln]
    assert len(argo_lines) == 1 and "from combat logs" in argo_lines[0], argo_lines

    # A boss with no server-measured respawn yet cannot be predicted, so it is left alone.
    assert gameservers.apply_kill(1, {"boss": "Executor Argo", "killed_at": now - 60, "reports": 2, "server": "100"},
                                  race=None, now=now) is False
    assert "Executor Argo" in _text(timer_board.build_embed(1))   # still shown as a reference row

    # Once the site's schedule is stored, the official times are added to it: Argo is on a schedule now, so the
    # feed leaves it alone, like the site's own Abyss bosses.
    timers.clear_manual(1, "Argo, the Spirit King")
    timers.store_rules([("Watcher Kaira", "World Bosses", "every 3h from 01:00")])
    assert timers.canonical(1, "Argo, the Spirit King") == ("Argo, the Spirit King", "World Bosses", "daily at 10:30, 22:30")
    assert gameservers.apply_kill(1, {**kill, "killed_at": now - 60}, race=None, now=now) is False
    board = _text(timer_board.build_embed(1))
    argo_lines = [ln for ln in board.splitlines() if "Argo" in ln and "Spirit King" in ln]
    assert len(argo_lines) == 1 and "<t:" in argo_lines[0], argo_lines           # one line, with its next start
    print("OK")


asyncio.run(main())
