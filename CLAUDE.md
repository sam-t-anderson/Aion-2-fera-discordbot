# AION 2 Discord bot ("Fera") — project context for Claude

Sam's public, multi-server Discord bot for AION 2. Anyone can invite it and it works with no setup by the owner.
Python 3.12 (Docker), discord.py 2.x (installed unpinned `>=2.4,<3`; dev sandbox had 2.7.1), SQLite.

## Where it runs
- Home server `island` (Ubuntu, 192.168.1.101), user `sam`. Stack dir `/opt/stacks/aion2-checklist`, container `aion2-checklist`.
- Health endpoint: container port 8080, host port 8099 (`curl localhost:8099/health`). Monitored by Uptime Kuma, shown on Homepage.
- Config in `.env` on the server (never in git). Data in `./data/checklist.db` (never in git). Back up `data/` before upgrades.
- `docker-compose.yml` has `init: true` (tini as PID 1) so Docker health-check processes don't pile up as zombies.

## Deploy / upgrade (Sam wants copy-paste commands, not "edit file X")
```bash
cd /opt/stacks/aion2-checklist
sudo cp -a data data.bak-$(date +%F)
sudo tar xzf /home/sam/aion2-bot.tar.gz --strip-components=1 -C /opt/stacks/aion2-checklist   # or git pull
sudo chown -R sam:sam /opt/stacks/aion2-checklist
docker compose up -d --build
docker logs --tail 20 aion2-checklist | grep -v -i "pynacl\|davey"      # expect "Logged in as", "Timers ok"
```
New global slash commands can take up to an hour to show up in Discord. The DB migrates itself on start (`db.init`).

## Layout
- `bot.py` entry; `GuildOnlyTree` rejects DMs and registers the server. Modules load in `setup_hook`.
- `checklist.py` per-user daily/weekly checklists (13:00 UTC reset, weekly Monday). Persistent buttons.
- `events.py` event planner (`/event`, `/eventlist`, `/timezone`), signups with roles.
- `timers.py` scrapes gamers4.life **Global** schedule (region verified from the page heading; times are UTC), `/timers ...`, live `/timer-setup` board, DM notifications, manual overrides.
- `links.py` private links. `guilds.py` welcome message + `/setup announcements|show`.
- `dkp.py` DKP: ledger, `/dkp` `/gear` `/loot`, check-in buttons, event award/undo panels (`/dkp event-button`), live full-list board (`/dkp-setup`).
- `db.py` schema + migrations. `config.py` env settings. `health.py` health endpoint. `inspect_timers.py` diagnostic.
- `docs/PLAYER-GUIDE.md` (+PDF) player docs; `docs/GUIDE-full.md` includes hosting notes.
- `tests/` plain scripts, run each from the repo root: `python3 tests/test6.py` etc. Background `tasks.loop` "Client has not been properly initialised" tracebacks are harmless noise; each test prints an OK line at the end.

## Design rules and gotchas learned the hard way
- **Per-server data**: every table is keyed by `guild_id`; `db.forget_guild` wipes a server on leave. Only scraped timers (`timer_rules`, `timer_events`) are shared.
- **DynamicItem buttons** (`ClaimButton`, `BatchButton`, `TickButton`, ...): never store your own state in `self.item`, `self.view`, `self.row` etc. — DynamicItem owns those names. (This silently broke checklist ticks; regression test `tests/test9.py`.) Encode state in the custom_id regex.
- **Timer overrides** (`guild_timer_manual`): `/timers set` on a timer the site lists stores `shift_sec`, which moves *every* site start by that amount (persists across refresh/restart; only within 12h of the site time). `only_this_time` / `/timers killed` replace just the nearest site start (`replaces_at`). `repeat:` creates its own cycle. The 30-day purge must skip shift rows.
- **Timers region guard**: if the page region isn't `TIMERS_REGION` (default Global) the last good schedule is kept.
- **DKP**: balances = SUM of `dkp_ledger.delta` for the current season where `undone=0`. Event panels use `dkp_batches` (`ready -> awarded -> undone`); undo flips `undone` on rows with that `batch_id`. Gear goes to the highest-DKP reserver (earlier reservation breaks ties), unaffordable players skipped, winner charged. No automatic decay (`/dkp decay` is manual).
- **Live boards** edit messages only when the payload changed; DKP board paginates (<=3800 chars per embed) and reposts everything if a page was deleted. The board only lists players with a ledger entry this season (no members intent).
- Python 3.11 forbids reusing the same quote inside f-string expressions; the server image is 3.12 but keep code 3.11-safe.
- Embed limits: 4096 description, 6000 total per message.

## Sam's preferences
- Give exact copy-paste commands for any file edit or server step.
- Keep player-facing text plain and friendly; admin-only features are labelled "(officers)" / "(admins)".

## Status / ideas not done
- Online "AION 2 Bot Guide" Claude Doc is out of date (missing `/dkp event-button`, `/dkp-setup`, `only_this_time`); PDF/txt/markdown in `docs/` are current.
- Optional: automatic scheduled DKP decay; DKP board could list members with zero DKP only with the privileged members intent.
