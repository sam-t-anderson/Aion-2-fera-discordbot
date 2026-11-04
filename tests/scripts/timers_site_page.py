"""The timers page as the site serves it: regions, server clocks, daily rules, resets."""
import asyncio, os, sys, tempfile
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import config, db, timers, checklist, gamedata, timer_board, timer_commands
UTC = timezone.utc
page = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "gamers4life_events_page.html"), encoding="utf-8").read()

rows = timers.parse_table(page)
assert len(rows) == 13 and rows[0] == ("Spacetime Rift", "Rifts", "every 3h from 02:00"), rows[:2]
assert ("Executor Argo", "World Bosses", "Mon, Thu, Sat at 21:30") in rows
assert ("Daily Reset", "Events", "daily at 03:00") in rows and ("Weekly Reset", "Events", "Wed at 03:00") in rows
assert timers.parse_page_meta(page) == ("North America (East)", -240)
assert timers.parse_page_meta("<h2>Full schedule (server time · UTC)<!-- --> · <!-- -->Global</h2>") == ("Global", 0)
assert timers.parse_page_meta("<h2>Full schedule (server time · GMT+8)<!-- --> · <!-- -->Korea</h2>") == ("Korea", 480)
assert timers.parse_page_meta("<h2>Full schedule (server time) · Taiwan · UTC+8</h2>") == ("Taiwan", 480)
assert timers.parse_page_meta("<h2>Full schedule (server time · UTC)</h2>") == (None, 0)
assert timers.parse_page_meta("<html>nothing</html>") == (None, None)
assert timers.page_accepted("North America (East)") and timers.page_accepted("Global") and timers.page_accepted(None)
assert not timers.page_accepted("Korea") and not timers.page_accepted("Taiwan")
assert all(timers.parse_rule(r[2]) for r in rows)          # every timing on the real page is understood
assert timers.parse_rule("daily at 03:00") == {"type": "daily", "times": [[3, 0]]}
assert timers.parse_rule("daily at 19:00, 11:00") == {"type": "daily", "times": [[11, 0], [19, 0]]}

# clock times are the region's own: 02:00 in New York (UTC-4 in October) is 06:00 UTC
NY, BERLIN = ZoneInfo("America/New_York"), ZoneInfo("Europe/Berlin")
start = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)            # a Wednesday
occ = timers.occurrences(timers.parse_rule("every 3h from 02:00"), start, start + timedelta(days=1))
assert [o.hour for o in occ] == [2, 5, 8, 11, 14, 17, 20, 23], [o.hour for o in occ]
occ = timers.occurrences(timers.parse_rule("every 3h from 02:00"), start, start + timedelta(days=1), NY)
assert [o.hour for o in occ] == [0, 3, 6, 9, 12, 15, 18, 21], [o.hour for o in occ]
wk = timers.occurrences(timers.parse_rule("Mon, Thu, Sat at 21:30"), start, start + timedelta(days=8))
assert [o.strftime("%a %H:%M") for o in wk] == ["Thu 21:30", "Sat 21:30", "Mon 21:30"], wk
wk = timers.occurrences(timers.parse_rule("Mon, Thu, Sat at 21:30"), start, start + timedelta(days=8), NY)
assert [o.strftime("%a %H:%M") for o in wk] == ["Fri 01:30", "Sun 01:30", "Tue 01:30"], wk
dl = timers.occurrences(timers.parse_rule("daily at 11:00, 19:00"), start, start + timedelta(days=1), BERLIN)
assert [o.strftime("%H:%M") for o in dl] == ["09:00", "17:00"], dl
# the clock keeps its local time when daylight saving ends (New York, 1 November 2026)
dst = timers.occurrences(timers.parse_rule("daily at 03:00"), datetime(2026, 10, 31, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC), NY)
assert [o.strftime("%d %H:%M") for o in dst] == ["31 07:00", "01 08:00", "02 08:00"], dst

