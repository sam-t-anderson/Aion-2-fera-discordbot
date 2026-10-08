"""Small helpers shared by several features: time parsing, timezones, permissions."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

import config
import db

TIME_HELP = (
    "Couldn't read that time. Try `8pm`, `20:30`, `tomorrow 7:30pm`, `10/4 8pm`, "
    "`2026-10-04 20:00` or `in 2h`. Times use your timezone (change it with `/timezone set`)."
)


def get_tz(user_id: int) -> ZoneInfo:
    row = db.conn().execute("SELECT tz FROM user_prefs WHERE user_id=?", (user_id,)).fetchone()
    name = row["tz"] if row and row["tz"] else config.DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def parse_when(text: str, tz, now: datetime | None = None) -> datetime:
    """Turn what a person typed into a UTC datetime. Raises ValueError with a helpful message."""
    now = now or datetime.now(timezone.utc)
    s = " ".join((text or "").lower().split())
    if not s:
        raise ValueError(TIME_HELP)

    if re.fullmatch(r"\d{10}", s):
        result = datetime.fromtimestamp(int(s), timezone.utc)
    else:
        rel = re.fullmatch(r"(?:in\s+|\+)?((?:\d+\s*[dhm]\s*)+)", s)
        if rel:
            total = timedelta()
            for n, unit in re.findall(r"(\d+)\s*([dhm])", rel.group(1)):
                n = int(n)
                total += timedelta(days=n) if unit == "d" else timedelta(hours=n) if unit == "h" else timedelta(minutes=n)
            if total <= timedelta(0):
                raise ValueError(TIME_HELP)
            result = now + total
        else:
            result = _parse_absolute(s, tz, now)

    if result > now + timedelta(days=90):
        raise ValueError("That is more than 90 days away. Pick a closer date.")
    return result


def _parse_absolute(s: str, tz, now: datetime) -> datetime:
    local_now = now.astimezone(tz)
    rest = s
    date = None
    explicit = False

    m = re.search(r"\b(today|tomorrow)\b", rest)
    if m:
        date = local_now.date() + (timedelta(days=1) if m.group(1) == "tomorrow" else timedelta(0))
        explicit = True
        rest = rest.replace(m.group(0), " ", 1)

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", rest)
    if m:
        try:
            date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            raise ValueError(TIME_HELP)
        explicit = True
        rest = rest.replace(m.group(0), " ", 1)
    else:
        m = re.search(r"(?<![\d:])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d:])", rest)
        if m:
            year = int(m.group(3)) if m.group(3) else local_now.year
            if year < 100:
                year += 2000
            try:
                date = datetime(year, int(m.group(1)), int(m.group(2))).date()
            except ValueError:
                raise ValueError(TIME_HELP)
            if not m.group(3) and date < local_now.date():
                date = date.replace(year=date.year + 1)
            explicit = True
            rest = rest.replace(m.group(0), " ", 1)

    tm = re.search(r"(?<![\d/-])(\d{1,2}):(\d{2})\s*(am|pm)?(?!\d)|(?<![\d/-])(\d{1,2})\s*(am|pm)\b", rest)
    if not tm:
        raise ValueError(TIME_HELP)
    hour = int(tm.group(1) or tm.group(4))
    minute = int(tm.group(2) or 0)
    ampm = tm.group(3) or tm.group(5)
    rest = rest.replace(tm.group(0), " ", 1)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(TIME_HELP)
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif not 0 <= hour <= 23:
        raise ValueError(TIME_HELP)
    if not 0 <= minute <= 59:
        raise ValueError(TIME_HELP)

    leftover = re.sub(r"\b(at|on)\b|[@,]", " ", rest).strip()
    if leftover:
        raise ValueError(TIME_HELP)

    day = date or local_now.date()
    cand = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if not explicit and cand <= now:
        day = day + timedelta(days=1)
        cand = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if explicit and cand < now - timedelta(minutes=1):
        raise ValueError("That time has already passed.")
    return cand.astimezone(timezone.utc)


def parse_duration(text: str) -> timedelta:
    """Read a length like 4h, 90m, 1d2h or 'in 4h30m'. Raises ValueError."""
    s = " ".join((text or "").lower().split())
    m = re.fullmatch(r"(?:in\s+|\+)?((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m)\s*)+)", s)
    if not m:
        raise ValueError("Use a length like `4h`, `90m` or `1d2h`.")
    total = timedelta()
    for n, unit in re.findall(r"(\d+)\s*(d|h|m)", m.group(1)):
        n = int(n)
        total += timedelta(days=n) if unit == "d" else timedelta(hours=n) if unit == "h" else timedelta(minutes=n)
    if total <= timedelta(0):
        raise ValueError("Use a length like `4h`, `90m` or `1d2h`.")
    return total


def ts(value, style: str = "f") -> str:
    """Discord timestamp markup. Each person sees it in their own timezone."""
    if isinstance(value, datetime):
        value = int(value.timestamp())
    return f"<t:{int(value)}:{style}>"


def is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.administrator))


def cut(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
