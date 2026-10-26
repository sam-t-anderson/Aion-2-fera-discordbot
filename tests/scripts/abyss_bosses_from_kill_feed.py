"""A contested Abyss boss (e.g. Argo) with no reliable community timer is timed from the
live combat-log kill feed, like a field boss: a fed kill that carries a measured respawn
starts an AUTO timer that supersedes the static "no reliable community timer" reference."""
import asyncio, os, sys, tempfile, time
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import db, timers, gameservers, timer_board


def _text(embed):
    return "\n".join(f.value for f in embed.fields)


async def main():
    db.init(); db.ensure_guild(1)
    now = int(time.time())

    # Before any kill: Argo shows the static reference text, no live time.
    before = _text(timer_board.build_embed(1))
    assert "Argo the Spirit King" in before and "no reliable community timer" in before.lower(), before

    # The feed reports Argo killed on this server with a server-measured respawn length.
    # The catalog spelling has a comma ("Argo, the Spirit King"); the bundled list doesn't.
    name, category, timing = timers.canonical(1, "Argo, the Spirit King")
    assert (name, category, timing) == ("Argo the Spirit King", timers.ABYSS_CATEGORY, None), (name, category, timing)

    kill = {"boss": "Argo, the Spirit King", "killed_at": now - 120, "reports": 2,
            "respawn_seconds": 3 * 3600, "server": "100"}
    assert gameservers.apply_kill(1, kill, race=None, now=now) is True

    # Now the board times Argo from the kill (superseding the reference text).
    after = _text(timer_board.build_embed(1))
    assert "Argo the Spirit King" in after and "from combat logs" in after, after
    # The static "no reliable community timer" line for Argo is gone (the live row replaced it).
    argo_lines = [ln for ln in after.splitlines() if "Argo the Spirit King" in ln]
    assert argo_lines and all("no reliable community timer" not in ln.lower() for ln in argo_lines), argo_lines

    # A boss with no server-measured respawn yet cannot be predicted, so it is left alone.
    assert gameservers.apply_kill(1, {"boss": "Executor Argo", "killed_at": now - 60, "reports": 2, "server": "100"},
                                  race=None, now=now) is False
    assert "Executor Argo" in _text(timer_board.build_embed(1))   # still shown as a reference row
    print("OK")


asyncio.run(main())
