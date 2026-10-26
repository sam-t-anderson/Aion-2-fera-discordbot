"""Tiny web endpoint so Uptime Kuma, Homepage and Docker can see if the bot is online.

It answers 200 while the bot is connected to Discord and 503 when it is not; Docker's health check and
the updater go by that. The body also says when each background loop last finished a round
(``loops``, and ``stale_loops`` for any that stopped) and how long ago each outside source was last
read (``sources_last_ok_age_seconds``), for a monitor that wants to watch those too."""

import json
import logging
import time

from aiohttp import web

import config
import db
import heartbeat

log = logging.getLogger("aion2.health")
STARTED = time.time()

# Outside sources and the meta key each one stamps after a good read.
SOURCES = {"timers": "timers_last_ok", "kill_feed": "kills_last_ok", "news": "news_last_ok",
           "boss_list": "bosses_last_ok", "field_bosses": "fieldbosses_last_ok", "server_list": "servers_last_ok"}


def source_ages(now: int) -> dict:
    """Seconds since each source was last read without error; None if never (or the database is busy)."""
    ages = {}
    for name, key in SOURCES.items():
        try:
            v = db.meta_get(key)
            ages[name] = now - int(v) if v else None
        except Exception:
            ages[name] = None
    return ages


def build_app(bot) -> web.Application:
    async def health(request: web.Request) -> web.Response:
        latency = bot.latency
        online = bot.is_ready() and not bot.is_closed() and latency == latency  # NaN check
        now = int(time.time())
        sources = source_ages(now)
        loops = heartbeat.snapshot(now)
        body = {
            "status": "ok" if online else "offline",
            "gateway_latency_ms": round(latency * 1000) if latency == latency else None,
            "guilds": len(bot.guilds),
            "uptime_seconds": int(time.time() - STARTED),
            "timers_last_ok_age_seconds": sources["timers"],
            "stale_loops": loops["stale"],
            "loops": loops["loops"],
            "sources_last_ok_age_seconds": sources,
        }
        return web.Response(
            text=json.dumps(body), content_type="application/json", status=200 if online else 503
        )

    app = web.Application()
    app.router.add_get("/health", health)
    return app


async def start(bot) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.HEALTH_PORT)
    await site.start()
    log.info("Health endpoint listening on port %s", config.HEALTH_PORT)
    return runner
