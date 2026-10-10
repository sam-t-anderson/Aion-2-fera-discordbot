# AION 2 Bot Guide

The bot gives each player a private daily and weekly checklist, plans group events with role signups, tracks boss and event timers, and keeps quick links to AION 2 sites. Type `/` in Discord to find every command below.

## Adding the bot to a server

Anyone with the invite link can add the bot to their server, and it works right away. Nothing needs configuring. Each server has its own event boss lists, links, manual timers, timer board and notices channel, so servers never affect each other.

On joining, the bot posts a short welcome message. Commands are global, so brand-new commands can take up to an hour to appear in Discord. Server managers can optionally run `/setup announcements` to choose a channel for checklist reset and new-timer notices. Those notices are off until a channel is chosen. `/setup region` picks the game region (NA East, NA West, EU, SA or JP) whose server clock the timers and checklist resets follow; it is NA East until a manager chooses. `/setup show` lists the current settings.

## Checklists

Every player ticks their own daily and weekly items, and nobody else sees them.

- Press the **Open** buttons on the checklist message, or type `/checklist` and pick daily or weekly. Only you can see your list.
- Tick items with the buttons. Items with several runs (like 5 duties) count up one press at a time, and there is a button to complete a whole list.
- Progress clears itself at reset. The checklist follows the **Daily Reset** and **Weekly Reset** timers for your server's game region, and shows when the next reset is.
- If the reset time is wrong, fix the Daily Reset or Weekly Reset timer with `/timers set` and the checklist follows it.

Admins post the buttons once with `/checklist-setup` in the channel people should use.

## Event planner

Start with `/event create`. The bot posts an event card that people sign up on with buttons.

| Event type | Default size | Sub-party size |
| --- | --- | --- |
| Raid | 10 | 5 |
| World Boss | 10 | 5 |
| Arena | 4 | 4 |
| Open World Faction PvP | 10 | 5 |
| Cross-Territory Rift | 5 | 5 |
| Custom Event | 5 | 5 |

Raid, world boss and custom events need a `name`. For raids and world bosses, pick from the list as you type. Only `kind` and `when` are required for the other types.

**Options on `/event create`**

- `when`: `8pm`, `tomorrow 7:30pm`, `10/4 8pm`, `2026-10-04 20:00` or `in 2h`. It uses your timezone.
- `size`: total players wanted, between 2 and 60. Leave it empty for the default above.
- `party_size`: players per sub-party, between 2 and 10.
- `description`: notes for the group.
- `alert_minutes`: how early people who chose Alert me get a DM (0 to 120).

**On the event card**

- **Sign up / edit**: choose Yes or Maybe, pick Tank, Healer or DPS, and optionally enter your combat power. People past the size limit go on a waitlist.
- **Leave**: remove yourself.
- **Alert me**: a private DM before the event starts.
- **Organizer**: auto-sort into sub-parties, clear parties or cancel. Organizer controls are for whoever created the event.

**Other commands**

- `/event list`: upcoming events in the server.
- `/event edit`: change time, size, name or notes.
- `/event cancel`: cancel an event you organize.
- `/event assign`: put a player in a sub-party (0 removes them).
- `/event autosort`: split confirmed players into balanced parties. Tanks and healers are spread out first, then DPS by party size and combat power.
- `/event alert`: turn your DM alert on or off for an event.

## Timers

