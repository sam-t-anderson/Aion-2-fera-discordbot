"""Pressing a checklist button must change the count, going through discord.py's own dynamic-item dispatch."""
import asyncio, os, sys, tempfile, logging
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from unittest.mock import MagicMock, AsyncMock
import discord
from discord.ext import commands
import db, checklist

def msg_payload(view):
    return {"id": "555", "channel_id": "9", "author": {"id": "1", "username": "bot", "discriminator": "0", "avatar": None},
            "content": "", "timestamp": "2026-10-08T00:00:00+00:00", "edited_timestamp": None, "tts": False,
            "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [],
            "pinned": False, "type": 0, "flags": 64, "components": view.to_components()}

async def press(bot, uid, kind, item):
    shown = checklist.build_view(kind, checklist.get_counts(uid, kind, 1))
    ch = MagicMock(); ch.id = 9
    message = discord.Message(state=bot._connection, channel=ch, data=msg_payload(shown))
    cid = f"cl:tick:{kind}:{item}"
    it = MagicMock(); it.user.id = uid; it.message = message; it.guild_id = 1
    it.data = {"custom_id": cid, "component_type": 2}
    it.response.edit_message = AsyncMock(); it.response.defer = AsyncMock()
    bot._connection._view_store.dispatch_view(2, cid, it)
    await asyncio.sleep(0.3)
    return it

async def main():
    db.init()
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot.add_dynamic_items(checklist.OpenButton, checklist.TickButton, checklist.BulkButton)
    seen = []
    for _ in range(4):
        it = await press(bot, 42, "daily", "nightmare")
        assert it.response.edit_message.await_count == 1, "button did nothing"
        seen.append(checklist.get_counts(42, "daily", 1).get("nightmare", 0))
    assert seen == [1, 2, 0, 1], seen            # 0/2 -> 1/2 -> 2/2 -> clears -> 1/2
    await press(bot, 42, "weekly", "dd")
    assert checklist.get_counts(42, "weekly", 1)["dd"] == 1
    # PvE and PvP command scroll missions count separately, 12 each
    for _ in range(3):
        await press(bot, 42, "weekly", "cmdpve")
    await press(bot, 42, "weekly", "cmdpvp")
    counts = checklist.get_counts(42, "weekly", 1)
    assert counts["cmdpve"] == 3 and counts["cmdpvp"] == 1
    assert checklist.ITEM_MAX["weekly"]["cmdpve"] == 12 and checklist.ITEM_MAX["weekly"]["cmdpvp"] == 12
    labels = [c.item.label for c in checklist.build_view("weekly", counts).children]
    assert "PvE command scroll missions 3/12" in labels and "PvP command scroll missions 1/12" in labels, labels
    assert len(labels) <= 25 and all(len(x) <= 80 for x in labels)
    print("CHECKLIST BUTTONS OK", seen)
asyncio.run(main())
