import asyncio, os, sys, tempfile, time
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import discord
from discord.ext import commands
import db, dkp, events

G, H = 1, 2
async def main():
    db.init(); c = db.conn()
    assert dkp.balance(G, 10) == 0 and dkp.standings(G) == [] and dkp.rank_of(G, 10) is None
    dkp.add_entry(G, 10, 50, "award", "Raid", 99); dkp.add_entry(G, 11, 80, "award", "Raid", 99)
    dkp.add_entry(G, 12, 80, "award", "Raid", 99); dkp.add_entry(H, 10, 999, "award", "Other server", 99)
    assert dkp.standings(G) == [(11, 80), (12, 80), (10, 50)]       # tie: earlier earner first
    assert dkp.rank_of(G, 10) == 3 and dkp.balance(H, 10) == 999 and dkp.balance(G, 10) == 50
    # loot table + reservations
    c.execute("INSERT INTO loot_items VALUES (?, 'Ludra Blade', 60, 'Weapon', 'Eternal Ludra')", (G,))
    c.execute("INSERT INTO loot_items VALUES (?, 'Rahu Ring', 20, NULL, NULL)", (G,))
    for uid, t in [(10, 1), (11, 2), (12, 3)]:
        c.execute("INSERT INTO loot_reservations VALUES (?,?,?,?)", (G, uid, "Ludra Blade", t))
    c.commit()
    assert [u for u, _, _ in dkp.reservers(G, "ludra blade")] == [11, 12, 10]
    w, skipped = dkp.pick_winner(G, "Ludra Blade"); assert (w, skipped) == (11, [])
    e = dkp.award_loot(G, "Ludra Blade", w, 99); assert e and dkp.balance(G, 11) == 20
    assert not c.execute("SELECT 1 FROM loot_reservations WHERE item='Ludra Blade'").fetchone()
    assert dkp.totals(G, 11) == (80, 60)
    # can't afford: skipped to the next, nobody left -> None
    for uid, t in [(10, 1), (11, 2), (12, 3)]:
        c.execute("INSERT INTO loot_reservations VALUES (?,?,?,?)", (G, uid, "Ludra Blade", t))
    c.commit()
    w, skipped = dkp.pick_winner(G, "Ludra Blade"); assert w == 12 and skipped == []     # 12 has 80
    dkp.award_loot(G, "Ludra Blade", 12, 99)
    assert dkp.balance(G, 12) == 20
    c.execute("INSERT INTO loot_reservations VALUES (?,?,?,?)", (G, 10, "Ludra Blade", 5)); c.commit()
    w, skipped = dkp.pick_winner(G, "Ludra Blade"); assert w is None and skipped == [10]  # 10 has 50 < 60
    assert dkp.award_loot(G, "Ludra Blade", 10, 99) is None and dkp.balance(G, 10) == 50
    c.execute("UPDATE dkp_config SET allow_negative=1 WHERE guild_id=?", (G,)); c.commit()
    assert dkp.pick_winner(G, "Ludra Blade")[0] == 10 and dkp.award_loot(G, "Ludra Blade", 10, 99) and dkp.balance(G, 10) == -10
    c.execute("UPDATE dkp_config SET allow_negative=0 WHERE guild_id=?", (G,)); c.commit()
    # undo restores the points, once
    assert dkp.undo_entry(G, e)["delta"] == -60 and dkp.balance(G, 11) == 80 and dkp.undo_entry(G, e) is None
    assert dkp.undo_entry(H, e) is None                                 # not another server's entry
    # decay
    n = dkp.decay_all(G, 10, 99); assert n == 2 and dkp.balance(G, 11) == 72 and dkp.balance(G, 10) == -10
    # reward check-in
    now = int(time.time())
    c.execute("INSERT INTO dkp_rewards (guild_id, name, points, closes_at, created_by) VALUES (?,?,?,?,?)", (G, "Boss clear", 15, now + 600, 99)); c.commit()
    before = dkp.balance(G, 20)
    assert dkp.claim_reward(1, 20)[0] == "ok" and dkp.balance(G, 20) == before + 15
    assert dkp.claim_reward(1, 20)[0] == "already" and dkp.balance(G, 20) == before + 15
    c.execute("UPDATE dkp_rewards SET closes_at=? WHERE id=1", (now - 1,)); c.commit()
    assert dkp.claim_reward(1, 21)[0] == "closed" and dkp.claim_reward(77, 21)[0] == "missing"
    assert dkp.ClaimButton.__discord_ui_compiled_template__.fullmatch("dkp:claim:12")
    # event award uses confirmed signups
    c.execute("INSERT INTO events (guild_id,channel_id,message_id,organizer_id,kind,name,starts_at,capacity,party_size,created_at) VALUES (?,2,3,9,'raid','Eternal Ludra',?,2,5,?)", (G, now, now)); c.commit()
    for uid, st in [(30, "yes"), (31, "yes"), (32, "yes"), (33, "maybe")]: events.upsert_signup(1, uid, st, "dps")
    conf, wait, maybe = events.split_signups(events.get_event(1), events.get_signups(1))
    assert [s["user_id"] for s in conf] == [30, 31] and [s["user_id"] for s in maybe] == [33]
    # ---- event award panel: award / undo / change amount / re-award / paid-elsewhere
    base = dkp.balance(G, 30)
    c.execute("INSERT INTO dkp_batches (guild_id,event_id,points,include_maybe,created_by) VALUES (?,1,15,0,99)", (G,)); c.commit()
    assert dkp.award_batch(1, 99) == ("ok", 2)
    assert dkp.balance(G, 30) == base + 15 and dkp.balance(G, 33) == 0
    assert dkp.award_batch(1, 99)[0] == "done" and dkp.balance(G, 30) == base + 15      # no double pay
    assert dkp.undo_batch(1, 99) == ("ok", 2) and dkp.balance(G, 30) == base
    assert dkp.undo_batch(1, 99)[0] == "notpaid"
    c.execute("UPDATE dkp_batches SET points=25, include_maybe=1 WHERE id=1"); c.commit()
    assert dkp.award_batch(1, 99) == ("ok", 3) and dkp.balance(G, 33) == 25 and dkp.balance(G, 30) == base + 25
    assert dkp.undo_batch(1, 99) == ("ok", 3) and dkp.balance(G, 33) == 0
    c.execute("INSERT INTO dkp_batches (guild_id,event_id,points,include_maybe,created_by) VALUES (?,1,5,0,99)", (G,)); c.commit()
    dkp.add_entry(G, 30, 5, "event", "Eternal Ludra (event #1)", 99)
    assert dkp.award_batch(2, 99)[0] == "paid"
    assert dkp.award_batch(999, 99)[0] == "missing" and dkp.undo_batch(999, 99)[0] == "missing"
    tpl = dkp.BatchButton.__discord_ui_compiled_template__
    assert tpl.fullmatch("dkp:batch:12:award") and not tpl.fullmatch("dkp:batch:12:nuke")
    b = c.execute("SELECT * FROM dkp_batches WHERE id=1").fetchone()
    assert dkp.batch_embed(b).to_dict() and len(dkp.batch_view(b).children) == 3
    # ---- live board: unlimited list paginates, every player kept, every embed under the limit
    rows = [(1000 + i, 100000 - i) for i in range(2000)]
    pages = dkp.build_pages(rows)
    assert len(pages) > 1 and all(len(p) <= 4096 for p in pages)
    assert sum(p.count("\n") + 1 for p in pages) == 2000
    for i in (0, 1999): assert any(f"<@{rows[i][0]}>" in p for p in pages)
    assert dkp.build_pages([]) == []
    assert all(e.to_dict() for e in dkp.board_embeds(G))
    class FakeBot:
        def get_cog(self, n): return None
    dkp.touch(FakeBot(), G)
    # season reset keeps history, zeroes balances, clears reservations
    c.execute("INSERT INTO loot_reservations VALUES (?,?,?,?)", (G, 10, "Rahu Ring", 1)); c.commit()
    assert dkp.new_season(G) == 2 and dkp.standings(G) == [] and dkp.balance(G, 11) == 0
    assert c.execute("SELECT COUNT(*) FROM dkp_ledger WHERE guild_id=?", (G,)).fetchone()[0] > 5
    assert not c.execute("SELECT 1 FROM loot_reservations WHERE guild_id=?", (G,)).fetchone()
    assert dkp.balance(H, 10) == 999
    # leaving a server wipes its DKP data
    db.forget_guild(G)
    assert not c.execute("SELECT 1 FROM dkp_ledger WHERE guild_id=?", (G,)).fetchone() and dkp.balance(H, 10) == 999
    assert dkp.sign(5) == "+5" and dkp.sign(-5) == "-5"

    # commands register with valid payloads
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
    await dkp.setup(bot)
    names = sorted(x.name for x in bot.tree.get_commands()); print(names)
    assert names == ["dkp", "dkp-setup", "gear", "loot"], names
    for g in ("dkp", "gear", "loot"): print(g, sorted(o.name for o in bot.tree.get_command(g).commands))
    assert "event-button" in [o.name for o in bot.tree.get_command("dkp").commands]
    for cmd in bot.tree.get_commands():
        p = cmd.to_dict(bot.tree)
        def walk(o):
            assert 1 <= len(o["name"]) <= 32 and len(o["description"]) <= 100, o["name"]
            for s in o.get("options", []): walk(s)
        walk(p)
    for cog in list(bot.cogs.values()): await bot.remove_cog(cog.qualified_name)
    print("DKP OK")
asyncio.run(main())
