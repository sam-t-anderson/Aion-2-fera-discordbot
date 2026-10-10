"""Settings read from environment variables (the .env file)."""

import os


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, "").strip() or default
    return [p.strip() for p in raw.split(",") if p.strip()]


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()
ANNOUNCE_CHANNEL_ID = os.getenv("ANNOUNCE_CHANNEL_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "/data/checklist.db")

# Checklist resets, used only while the timers site lists no "Daily Reset" / "Weekly Reset"
# (normally the checklist follows those timers). RESET_UTC_HOUR 13 = 7:00 AM MDT / 6:00 AM MST.
RESET_UTC_HOUR = _int("RESET_UTC_HOUR", 13)
WEEKLY_RESET_WEEKDAY = _int("WEEKLY_RESET_WEEKDAY", 0)  # Monday=0 ... Sunday=6

# Event planner
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "America/Denver").strip() or "America/Denver"
EVENT_ALERT_MINUTES = _int("EVENT_ALERT_MINUTES", 15)

# Timers scraped from gamers4.life
TIMERS_URL = os.getenv("TIMERS_URL", "https://gamers4.life/aion-2/database/en/events/").strip()
# "*" tracks every category the site lists, including ones added later.
# Which game version's schedule to use. The page states it; anything else is refused.
TIMERS_REGION = os.getenv("TIMERS_REGION", "Global").strip() or "Global"
# The region clock a Discord server follows until its managers pick one with /setup region:
# na (NA East), naw (NA West), eu, sa or jp.
TIMERS_DEFAULT_REGION = os.getenv("TIMERS_DEFAULT_REGION", "na").strip().lower() or "na"
TIMER_CATEGORIES = _csv("TIMER_CATEGORIES", "*")
TIMER_REFRESH_MINUTES = max(5, _int("TIMER_REFRESH_MINUTES", 15))
TIMER_BOARD_MINUTES = max(1, _int("TIMER_BOARD_MINUTES", 2))
DKP_BOARD_MINUTES = max(1, _int("DKP_BOARD_MINUTES", 2))
TIMER_RETENTION_DAYS = _int("TIMER_RETENTION_DAYS", 7)
USER_AGENT = "AION2DiscordBot/2.0 (personal server bot; checks every 15 min)"

# Health endpoint for Uptime Kuma / Homepage (port inside the container)
HEALTH_PORT = _int("HEALTH_PORT", 8080)
