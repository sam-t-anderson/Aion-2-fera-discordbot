"""When each background loop last finished a round, for the health endpoint.

Each loop calls ``beat(name, loop)`` at the end of a round that did not fail. A loop whose last good round
is more than three of its intervals old (and at least five minutes) is ``stale``: stuck, stopped by an
error, or failing every round. A source that fails inside a round (the timers site, the log server's
news or kill feed) is handled by its loop; its own ``*_last_ok`` time shows that separately."""
import time

_BEATS: dict[str, tuple[float, float]] = {}     # name -> (time of the last good round, interval in seconds)


def interval(loop) -> float:
    """Seconds between the rounds of a ``discord.ext.tasks.Loop``, as it is set now."""
    return (loop.hours or 0) * 3600 + (loop.minutes or 0) * 60 + (loop.seconds or 0)


def beat(name: str, loop) -> None:
    _BEATS[name] = (time.time(), interval(loop))


def snapshot(now: float | None = None) -> dict:
    """``{"loops": {name: {"age_seconds", "every_seconds", "stale"}}, "stale": [names]}``"""
    now = time.time() if now is None else now
    loops = {}
    for name, (at, every) in sorted(_BEATS.items()):
        age = max(0, int(now - at))
        loops[name] = {"age_seconds": age, "every_seconds": int(every), "stale": age > max(3 * every, 300)}
    return {"loops": loops, "stale": [n for n, v in loops.items() if v["stale"]]}


def reset() -> None:
    _BEATS.clear()
