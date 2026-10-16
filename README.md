# AION 2 Discord Bot

Per-user daily/weekly checklists, group event planner, event timers and private links.

## Adding it to a server
Anyone with the invite link can add the bot. It works straight away, with no setup by the bot's owner:
each server gets its own boss lists, links, manual timers, timer board and notices channel.
Commands are global, so a brand-new command can take up to an hour to show up in Discord.

Invite link: Developer Portal > Bot > turn on **Public Bot**. Then OAuth2 > URL Generator, scopes `bot` and
`applications.commands`, permissions View Channels, Send Messages, Embed Links, Read Message History.

On join the bot posts a short welcome message. Server managers can optionally run `/setup announcements` to pick a
channel for reset and new-timer notices (off until chosen), `/setup region` to pick the game region whose server clock timers and resets follow (NA East until chosen), `/setup news` in a channel to have the official AION 2 news posted there as it appears, and `/setup show` to see the settings.

## Commands
Checklists
- `/checklist-setup` (Manage Server): post the checklist buttons in this channel.
- `/checklist daily|weekly`: open your own checklist.

Event planner
- `/event create`: raid, world boss, arena, open world faction PvP, cross-territory rift or custom event.
- Buttons on the event: Sign up / edit (Yes or Maybe as Tank, Healer or DPS, optional combat power and gear score), Leave, Alert me (private DM), Organizer (auto-sort into sub-parties, clear parties, cancel).
- `/event list`, `/event edit`, `/event cancel`, `/event assign`, `/event autosort`, `/event alert`.
- `/timezone set|show`: the timezone used for the times you type.

Timers (from gamers4.life, refreshed every 15 minutes). Only the Global schedule is accepted (`TIMERS_REGION`). Each Global region runs it on its own server clock, so every Discord server picks its region with `/setup region` (default `TIMERS_DEFAULT_REGION`, NA East). The checklist resets follow the site's Daily Reset and Weekly Reset timers, including any `/timers set` correction.
- `/timers show`, `/timers notify`, `/timers unnotify`, `/timers mine`, `/timers status`.
- Manual fixes, shared with everyone: `/timers set` (moves the site's whole schedule for that timer by the difference, or `only_this_time:True` for one start, or `repeat:` for your own cycle), `/timers killed` (starts a respawn countdown; knows the Verteron and Altgard field boss respawns and remembers any other length), `/timers clear` (back to the site's schedule).
- Boss names, levels and locations for every open-world boss come from [aion2data.com](https://aion2data.com/bosses)'s JSON feed of the Global client files, read once a day (`BOSS_DATA_URL`). The game files hold no respawn times, so those come from kill reports.
- `/timer-setup` (Manage Server): posts a live timer board in the channel, edited every `TIMER_BOARD_MINUTES` (default 2). `stop:True` removes it.
- New rows on the site are tracked automatically and announced in each server's notices channel (if set). New world bosses also join every server's event planner boss list.

DKP and gear (per server)
- Players: `/dkp me`, `/dkp top`, `/dkp history`, `/gear list`, `/gear reserve`, `/gear unreserve`, `/gear queue`, `/gear mine`.
- Officers (Manage Server or the officer role): `/dkp reward` (check-in button), `/dkp award-event`, `/dkp event-button` (Award and Undo buttons for an event), `/dkp award`, `/dkp award-role`, `/dkp take`, `/dkp undo`, `/dkp decay`, `/dkp export`, `/loot add|remove|award|clear`.
- Managers: `/dkp-setup` (live board of everyone's DKP, no limit), `/dkp config` (max reservations, allow negative, officer role), `/dkp season confirm:True`.
- Gear goes to the top reserver by DKP when `/loot award` runs; the winner is charged the item's cost.

Links (only you see the reply)
- `/link site`, `/links`.

Admin (Manage Server)
- `/setup announcements [channel]`, `/setup region`, `/setup server`, `/setup show`: optional notices channel, game region, game server and current settings.
- Field bosses come from the shared `assets/field-bosses.json`; once a day the bot also reads the copy published on the Aion 2 tools Pages site (`FIELD_BOSSES_URL`), validates it and swaps it in, so a field boss added upstream appears in `/timers` without a redeploy (`/setup show` says the source and age; a bad or unreachable copy changes nothing).
- `/setup news [region] [stop]`: posts the official AION 2 news (notices and updates, as on the Aion 2 tools News page) in the channel it was run in, each article as a short lead line plus its official link so Discord shows the link preview. Read from the log server's news feed (`NEWS_URL`) every `NEWS_MINUTES` (15). The region follows `/setup region` (the Global English boards) unless Korea, Taiwan or all regions is chosen; the first run posts only the newest article, later runs at most `NEWS_MAX_PER_CYCLE` (5), and nothing is posted twice. `stop:True` turns it off.
- With a game server picked, field boss kills found in combat logs uploaded to the log server (`BOSS_KILLS_URL`) start respawn timers by themselves.
- `/eventlist add|remove|show`: raid and world boss lists.
- `/linkadmin add|remove`: your own extra links.

## Default event sizes
Raid 10 (global first raid), Arena 4 (4v4), Faction PvP 10, World Boss 10, Rift 5, Custom 5.
Sub-party size defaults to 5 (global party size), 4 for arena. Every event can override both.

## Health check
`http://SERVER_IP:8099/health` returns 200 when the bot is connected to Discord and 503 otherwise.

## Run
    docker compose up -d --build
    docker logs -f aion2-checklist

## Automatic updates
The server can update itself from `main`. A systemd timer checks every 15 minutes (and after a reboot); when `main`
has a new commit whose GitHub Actions run (`CI`) passed, it copies `data/`, pulls, rebuilds the container and waits for
the bot to report healthy.

The stack folder must be a Git checkout of this repository on `main`. Turn it on once:

    sudo bash deploy/enable-updates.sh

- Update right now: `sudo systemctl start aion2bot-update`
- See what it did: `sudo journalctl -u aion2bot-update -n 40 --no-pager`
- Next check: `systemctl list-timers aion2bot-update.timer --no-pager`
- Turn it off: `sudo systemctl disable --now aion2bot-update.timer`

While the repository is private, the server needs two things: Git access for the account that owns the stack folder
(a read-only deploy key works), and a fine-grained GitHub token limited to this repository with **Actions: Read-only**
saved in `/etc/aion2bot-github-token` (owner root, mode 600) so the updater can read the CI result. A public repository
needs neither.

It refuses to run when tracked files were edited on the server or the checkout is not on `main`, and it defers while
CI is pending or failed. A journal line saying "Finished" can also mean deferred: look for `updated ... and restarted`
or `already installed and up to date`. Data copies are kept as `data.bak-auto-*` (newest 5). There is no automatic
rollback: if the bot is not healthy after an update the updater reports it and tries again on the next check.

## Timer region check
`docker compose exec aion2-checklist python inspect_timers.py` shows which region the timers page is serving and whether the bot accepts it.

## Running the tests
From the repo root: `pip install -r requirements.txt` then `for t in tests/test*.py; do python3 $t; done`. See `CLAUDE.md` for project notes.
