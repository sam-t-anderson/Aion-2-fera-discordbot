"""Game facts that don't come from the timers site: server regions and field bosses.

Field bosses are read from ``assets/field-bosses.json`` — the same community respawn
reference the Aion 2 tools client/Pages ship (`aion2calc/app/static/field-bosses.json`),
vendored here so the bot names the same bosses with the same factions and respawns.
Re-sync by copying that file over ``assets/field-bosses.json``. If the file is missing
or unreadable, the bot falls back to the built-in list below so timers never break.

Names are spelled as in the Global client. Field boss respawns count from the kill."""

import json
import re
from pathlib import Path

# Each Global region runs the same schedule on its own local server clock (the timers
# site lists these same regions). Key, short label, full label, timezone of the clock.
REGIONS = {
    "na": ("NA East", "North America (East)", "America/New_York"),
    "naw": ("NA West", "North America (West)", "America/Los_Angeles"),
    "eu": ("EU", "Europe", "Europe/Berlin"),
    "sa": ("SA", "South America", "America/Sao_Paulo"),
    "jp": ("JP", "Japan", "Asia/Tokyo"),
}
# The official site's code for each region's server list. Japan is assumed to be in its Asia list.
OFFICIAL_REGIONS = {"na": "nae", "naw": "naw", "eu": "eu", "sa": "la", "jp": "as"}
# Regions the site also offers that follow a different schedule; their page is not accepted.
OTHER_REGIONS = ("Taiwan", "Korea")

# Official schedule changes the timers site may not show yet, from the Global patch notes. Each is (timer name,
# the timing the site showed before the change, the official timing, source). The official timing is used only
# while the site still shows the old one: once the site follows the change, or shows anything else, the site's
# own timing is used again. Clock times are each region's own server time, like the site's, so every region
# keeps its local time (daylight saving included).
PATCH_2026_11_04 = "Patch notes, 3 Nov (PST) / 4 Nov (CET) 2026"
SCHEDULE_CHANGES = (
    # "The Artifact Siege schedule will be changed from three times a week to twice a week (Thursdays and
    # Saturdays) ... at the same time as before, 21:00", and the three Artifact Bosses with it, at 21:30.
    ("Artifact Siege", "Mon, Thu, Sat at 21:00", "Thu, Sat at 21:00", PATCH_2026_11_04),
    ("Executor Tamasa", "Mon, Thu, Sat at 21:30", "Thu, Sat at 21:30", PATCH_2026_11_04),
    ("Executor Argo", "Mon, Thu, Sat at 21:30", "Thu, Sat at 21:30", PATCH_2026_11_04),
    ("Executor Kaira", "Mon, Thu, Sat at 21:30", "Thu, Sat at 21:30", PATCH_2026_11_04),
)
# Officially scheduled timers the site does not list: (name, category, timing, source). The site's own row is
# used once it lists the timer.
SCHEDULE_ADDITIONS = (
    # "The respawn time for the Abyss Field Boss Argo, the Spirit King will be changed. Will spawn twice daily at
    # 10:30 and 22:30."
    ("Argo, the Spirit King", "World Bosses", "daily at 10:30, 22:30", PATCH_2026_11_04),
)

# Which faction each field-boss starting zone belongs to. Used only as a fallback for the
# built-in list below; the shared JSON carries an explicit faction per boss.
ZONE_FACTION = {"Verteron": "elyos", "Altgard": "asmodian"}
FACTIONS = ("elyos", "asmodian")
FACTION_LABELS = {"elyos": "Elyos", "asmodian": "Asmodian"}