class Resp:
    def __init__(self, body, status=200): self.body, self.status, self.headers = body, status, {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def text(self): return self.body
class Session:
    def __init__(self, body): self.body = body
    def get(self, *a, **k): return Resp(self.body)
class Bot:
    def get_channel(self, i): return None
    async def fetch_channel(self, i): raise Exception("no channel")

def nxt(g, name, now):
    return datetime.fromtimestamp(timers.next_start(g, name, int(now.timestamp())), UTC)

async def main():
    db.init()
    cog = timer_commands.Timers.__new__(timer_commands.Timers); cog.bot = Bot(); cog.session = Session(page)
    assert await cog.fetch_and_store() == "ok"
    assert db.meta_get("timers_region") == "Global"
    assert db.conn().execute("SELECT COUNT(*) c FROM timer_rules").fetchone()["c"] == 14     # 13 + Argo, the Spirit King
    assert db.meta_get("timers_last_new") is None            # nothing announced on the first read
    # a page that switches to the Korean schedule is refused and the good schedule stays
    kr = page.replace("North America (East)", "Korea")
    cog.session = Session(kr)
    db.meta_set("timers_etag", "x")
    assert await cog.fetch_and_store() == "error"
    assert "Korea" in db.meta_get("timers_last_error")
    assert db.conn().execute("SELECT COUNT(*) c FROM timer_rules").fetchone()["c"] == 14
    assert db.meta_get("timers_etag") is None

    # each Discord server follows its own region's clock
    NA, EU = 1, 2
    db.ensure_guild(NA); db.ensure_guild(EU)
    assert timers.region_key(NA) == "na"                     # the default until /setup region
    db.set_settings(EU, timer_region="eu")
    now = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)            # a Thursday
    assert nxt(NA, "Daily Reset", now).strftime("%a %H:%M") == "Thu 07:00"
    assert nxt(EU, "Daily Reset", now).strftime("%a %H:%M") == "Thu 01:00"
    assert nxt(NA, "Weekly Reset", now).strftime("%a %H:%M") == "Wed 07:00"
    assert nxt(NA, "Arena of Tactics", now).strftime("%a %H:%M") == "Thu 15:00"
    embed = str(timer_board.build_embed(NA).to_dict())
    assert "Daily Reset" in embed and "no upcoming time found" not in embed and "NA East" in embed

    # the 4 November 2026 patch notes, over a site that still shows the old schedule: the Artifact Siege and its
    # three bosses on Thursdays and Saturdays only, Argo, the Spirit King at 10:30 and 22:30, all on each region's
    # own clock (New York is UTC-5 and Berlin UTC+1 by then)
    mon = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)            # Monday 9 November, 00:00 UTC (Sunday 19:00 in New York)
    assert ("Artifact Siege", "Abyss", "Mon, Thu, Sat at 21:00") in rows                    # what the page says
    assert nxt(NA, "Artifact Siege", mon).strftime("%a %d %H:%M") == "Fri 13 02:00"         # Thu 21:00 EST, not Mon
    assert nxt(EU, "Artifact Siege", mon).strftime("%a %d %H:%M") == "Thu 12 20:00"         # Thu 21:00 CET
    assert nxt(NA, "Executor Tamasa", mon).strftime("%a %d %H:%M") == "Fri 13 02:30"
    assert nxt(NA, "Argo, the Spirit King", mon).strftime("%a %d %H:%M") == "Mon 09 03:30"  # Sun 22:30 EST
    assert nxt(EU, "Argo, the Spirit King", mon).strftime("%a %d %H:%M") == "Mon 09 09:30"  # Mon 10:30 CET
    # a site that already shows the change, or anything else, is followed as it is
    assert timers.official_schedule([("Artifact Siege", "Abyss", "Tue at 20:00")])[0][2] == "Tue at 20:00"
    assert timers.official_schedule([("Artifact Siege", "Abyss", "Thu, Sat at 21:00")])[0][2] == "Thu, Sat at 21:00"
    assert [r[0] for r in timers.official_schedule([("Argo the Spirit King", "World Bosses", "daily at 11:00")])] == \
        ["Argo the Spirit King"]                             # listed by the site: not added twice

    # the checklist resets with those timers, and with a correction made by /timers set
    assert checklist.last_reset("daily", NA, now) == int(datetime(2026, 10, 7, 7, 0, tzinfo=UTC).timestamp())
    assert checklist.next_reset("daily", NA, now) == int(datetime(2026, 10, 8, 7, 0, tzinfo=UTC).timestamp())
    assert checklist.last_reset("weekly", NA, now) == int(datetime(2026, 10, 7, 7, 0, tzinfo=UTC).timestamp())
    assert checklist.last_reset("daily", EU, now) == int(datetime(2026, 10, 7, 1, 0, tzinfo=UTC).timestamp())
    site = timers.nearest_site_time("daily at 03:00", int(now.timestamp()), timers.guild_tz(NA))
    timers.save_manual(NA, "Daily Reset", "Events", site + 1800, None, site, None, 9, 1800)   # 30 min later, every day
    assert checklist.last_reset("daily", NA, now) == int(datetime(2026, 10, 7, 7, 30, tzinfo=UTC).timestamp())
    assert checklist.next_reset("daily", NA, now + timedelta(days=3)) == int(datetime(2026, 10, 11, 7, 30, tzinfo=UTC).timestamp())
    assert checklist.last_reset("daily", EU, now) == int(datetime(2026, 10, 7, 1, 0, tzinfo=UTC).timestamp())
    # progress ticked before the last reset no longer counts
    checklist.set_count(42, "daily", "nightmare", 2)
    assert checklist.get_counts(42, "daily", NA) == {"nightmare": 2}
    db.conn().execute("UPDATE progress SET period=?", (str(checklist.last_reset("daily", NA) - 5),)); db.conn().commit()
    assert checklist.get_counts(42, "daily", NA) == {}
    # with no reset timer on the site the fixed hour from the settings is used
    db.conn().execute("DELETE FROM timer_rules"); db.conn().commit()
    assert datetime.fromtimestamp(checklist.next_reset("daily", EU, now), UTC).hour == config.RESET_UTC_HOUR

    # field bosses are known by name with their respawn
    assert timers.canonical(NA, "eternal gartua") == ("Eternal Gartua", "Field Bosses", None)
    assert timers.field_respawn("soul ruler kashapa") == 360 and timers.field_respawn("Immortal Gartua") == 660   # 11 h
    assert timers.field_respawn("Silent Dartan (Verteron)") == 180 and timers.field_respawn("Silent Dartan (Altgard)") == 360
    assert timers.field_respawn("Silent Dartan") is None     # differs by zone, so the zone has to be picked
    picks = [c.name for c in timer_commands.Timers._name_choices(NA, "lagta")]
    assert "High Commander Lagta" in picks and not any("(" in p for p in picks), picks   # 11 h in both zones now
    assert timers.field_respawn("High Commander Lagta") == 660
    assert {"Eternal Gartua", "Silent Dartan", "Soul Ruler Kashapa", "High Commander Lagta"} <= set(gamedata.field_boss_names())
    print("REGION OK")
asyncio.run(main())
