"""Tiny web endpoint so Uptime Kuma, Homepage and Docker can see if the bot is online."""

import json
import logging
import time

from aiohttp import web

import config
import db

log = logging.getLogger("aion2.health")
STARTED = time.time()


def build_app(bot) -> web.Application:
    async def health(request: web.Request) -> web.Response:
        latency = bot.latency
        online = bot.is_ready() and not bot.is_closed() and latency == latency  # NaN check
        last_ok = None
        try:
            v = db.meta_get("timers_last_ok")
            last_ok = int(v) if v else None
        except Exception:
            pass
        body = {
            "status": "ok" if online else "offline",
            "gateway_latency_ms": round(latency * 1000) if latency == latency else None,
            "guilds": len(bot.guilds),
            "uptime_seconds": int(time.time() - STARTED),
            "timers_last_ok_age_seconds": (int(time.time()) - last_ok) if last_ok else None,
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
