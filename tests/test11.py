import asyncio, os, sys, tempfile
from unittest.mock import MagicMock, AsyncMock
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import discord
from discord.ext import commands
import db, timers
async def main():
    db.init(); db.ensure_guild(1)
    db.conn().execute("INSERT INTO timer_rules (name, category, timing) VALUES ('Executor Argo','World Bosses','Mon, Thu, Sat at 21:30')"); db.conn().commit()
    timers.purge_old()
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    cog = timers.Timers(bot)
    nxt = min(t for n, _c, t in timers.events_between(1, int(__import__("time").time()), int(__import__("time").time()) + 8 * 86400) if n == "Executor Argo")
    when = __import__("datetime").datetime.fromtimestamp(nxt + 900, __import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M")
    for kw in ({}, {"only_this_time": True}):
        it = MagicMock(); it.guild_id = 1; it.user.id = 5; it.response.send_message = AsyncMock()
        await timers.Timers.set_timer.callback(cog, it, name="executor argo", when=when, **kw)
        print(it.response.send_message.await_args.args[0][:230]); print()
    for tr in list(bot.cogs): pass
    names = [o.name for o in timers.Timers.set_timer.parameters]; assert "only_this_time" in names; print("params", names)
asyncio.run(main())
