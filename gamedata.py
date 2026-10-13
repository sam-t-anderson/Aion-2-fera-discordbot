"""Game facts that don't come from the timers site: server regions and field bosses.

Field bosses are read from ``assets/field-bosses.json`` — the same community respawn
reference the Aion 2 tools client/Pages ship (`aion2calc/app/static/field-bosses.json`),
vendored here so the bot names the same bosses with the same factions and respawns.
Re-sync by copying that file over ``assets/field-bosses.json``. If the file is missing
or unreadable, the bot falls back to the built-in list below so timers never break.

Names are spelled as in the Global client. Field boss respawns count from the kill."""

import json
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
    ("High Commander Lagta", "Verteron", 360), ("Eternal Gartua", "Verteron", 360),
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
    ("High Commander Lagta", "Altgard", 720), ("Immortal Gartua", "Altgard", 720),
]
# Contested Abyss / Reshanta field bosses (shown to both factions). (name, zone, schedule text)
_BUILTIN_ABYSS = [
    ("Watcher Kaira", "Chaotic Lower Reshanta", "every 3h from 01:00 server time"),
    ("Executor Tamasa", "Chaotic Lower Reshanta", "Mon, Thu, Sat 21:30 server time (after Artifact Siege)"),
    ("Executor Argo", "Siel's Heart", "Mon, Thu, Sat 21:30 server time (after Artifact Siege)"),
    ("Executor Kaira", "Branding Island", "Mon, Thu, Sat 21:30 server time (after Artifact Siege)"),
    ("Guardian Lord Nahma", "Chaotic Lower Reshanta", "Fri and Sun 21:00 server time"),
    ("Argo the Spirit King", "Chaotic Lower Reshanta", "no reliable community timer yet — confirm in game"),
]

_FIELD_BOSSES_JSON = Path(__file__).resolve().parent / "assets" / "field-bosses.json"


def _short_zone(zone: str) -> str:
    """The area/region prefix of a JSON zone, e.g. "Verteron · Cantas Valley" -> "Verteron".
    This is what disambiguates a boss that lives in both factions' zones."""
    return (zone or "").split("·")[0].strip() or (zone or "")


def _load():
    """Return (field_bosses, abyss_bosses, faction_by_key) from the shared JSON, or the
    built-in fallback. ``field_bosses`` are ``(name, short_zone, minutes)`` non-Abyss rows;
    ``abyss_bosses`` are ``(name, zone, schedule_text)``; ``faction_by_key`` maps
    ``(name, short_zone)`` to the boss's faction for ``field_boss_factions``."""
    try:
        doc = json.loads(_FIELD_BOSSES_JSON.read_text(encoding="utf-8"))
        rows = doc["bosses"]
        if not isinstance(rows, list) or len(rows) < 20:
            raise ValueError("field-bosses.json has too few bosses to trust")
        field, abyss, faction = [], [], {}
        for r in rows:
            name, fac, zone = str(r["name"]).strip(), r.get("faction"), r.get("zone") or ""
            mins = r.get("respawn_minutes")
            if fac == "abyss" or mins is None:
                abyss.append((name, zone, r.get("schedule") or "community schedule — confirm in game"))
            else:
                short = _short_zone(zone)
                field.append((name, short, int(mins)))
                faction[(name, short)] = fac or ZONE_FACTION.get(short, "both")
        if not field:
            raise ValueError("field-bosses.json has no respawn-timed field bosses")
        return field, abyss, faction
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        field = list(_BUILTIN_FIELD_BOSSES)
        faction = {(n, z): ZONE_FACTION.get(z, "both") for n, z, _m in field}
        return field, list(_BUILTIN_ABYSS), faction


FIELD_BOSSES, ABYSS_BOSSES, _FIELD_FACTION = _load()


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
