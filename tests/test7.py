import asyncio, glob, os, sys, tempfile
from datetime import datetime, timezone, timedelta
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import config, db, timers
UTC = timezone.utc
page = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "gamers4life_events_page.html"), encoding="utf-8").read()

rows = timers.parse_table(page)
assert len(rows) == 13 and rows[0] == ("Spacetime Rift", "Rifts", "every 3h from 02:00"), rows[:2]
assert ("Executor Argo", "World Bosses", "Mon, Thu, Sat at 21:30") in rows
assert timers.parse_page_meta(page) == ("Global", 0)
assert timers.parse_page_meta("<h2>Full schedule (server time · GMT+8)<!-- --> · <!-- -->Korea</h2>") == ("Korea", 480)
assert timers.parse_page_meta("<h2>Full schedule (server time · UTC-5) · Taiwan</h2>") == ("Taiwan", -300)
assert timers.parse_page_meta("<h2>Full schedule (server time · UTC)</h2>") == (None, 0)
assert timers.parse_page_meta("<html>nothing</html>") == (None, None)
assert all(timers.parse_rule(r[2]) for r in rows)          # every timing on the real page is understood

# UTC clock times: Spacetime Rift every 3h from 02:00 UTC, Mon/Thu/Sat 21:30 UTC bosses
start = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)            # a Wednesday
occ = timers.occurrences(timers.parse_rule("every 3h from 02:00"), start, start + timedelta(days=1))
assert [o.hour for o in occ] == [2, 5, 8, 11, 14, 17, 20, 23], [o.hour for o in occ]
wk = timers.occurrences(timers.parse_rule("Mon, Thu, Sat at 21:30"), start, start + timedelta(days=8))
assert [o.strftime("%a %H:%M") for o in wk] == ["Thu 21:30", "Sat 21:30", "Mon 21:30"], wk

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

async def main():
    db.init()
    assert db.meta_get("timers_etag") is None                # fresh DB: nothing cached
    db.meta_set("timers_etag", "old-etag")
    db.init(os.environ["DB_PATH"])                           # restart before the timezone was ever stored
    assert db.meta_get("timers_etag") is None                # forced a full re-read
    cog = timers.Timers.__new__(timers.Timers); cog.bot = Bot(); cog.session = Session(page)
    assert await cog.fetch_and_store() == "ok"
    assert db.meta_get("timers_tz_min") == "0" and db.meta_get("timers_region") == "Global"
    assert db.conn().execute("SELECT COUNT(*) c FROM timer_rules").fetchone()["c"] == 13
    n = db.conn().execute("SELECT COUNT(*) c FROM timer_events").fetchone()["c"]; assert n > 100
    assert db.meta_get("timers_last_new") is None            # nothing announced on the first read
    # the same page seen after the bot used to assume another region: no announcement spam either
    db.conn().execute("DELETE FROM timer_rules"); db.conn().commit()
    db.meta_set("timers_region", "Taiwan"); db.meta_set("timers_tz_min", "480")
    assert await cog.fetch_and_store() == "ok" and db.meta_get("timers_tz_min") == "0"
    assert db.meta_get("timers_last_new") is None
    # a page that switches to another region is refused and the good schedule stays
    kr = page.replace("<!-- -->Global</h2>", "<!-- -->Korea</h2>").replace("(server time · UTC)", "(server time · GMT+9)")
    assert "Korea" in kr
    cog.session = Session(kr)
    assert await cog.fetch_and_store() == "error"
    assert "Korea" in db.meta_get("timers_last_error") and db.meta_get("timers_tz_min") == "0"
    assert db.conn().execute("SELECT COUNT(*) c FROM timer_rules").fetchone()["c"] == 13
    assert db.meta_get("timers_etag") is None
    # next scraped time uses UTC
    nxt = timers.scraped_next("every 3h from 02:00", datetime(2026, 9, 30, 0, 30, tzinfo=UTC))
    assert datetime.fromtimestamp(nxt, UTC).hour == 2
    print("REGION OK")
asyncio.run(main())