The bot reads the event schedule on gamers4.life (https://gamers4.life/aion-2/database/en/events/) every 15 minutes and keeps it for a week. The bot only accepts the Global version of the schedule. Each Global region (NA East, NA West, EU, SA, JP) runs that schedule on its own server clock, so a server manager picks the region with `/setup region` (NA East until then). The site publishes recurring schedules only and does not know individual boss respawns. Manual timers fill that gap.

**Viewing**

- `/timers show`: the next start of every rift, boss and event, in your own timezone. Add `category` to narrow it.
- `/timer-setup` (Manage Server): posts a live board in the current channel. The bot edits it every 2 minutes and the countdowns tick live between edits. Run it in another channel to move the board, or `/timer-setup stop:True` to remove it.
- `/timers status`: when the last refresh happened and how many timers are tracked.

**Fixing or adding a time (shared with everyone)**

- `/timers set name when`: sets when a timer starts. If the site lists that timer, every start moves by the same amount (up to 12 hours either way) and stays that way until you `/timers clear` it. Add `only_this_time:True` to change just one start, or `repeat:4h` to make it repeat. Without a site schedule it creates a new timer with the `category` you pick.
- `/timers killed name`: a boss just died, so its respawn countdown starts now. The bot knows the respawn of the Verteron and Altgard field bosses (Eternal Gartua, Soul Ruler Kashapa and so on), so pick the name from the list and you are done. Silent Dartan and High Commander Lagta respawn at different speeds in each zone, so pick the one with your zone in its name. For any other boss add `respawn:4h` the first time. The bot remembers the length for next time. The name list covers every open-world boss in the Global client (Verteron, Altgard, Eltnen, Morheim and Reshanta), read once a day from aion2data.com, and the reply shows the boss's level and where it stands.
- `/timers clear name`: removes the manual time and goes back to the site's schedule. Only the person who set it or a server manager can clear it.
- The list shows who set each manual time.

**DM reminders**

- `/timers notify target lead`: get a DM for one timer, or for a whole category, when it starts or 1, 3, 5 or 10 minutes before. Categories that start every hour can send many DMs.
- `/timers unnotify` stops them. `/timers mine` lists yours.
- You need server DMs turned on for the bot to reach you.

**New events**

Anything new on the site is tracked automatically and announced in the server's notices channel, if one is set. New world bosses also join each server's event planner boss list.

## DKP and gear

DKP is a point balance your guild uses to decide who gets gear. Officers give points for events. Players reserve the gear they want. When it drops, the officer awards it and the winner's DKP is charged. Everything is per server and every change is recorded, so nothing is lost or hidden.

**For players**

- `/dkp me`: your balance, rank, recent changes and reserved gear. Only you see it.
- `/dkp top`: the standings. Add `public:True` to show the channel.
- `/dkp history`: your recent DKP changes, each with an entry number.
- `/gear list`: gear that can be reserved, with costs and how many people reserved each.
- `/gear reserve item`: reserve a piece of gear. You can hold 3 reservations at once by default.
- `/gear unreserve item`: give one up.
- `/gear queue item`: who reserved it, in the order it would go.
- `/gear mine`: your reservations and your place in each line.

Reserving does not spend DKP. Points are only charged when you win.

**How gear is awarded**

When an item drops, an officer runs `/loot award item`. The player with the most DKP among those who reserved it wins, and an earlier reservation breaks a tie. Anyone who can't afford the cost is skipped. The winner is charged the item's cost and everyone's reservations for that item are cleared. An officer can also name a player directly.

**For officers**

Officers are server managers, plus anyone with the officer role set in `/dkp config`.

| Command | What it does |
| --- | --- |
| `/dkp reward name points minutes` | Posts a Check in button. Everyone who presses it in time gets the points once. Closes after 30 minutes by default. |
| `/dkp award-event event points` | Gives everyone confirmed on a planned event the points. Add `include_maybe:True` for Maybe signups. It refuses to pay the same event twice. |
| `/dkp event-button event points` | Posts a panel for a planned event with **Award N DKP**, **Undo** and **Change amount** buttons. Award pays everyone confirmed (add `include_maybe:True` for Maybe signups) once. Undo reverses exactly what that panel paid, and you can change the amount and award again. Only officers can press them. |
| `/dkp award member points reason` | Gives one player points. |
| `/dkp award-role role points reason` | Gives everyone with a role points. |
| `/dkp take member points reason` | Removes points as a penalty or correction. |
| `/dkp undo entry` | Reverses one entry by its number from `/dkp history`. |
| `/dkp decay percent` | Takes a percentage off every positive balance. |
| `/dkp export` | Downloads standings as a CSV file. |
| `/dkp history member` | Shows any player's history. |
| `/dkp-setup` | Managers only. Posts a live board of everyone's DKP in the channel, over several messages when the list is long, with no limit. It updates a few seconds after any change. Run it elsewhere to move it, or `/dkp-setup stop:True` to remove it. Only players who have had DKP this season are listed. |
| `/loot add item cost slot source` | Adds gear to the list, or changes its cost. |
| `/loot remove item` | Removes gear and its reservations. |
| `/loot award item member` | Awards gear to the top reserver, or to the player you pick. |
| `/loot clear item` | Clears all reservations for an item. |

**Managers only**

- `/dkp config`: max reservations per player, whether players may win gear they can't afford (off by default), and the officer role.
- `/dkp season confirm:True`: starts a new season. Everyone goes back to 0 and reservations clear. Old history is kept.

## Links and timezone

Link replies are private. Only you see them.

| Command | Opens |
| --- | --- |
| `/link map` | Interactive map: https://interactivemap.app/aion2/maps/verteron |
| `/link db` | Database: https://gamers4.life/aion-2/database/en/ |
| `/link leaderboard` | Leaderboard: https://gamers4.life/aion-2/database/en/leaderboard/ |
| `/link daevanion` | Daevanion node planner: https://gamers4.life/aion-2/database/en/daevanion-planner/ |
| `/link build` | Build planner: https://gamers4.life/aion-2/database/en/build-calculator/ |
| `/link news` | News: https://aion2.online/news/ |
| `/link twitter` | AION 2 on X: https://x.com/AION2Official?lang=en |
| `/link abysslogs` | Abyss Logs: https://abysslogs.com/ |

`/links` lists every saved link, including ones admins added.

**Your timezone**

Times you type use your timezone, which starts as America/Denver. Change it with `/timezone set` (for example `America/New_York`) and check it with `/timezone show`. Times the bot shows you always appear in your local time.

## Admin setup

These commands need the **Manage Server** permission. Their replies are private to you, except the boards and buttons they post.

| Command | What it does |
| --- | --- |
| `/checklist-setup` | Posts the checklist buttons in this channel |
| `/timer-setup` | Posts the live timer board here (`stop:True` removes it) |
| `/setup announcements` | Picks the channel for reset and new-timer notices (leave empty to turn off) |
| `/setup region` | Picks the game region whose server clock the timers and checklist resets follow |
| `/setup show` | Shows this server's settings |
| `/eventlist add` | Adds a raid or world boss name to the event planner list |
| `/eventlist remove` | Removes one from the list |
| `/eventlist show` | Shows both lists |
| `/linkadmin add` | Adds or updates a link for `/link` and `/links` (name, url, optional label) |
| `/linkadmin remove` | Removes a custom link |
| `/timers clear` | Clears anyone's manual timer |

The built-in raids are Central Abyss Fusion Reactor, Isolation Specialist Rahu, Abyssal Wings Ketu and Eternal Ludra. The built-in world bosses are every open-world boss in the Global client's files (read once a day from aion2data.com): the Verteron and Altgard field bosses (Eternal Gartua, Immortal Gartua, High Commander Lagta, Soul Ruler Kashapa, Silent Dartan and the rest), the level 80 Eltnen and Morheim bosses, and the Reshanta bosses.
