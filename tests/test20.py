"""Field bosses are read from the shared assets/field-bosses.json (the same reference the
Aion 2 tools client/Pages ship), not a hand-kept Python list, so the bot names the same
bosses with the same factions and respawns. Falls back to the built-in list if the file
is unreadable so timers never break."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gamedata


def main():
    doc = json.loads((gamedata._FIELD_BOSSES_JSON).read_text(encoding="utf-8"))
    rows = doc["bosses"]
    json_names = {r["name"] for r in rows}
    abyss_json = {r["name"] for r in rows if r.get("faction") == "abyss" or r.get("respawn_minutes") is None}

    # Every boss in the shared file is represented, split into respawn-timed field bosses and
    # scheduled Abyss bosses exactly as the file classifies them.
    loaded = {n for n, _z, _m in gamedata.FIELD_BOSSES} | {n for n, _z, _s in gamedata.ABYSS_BOSSES}
    assert loaded == json_names, loaded ^ json_names
    assert {n for n, _z, _s in gamedata.ABYSS_BOSSES} == abyss_json
    assert len(rows) >= 54, len(rows)

    # The shared file's values drive the zone-disambiguated respawn timers and factions.
    r = gamedata.field_boss_respawns()
    f = gamedata.field_boss_factions()
    assert r["Silent Dartan (Verteron)"] == 180 and r["Silent Dartan (Altgard)"] == 360
    assert "Silent Dartan" not in r                      # differs by zone -> must pick the zone
    assert r["Soul Ruler Kashapa"] == 360 and f["Soul Ruler Kashapa"] == "both"  # same respawn both zones -> contested
    assert f["Kernon of the West"] == "elyos" and f.get("Melted Danar") == "asmodian"

    # Fallback: an unreadable file keeps the bot working on the built-in list.
    saved = gamedata._FIELD_BOSSES_JSON
    try:
        gamedata._FIELD_BOSSES_JSON = saved.with_name("does-not-exist.json")
        field, abyss, faction = gamedata._load()
    finally:
        gamedata._FIELD_BOSSES_JSON = saved
    assert field == gamedata._BUILTIN_FIELD_BOSSES and abyss == gamedata._BUILTIN_ABYSS
    assert faction[("Kernon of the West", "Verteron")] == "elyos"

    print("FIELD BOSSES FROM JSON OK")


if __name__ == "__main__":
    main()
