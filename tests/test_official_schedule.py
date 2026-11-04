"""Official schedule changes over the timers site (gamedata.SCHEDULE_CHANGES / SCHEDULE_ADDITIONS, 4 November 2026).

The site answers "not modified" to the stored ETag while its page is unchanged, which would keep a schedule the bot
stored before a correction was added. So a schedule stored with older corrections is read in full once."""
import asyncio
import pathlib

import db
import timer_commands

PAGE = (pathlib.Path(__file__).resolve().parent / "fixtures" / "gamers4life_events_page.html").read_text(encoding="utf-8")


class Resp:
    status, headers = 200, {"ETag": "fresh"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return PAGE


class Session:
    def __init__(self):
        self.sent = []

    def get(self, url, headers=None, **kw):
        self.sent.append(dict(headers or {}))
        return Resp()


class Bot:
    def get_channel(self, _id):
        return None


def test_a_schedule_stored_before_the_official_changes_is_read_again(tmp_path):
    db.init(str(tmp_path / "t.db"))
    cog = timer_commands.Timers.__new__(timer_commands.Timers)
    cog.bot, cog.session = Bot(), Session()
    db.meta_set("timers_etag", "stale")                        # a page stored by a bot without these corrections
    assert asyncio.run(cog.fetch_and_store()) == "ok"
    assert "If-None-Match" not in cog.session.sent[0]
    rules = {r["name"]: r["timing"] for r in db.conn().execute("SELECT name, timing FROM timer_rules")}
    assert rules["Artifact Siege"] == "Thu, Sat at 21:00" and rules["Executor Kaira"] == "Thu, Sat at 21:30"
    assert rules["Argo, the Spirit King"] == "daily at 10:30, 22:30"
    assert asyncio.run(cog.fetch_and_store()) == "ok"           # stored with them now: the ETag is sent again
    assert cog.session.sent[1]["If-None-Match"] == "fresh"
