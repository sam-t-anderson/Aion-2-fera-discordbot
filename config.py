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
# The image's own user (set in the Dockerfile): the bot gives it the data folder and drops root at start.
BOT_USER = os.getenv("BOT_USER", "").strip()

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
# Open-world boss names, levels and locations (JSON feed built from the Global client). Empty turns it off.
BOSS_DATA_URL = os.getenv("BOSS_DATA_URL", "https://aion2data.com/raw/monsters.json").strip()
# Official list of game servers per region, for /setup server. Empty turns it off.
SERVER_LIST_URL = os.getenv("SERVER_LIST_URL", "https://aion2.plaync.com/en-us/api/gameinfo/servers").strip()
# Field boss kills found in combat logs uploaded to the log server. Each starts a respawn timer. Empty turns it off.
BOSS_KILLS_URL = os.getenv("BOSS_KILLS_URL", "https://island-1.tailbe3340.ts.net/api/v1/boss-kills").strip()
BOSS_KILLS_MINUTES = max(1, _int("BOSS_KILLS_MINUTES", 1))
# How many different uploaders must have recorded a kill before it starts a timer.
BOSS_KILLS_MIN_REPORTS = max(1, _int("BOSS_KILLS_MIN_REPORTS", 1))
# The published copy of the shared field-boss reference (the Aion 2 tools Pages site), read once a day so
# new field bosses appear without redeploying the bot. Empty keeps the bundled file only.
FIELD_BOSSES_URL = os.getenv("FIELD_BOSSES_URL", "https://sam-t-anderson.github.io/Aion-2-tools/static/field-bosses.json").strip()
# Official news (notices and updates) read from the log server's feed, posted by /setup news. Empty turns it off.
NEWS_URL = os.getenv("NEWS_URL", "https://island-1.tailbe3340.ts.net/api/v1/news").strip()
NEWS_MINUTES = max(5, _int("NEWS_MINUTES", 15))
NEWS_MAX_PER_CYCLE = max(1, _int("NEWS_MAX_PER_CYCLE", 5))
USER_AGENT = "AION2DiscordBot/2.0 (personal server bot; checks every 15 min)"

# Health endpoint for Docker, the updater and any monitoring (port inside the container)
HEALTH_PORT = _int("HEALTH_PORT", 8080)
