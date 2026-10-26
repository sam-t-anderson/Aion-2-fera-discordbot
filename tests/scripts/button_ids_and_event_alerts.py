import os, sys, tempfile, time
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import db, events, checklist, event_panels
db.init(); c = db.conn(); now = int(time.time())
# dynamic item templates
for cls, good, bad in [(event_panels.EvBtn, ["ev:open:12","ev:cancelok:3","ev:cp:9"], ["ev:set:1:yes:tank","ev:open:x"]),
                       (event_panels.EvSet, ["ev:set:12:maybe:healer"], ["ev:open:12"]),
                       (checklist.TickButton, ["cl:tick:daily:duties"], ["cl:open:daily"])]:
    pat = cls.__discord_ui_compiled_template__
    assert all(pat.fullmatch(g) for g in good) and not any(pat.fullmatch(b) for b in bad), cls
# alert query
c.execute("INSERT INTO events (guild_id,channel_id,message_id,organizer_id,kind,name,starts_at,capacity,party_size,alert_minutes,created_at) VALUES (1,2,3,9,'raid','X',?,10,5,15,?)", (now+600, now))
c.execute("INSERT INTO signups (event_id,user_id,status,role,alert,joined_at) VALUES (1,7,'yes','dps',1,?)", (now,)); c.commit()
due = c.execute("""SELECT s.event_id, s.user_id, e.* FROM signups s JOIN events e ON e.id = s.event_id
   WHERE e.status='open' AND s.alert=1 AND s.alerted=0 AND e.starts_at - e.alert_minutes*60 <= ? AND e.starts_at + 300 > ?""", (now, now)).fetchall()
assert len(due) == 1 and events.event_title(due[0]) == "Raid: X" and events.event_url(due[0]).endswith("/1/2/3")
print("ok")
