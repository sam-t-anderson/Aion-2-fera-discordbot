import asyncio, os, sys, tempfile, json
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import discord
from discord.ext import commands
import db, dkp, dkp_board
G=1
class Msg:
    def __init__(s,ch,i): s.ch,s.id=ch,i
    async def edit(s,embed=None):
        if s.id not in s.ch.msgs: raise discord.NotFound(type("R",(),{"status":404,"reason":"x"})(),"gone")
        s.ch.msgs[s.id]=embed; s.ch.edits+=1
    async def delete(s): s.ch.msgs.pop(s.id,None)
class Ch:
    def __init__(s): s.msgs={}; s.n=100; s.edits=0
    def get_partial_message(s,i): return Msg(s,i)
    async def send(s,embed=None,allowed_mentions=None):
        s.n+=1; s.msgs[s.n]=embed; return Msg(s,s.n)
async def main():
    db.init(); c=db.conn(); ch=Ch()
    bot=commands.Bot(command_prefix="!",intents=discord.Intents.default())
    cog=dkp_board.DkpBoard(bot); 
    async def _c(cid): return ch
    cog._channel=_c
    for i in range(900): dkp.add_entry(G,1000+i,i+1,"award","x",9)
    c.execute("INSERT INTO dkp_boards (guild_id,channel_id,message_ids) VALUES (?,5,'[]')",(G,)); c.commit()
    await cog.refresh(G)
    ids=json.loads(c.execute("SELECT message_ids FROM dkp_boards").fetchone()[0]); print("pages",len(ids))
    assert len(ids)>1 and len(ch.msgs)==len(ids)
    await cog.refresh(G); assert ch.edits==0            # unchanged -> no edits
    dkp.add_entry(G,5000,5000,"award","x",9); await cog.refresh(G); assert ch.edits>=1
    ids=json.loads(c.execute('SELECT message_ids FROM dkp_boards').fetchone()[0])
    del ch.msgs[ids[1]]                                  # delete a middle page
    cog._last.pop(G)                                     # what the delete listener does
    await cog.refresh(G)
    new=json.loads(c.execute("SELECT message_ids FROM dkp_boards").fetchone()[0])
    assert len(ch.msgs)==len(new)==len(ids) and not set(new)&set(ids)
    for i in range(900): dkp.add_entry(G,1000+i,-(i+1),"take","x",9)   # list shrinks
    dkp.add_entry(G,5000,-5000,"take","x",9)
    await cog.refresh(G)
    new2=json.loads(c.execute("SELECT message_ids FROM dkp_boards").fetchone()[0]); print("shrunk",len(new2)); assert len(ch.msgs)==len(new2)
    print("BOARD OK")
asyncio.run(main())
