"""Game facts that don't come from the timers site: server regions and field bosses.

Names are spelled as in the Global client. Field boss respawns count from the kill."""

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

# (name, zone, respawn in minutes)
FIELD_BOSSES = [
    ("Kernon of the West", "Verteron", 30),
    ("Neikel of the East", "Verteron", 30),
    ("Rotten Kutar", "Verteron", 30),
    ("Blooming Korin", "Verteron", 60),
    ("Bodyguard Teegant", "Verteron", 90),
    ("Kusan the Mad Gladiator", "Verteron", 120),
    ("Ritualist Garshim", "Verteron", 120),
    ("Bloodfang Pnyn", "Verteron", 180),
    ("Furious Saursus", "Verteron", 180),
    ("Scholar Aulla", "Verteron", 120),
    ("Chaser Taulo", "Verteron", 120),
    ("Forest Warrior Aullamu", "Verteron", 120),
    ("Heretic Layla", "Verteron", 180),
    ("Black Tentacle Lawa", "Verteron", 120),
    ("Centurion Demiros", "Verteron", 120),
    ("Divine Ansas", "Verteron", 360),
    ("Harvest Manager Moshav", "Verteron", 180),
    ("Sentinel K'nash", "Verteron", 120),
    ("Researcher Setram", "Verteron", 180),
    ("Phantasm Kasia", "Verteron", 360),
    ("Silent Dartan", "Verteron", 180),
    ("Soul Ruler Kashapa", "Verteron", 360),
    ("High Commander Lagta", "Verteron", 360),
    ("Eternal Gartua", "Verteron", 360),
    ("Melted Danar", "Altgard", 30),
    ("Black Warrior Aed", "Altgard", 30),
    ("Faithful Rajit", "Altgard", 30),
    ("Berserker Vargor", "Altgard", 60),
    ("Blood Warrior Lannar", "Altgard", 90),
    ("Predator Garsan", "Altgard", 120),
    ("Deceiver Trid", "Altgard", 120),
    ("Blue Wave Kelpina", "Altgard", 120),
    ("Advisor Resana", "Altgard", 180),
    ("High Overseer Nutah", "Altgard", 120),
    ("Special Operations Leader Linx", "Altgard", 180),
    ("Desecrator Newbold", "Altgard", 240),
    ("Specter Archon Axios", "Altgard", 240),
    ("Addicted Hardirun", "Altgard", 180),
    ("Executioner Barthien", "Altgard", 240),
    ("Drakan Battalion Weapon Guruta", "Altgard", 360),
    ("Veteran Shujakan", "Altgard", 180),
    ("Visionary Karuka", "Altgard", 240),
    ("Dark Shadow Vishwada", "Altgard", 360),
    ("Sharp Shylak", "Altgard", 360),
    ("Silent Dartan", "Altgard", 360),
    ("Soul Ruler Kashapa", "Altgard", 360),
    ("High Commander Lagta", "Altgard", 720),
    ("Immortal Gartua", "Altgard", 720),
]


# Which faction each field-boss starting zone belongs to. Bosses whose zone isn't here
# (the contested Abyss list below) are shown to both factions.
ZONE_FACTION = {"Verteron": "elyos", "Altgard": "asmodian"}
FACTIONS = ("elyos", "asmodian")
FACTION_LABELS = {"elyos": "Elyos", "asmodian": "Asmodian"}

# Contested Abyss / Reshanta field bosses. Unlike the zone field bosses these follow a
# community schedule rather than a respawn-after-kill, and are shown to both factions.
# Community-sourced timers (confirm in game); spelled as in the Global client.
# (name, zone, community schedule text)
ABYSS_BOSSES = [
    ("Watcher Kaira", "Chaotic Lower Reshanta", "every 3h from 01:00 server time"),
    ("Executor Tamasa", "Chaotic Lower Reshanta", "Mon, Thu, Sat 21:30 server time (after Artifact Siege)"),
    ("Executor Argo", "Siel's Heart", "Mon, Thu, Sat 21:30 server time (after Artifact Siege)"),
    ("Executor Kaira", "Branding Island", "Mon, Thu, Sat 21:30 server time (after Artifact Siege)"),
    ("Guardian Lord Nahma", "Chaotic Lower Reshanta", "Fri and Sun 21:00 server time"),
    ("Argo the Spirit King", "Chaotic Lower Reshanta", "no reliable community timer yet — confirm in game"),
]


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
        facs.setdefault(key, set()).add(ZONE_FACTION.get(zone, "both"))
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