# Built-in fallback — used only when assets/field-bosses.json can't be read. (name, zone, respawn minutes)
_BUILTIN_FIELD_BOSSES = [
    ("Kernon of the West", "Verteron", 30), ("Neikel of the East", "Verteron", 30),
    ("Rotten Kutar", "Verteron", 30), ("Blooming Korin", "Verteron", 60),
    ("Bodyguard Teegant", "Verteron", 90), ("Kusan the Mad Gladiator", "Verteron", 120),
    ("Ritualist Garshim", "Verteron", 120), ("Bloodfang Pnyn", "Verteron", 180),
    ("Furious Saursus", "Verteron", 180), ("Scholar Aulla", "Verteron", 120),
    ("Chaser Taulo", "Verteron", 120), ("Forest Warrior Aullamu", "Verteron", 120),
    ("Heretic Layla", "Verteron", 180), ("Black Tentacle Lawa", "Verteron", 120),
    ("Centurion Demiros", "Verteron", 120), ("Divine Ansas", "Verteron", 360),
    ("Harvest Manager Moshav", "Verteron", 180), ("Sentinel K'nash", "Verteron", 120),
    ("Researcher Setram", "Verteron", 180), ("Phantasm Kasia", "Verteron", 360),
    ("Silent Dartan", "Verteron", 180), ("Soul Ruler Kashapa", "Verteron", 360),
    # 11 h for both factions since the 4 November 2026 patch (12 h before, the notes say)
    ("High Commander Lagta", "Verteron", 660), ("Eternal Gartua", "Verteron", 660),
    ("Melted Danar", "Altgard", 30), ("Black Warrior Aed", "Altgard", 30),
    ("Faithful Rajit", "Altgard", 30), ("Berserker Vargor", "Altgard", 60),
    ("Blood Warrior Lannar", "Altgard", 90), ("Predator Garsan", "Altgard", 120),
    ("Deceiver Trid", "Altgard", 120), ("Blue Wave Kelpina", "Altgard", 120),
    ("Advisor Resana", "Altgard", 180), ("High Overseer Nutah", "Altgard", 120),
    ("Special Operations Leader Linx", "Altgard", 180), ("Desecrator Newbold", "Altgard", 240),
    ("Specter Archon Axios", "Altgard", 240), ("Addicted Hardirun", "Altgard", 180),
    ("Executioner Barthien", "Altgard", 240), ("Drakan Battalion Weapon Guruta", "Altgard", 360),
    ("Veteran Shujakan", "Altgard", 180), ("Visionary Karuka", "Altgard", 240),
    ("Dark Shadow Vishwada", "Altgard", 360), ("Sharp Shylak", "Altgard", 360),
    ("Silent Dartan", "Altgard", 360), ("Soul Ruler Kashapa", "Altgard", 360),
    ("High Commander Lagta", "Altgard", 660), ("Immortal Gartua", "Altgard", 660),
]
# Contested Abyss / Reshanta field bosses (shown to both factions). (name, zone, schedule text)
_BUILTIN_ABYSS = [
    ("Watcher Kaira", "Chaotic Lower Reshanta", "every 3h from 01:00 server time"),
    ("Executor Tamasa", "Chaotic Lower Reshanta", "Thu and Sat 21:30 server time (after Artifact Siege)"),
    ("Executor Argo", "Siel's Heart", "Thu and Sat 21:30 server time (after Artifact Siege)"),
    ("Executor Kaira", "Branding Island", "Thu and Sat 21:30 server time (after Artifact Siege)"),
    ("Guardian Lord Nahma", "Chaotic Lower Reshanta", "Fri and Sun 21:00 server time"),
    ("Argo, the Spirit King", "Chaotic Lower Reshanta", "daily at 10:30 and 22:30 server time"),
]

_FIELD_BOSSES_JSON = Path(__file__).resolve().parent / "assets" / "field-bosses.json"


def _short_zone(zone: str) -> str:
    """The area/region prefix of a JSON zone, e.g. "Verteron · Cantas Valley" -> "Verteron".
    This is what disambiguates a boss that lives in both factions' zones."""
    return (zone or "").split("·")[0].strip() or (zone or "")


