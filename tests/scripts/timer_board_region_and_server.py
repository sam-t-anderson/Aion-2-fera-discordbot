"""/timer-setup gained optional region + server params so an admin can pin a board to
the right game region/server in one step instead of relying on the single guild setting.
They reuse the same validation as /setup region and /setup server."""
import asyncio, os, sys, tempfile
from unittest.mock import MagicMock, AsyncMock
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import discord
from discord.ext import commands
import db, timers, gamedata


def _interaction(gid=1):
    it = MagicMock(); it.guild_id = gid
    it.user.guild_permissions.manage_guild = True
    it.user.guild_permissions.administrator = False
    it.response.defer = AsyncMock(); it.response.send_message = AsyncMock()
    it.followup.send = AsyncMock()
    it.channel.send = AsyncMock(return_value=MagicMock(id=222, channel=MagicMock(id=111)))
    return it


async def main():
    db.init(); db.ensure_guild(1)
    db.conn().execute("INSERT INTO timer_rules (name, category, timing) VALUES ('Executor Argo','World Bosses','daily at 21:30')")
    db.conn().execute("INSERT INTO game_servers (server_id, region, name, race) VALUES ('eu-siel','eu','Siel',0)")
    db.conn().commit()
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    cog = timers.TimerBoard(bot)

    # The command now exposes the two optional params, and the region choices cover every region.
    params = {o.name: o for o in timers.TimerBoard.timer_setup.parameters}
    assert "region" in params and "server" in params, list(params)
    assert {c.value for c in params["region"].choices} == set(gamedata.REGIONS)

    # region + a valid server: both are stored and the board is posted in one step.
    it = _interaction()
    await timers.TimerBoard.timer_setup.callback(cog, it, region="eu", server="siel")
    assert db.get_setting(1, "timer_region") == "eu", db.get_setting(1, "timer_region")
    assert db.get_setting(1, "game_server") == "eu-siel", db.get_setting(1, "game_server")
    assert it.channel.send.await_count == 1 and db.get_setting(1, "board_message") == 222
    assert "Siel" in it.followup.send.await_args.args[0]

    # An unknown server is rejected: no board is posted and the existing board stays cleared.
    db.set_settings(1, board_channel=None, board_message=None)
    it2 = _interaction()
    await timers.TimerBoard.timer_setup.callback(cog, it2, server="does-not-exist")
    assert it2.channel.send.await_count == 0
    assert db.get_setting(1, "board_message") is None
    assert "couldn't match" in it2.followup.send.await_args.args[0].lower()

    # No params: posts the board using the current region, nothing else changed.
    it3 = _interaction()
    await timers.TimerBoard.timer_setup.callback(cog, it3)
    assert it3.channel.send.await_count == 1 and db.get_setting(1, "timer_region") == "eu"
    print("OK")


asyncio.run(main())
