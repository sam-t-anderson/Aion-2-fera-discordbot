"""Field bosses show on the board / in /timers show, filtered by the server's faction,
plus the contested Abyss bosses for everyone. /setup faction stores the choice."""
import asyncio, os, sys, tempfile
from unittest.mock import MagicMock, AsyncMock
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import discord
from discord.ext import commands
import db, guilds, timer_board


def _text(embed):
    return "\n".join(f.value for f in embed.fields)


async def main():
    db.init(); db.ensure_guild(1)
    db.conn().execute("INSERT INTO timer_rules (name, category, timing) VALUES ('Kromede','World Bosses','daily at 20:00')")
    db.conn().commit()

    # No faction set: both factions' zone field bosses show, plus the Abyss list.
    e = timer_board.build_embed(1); t = _text(e)
    assert "Kernon of the West" in t, "Verteron/Elyos boss missing"
    assert "Melted Danar" in t, "Altgard/Asmodian boss missing"
    assert "Watcher Kaira" in t and "Argo, the Spirit King" in t and "10:30 and 22:30" in t, "Abyss boss missing"
    assert "not seen yet" in t and "after kill" in t
    cats = {f.name.replace(" (cont.)", "") for f in e.fields}
    assert {"World Bosses", "Field Bosses", "Abyss (contested)"} <= cats, cats

    # Faction Elyos: Altgard (Asmodian) bosses drop out; Verteron + Abyss stay.
    db.set_settings(1, faction="elyos")
    t = _text(timer_board.build_embed(1))
    assert "Kernon of the West" in t and "Watcher Kaira" in t
    assert "Melted Danar" not in t and "Black Warrior Aed" not in t

    # Faction Asmodian: the mirror image.
    db.set_settings(1, faction="asmodian")
    t = _text(timer_board.build_embed(1))
    assert "Melted Danar" in t and "Kernon of the West" not in t
    # A boss that lives in both zones with the same respawn is contested -> shows either way.
    assert "Soul Ruler Kashapa" in t

    # /setup faction stores the choice (and empty clears it back to both).
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    cog = guilds.ServerSetup(bot)
    it = MagicMock(); it.guild_id = 1
    it.user.guild_permissions.manage_guild = True; it.user.guild_permissions.administrator = False
    it.response.send_message = AsyncMock()
    await guilds.ServerSetup.faction.callback(cog, it, faction="elyos")
    assert db.get_setting(1, "faction") == "elyos"
    await guilds.ServerSetup.faction.callback(cog, it, faction=None)
    assert db.get_setting(1, "faction") is None
    print("OK")


asyncio.run(main())