def parse(doc):
    """Validate a field-bosses document and return (field_bosses, abyss_bosses, faction_by_key).
    ``field_bosses`` are ``(name, short_zone, minutes)`` non-Abyss rows; ``abyss_bosses`` are
    ``(name, zone, schedule_text)``; ``faction_by_key`` maps ``(name, short_zone)`` to the boss's
    faction for ``field_boss_factions``. Raises ValueError for anything not worth trusting."""
    if not isinstance(doc, dict):
        raise ValueError("field bosses must be a JSON object")
    rows = doc.get("bosses")
    if not isinstance(rows, list) or len(rows) < 20 or len(rows) > 2000:
        raise ValueError("field-bosses.json has too few (or too many) bosses to trust")
    field, abyss, faction = [], [], {}
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not r["name"].strip():
            raise ValueError("every boss needs a name")
        name, fac, zone = " ".join(r["name"].split())[:60], r.get("faction"), r.get("zone") or ""
        if not isinstance(zone, str) or fac not in (None, "elyos", "asmodian", "both", "abyss"):
            raise ValueError(f"bad zone or faction for {name}")
        mins = r.get("respawn_minutes")
        if fac == "abyss" or mins is None:
            sched = r.get("schedule")
            abyss.append((name, zone[:80], (sched if isinstance(sched, str) and sched.strip() else "community schedule — confirm in game")[:120]))
        else:
            if isinstance(mins, bool) or not isinstance(mins, (int, float)) or not 1 <= mins <= 60 * 24 * 14:
                raise ValueError(f"bad respawn for {name}")
            short = _short_zone(zone)
            field.append((name, short, int(mins)))
            faction[(name, short)] = fac or ZONE_FACTION.get(short, "both")
    if not field:
        raise ValueError("field-bosses.json has no respawn-timed field bosses")
    return field, abyss, faction


def _load():
    """The tables from the bundled JSON, or the built-in fallback if it can't be read."""
    try:
        return parse(json.loads(_FIELD_BOSSES_JSON.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        field = list(_BUILTIN_FIELD_BOSSES)
        faction = {(n, z): ZONE_FACTION.get(z, "both") for n, z, _m in field}
        return field, list(_BUILTIN_ABYSS), faction


def checked_at(doc) -> str:
    """The day a field-bosses document says it was last checked ("YYYY-MM-DD"), or "" if it doesn't say."""
    value = doc.get("checked_at") if isinstance(doc, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else ""


def _bundled_checked_at() -> str:
    try:
        doc = json.loads(_FIELD_BOSSES_JSON.read_text(encoding="utf-8"))
        parse(doc)
        return checked_at(doc)
    except (OSError, ValueError, KeyError, TypeError):
        return ""                                     # the built-in list: any valid published copy is newer


FIELD_BOSSES, ABYSS_BOSSES, _FIELD_FACTION = _load()
# When the bundled file was checked. fieldbosses.py doesn't use a published copy checked before it.
BUNDLED_CHECKED_AT = _bundled_checked_at()


def apply(doc) -> int:
    """Replace the live tables with a validated document, in place, so every module holding
    FIELD_BOSSES / ABYSS_BOSSES keeps working (timers.py refreshes its derived maps itself).
    Returns the number of bosses taken. An invalid document raises and changes nothing."""
    return _swap(*parse(doc))


def apply_bundled() -> int:
    """Put the bundled tables (or the built-in fallback) back in place, like ``apply``."""
    return _swap(*_load())


def _swap(field, abyss, faction) -> int:
    FIELD_BOSSES[:] = field
    ABYSS_BOSSES[:] = abyss
    _FIELD_FACTION.clear()
    _FIELD_FACTION.update(faction)
    return len(field) + len(abyss)


def field_boss_names() -> list[str]:
    """Every field boss once, for the event planner's boss list."""
    return sorted({name for name, _zone, _mins in FIELD_BOSSES})


def field_boss_factions() -> dict[str, str]:
    """Display name (matching field_boss_respawns' keys) -> faction. A boss that lives in
    both factions' zones with the same respawn (one un-suffixed key) is marked "both"."""
    mins_by_name: dict[str, set] = {}
    for name, _zone, mins in FIELD_BOSSES:
        mins_by_name.setdefault(name, set()).add(mins)
    facs: dict[str, set] = {}
    for name, zone, _mins in FIELD_BOSSES:
        key = name if len(mins_by_name[name]) == 1 else f"{name} ({zone})"
        facs.setdefault(key, set()).add(_FIELD_FACTION.get((name, zone), "both"))
    return {k: (next(iter(v)) if len(v) == 1 else "both") for k, v in facs.items()}


def field_boss_respawns() -> dict[str, int]:
    """Timer name -> respawn minutes. A boss that lives in both zones with different
    respawns gets one timer per zone, like "Silent Dartan (Altgard)"."""
    by_name: dict[str, set[int]] = {}
    for name, _zone, mins in FIELD_BOSSES:
        by_name.setdefault(name, set()).add(mins)
    out = {}
    for name, zone, mins in FIELD_BOSSES:
        out[name if len(by_name[name]) == 1 else f"{name} ({zone})"] = mins
    return out
