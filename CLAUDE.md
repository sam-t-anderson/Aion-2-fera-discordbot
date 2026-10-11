# AION 2 Discord bot ("Fera") — project context for Claude

Sam's public, multi-server Discord bot for AION 2. Anyone can invite it and it works with no setup by the owner.
Python 3.12 (Docker), discord.py 2.x (installed unpinned `>=2.4,<3`; dev sandbox had 2.7.1), SQLite.

## Where it runs
- Home server `island` (Ubuntu, 192.168.1.101), user `sam`. Stack dir `/opt/stacks/aion2-checklist`, container `aion2-checklist`.
- Health endpoint: container port 8080, host port 8099 (`curl localhost:8099/health`). Monitored by Uptime Kuma, shown on Homepage.
- Config in `.env` on the server (never in git). Data in `./data/checklist.db` (never in git). Back up `data/` before upgrades.
- `docker-compose.yml` has `init: true` (tini as PID 1) so Docker health-check processes don't pile up as zombies.

## Deploy / upgrade (Sam wants copy-paste commands, not "edit file X")
Normally automatic: merging to `main` is the deploy. `deploy/aion2bot-update.sh` runs from a systemd timer every 15 min (`deploy/enable-updates.sh` installs it), and only installs a `main` commit whose GitHub Actions workflow named `CI` passed, so keep the tests green and don't rename that workflow. It copies `data/` to `data.bak-auto-*`, fast-forwards, runs `docker compose up -d --build` and waits for Docker's health check. Force a check with `sudo systemctl start aion2bot-update`; read the result with `sudo journalctl -u aion2bot-update -n 40 --no-pager`. The stack dir must stay a clean `main` checkout (no edits to tracked files on the server).

By hand:
```bash
cd /opt/stacks/aion2-checklist
sudo cp -a data data.bak-$(date +%F)
git pull --ff-only origin main
docker compose up -d --build
docker logs --tail 20 aion2-checklist | grep -v -i "pynacl\|davey"      # expect "Logged in as", "Timers ok"
```
New global slash commands can take up to an hour to show up in Discord. The DB migrates itself on start (`db.init`).

## Layout
- `bot.py` entry; `GuildOnlyTree` rejects DMs and registers the server. Modules load in `setup_hook`.
- `checklist.py` per-user daily/weekly checklists. Resets follow the server's "Daily Reset" / "Weekly Reset" timers (overrides included); `RESET_UTC_HOUR` / `WEEKLY_RESET_WEEKDAY` are only the fallback. Persistent buttons.
- `events.py` event planner (`/event`, `/eventlist`, `/timezone`), signups with roles.
- `timers.py` scrapes gamers4.life **Global** schedule (Taiwan/Korea pages refused), `/timers ...`, live `/timer-setup` board, DM notifications, manual overrides.
- `bosscatalog.py` reads aion2data.com's `raw/monsters.json` (Global client files; free to use with a link back) once a day into `boss_catalog`: every open-world boss with level, zone and area. Names feed `/timers` autocomplete and each server's planner boss list. It has no respawn times.
- `gameservers.py` official game server list per region (daily, `game_servers` table) for `/setup server`, and the kill feed: polls the log server's `/api/v1/boss-kills?server=ID` (Sam's `Aion-2-tools-server`) every minute and starts respawn timers as manual rows with `set_by=0` ("from combat logs"). Respawn used = shortest of the feed's measured value, `gamedata` and the guild's remembered one. A local `/timers killed` newer than the kill wins. Zone-split bosses use the server faction's home zone.
- `gamedata.py` regions (key, labels, timezone) and the Verteron/Altgard field bosses with respawn minutes, spelled as in the Global client.
- `links.py` private links. `guilds.py` welcome message + `/setup announcements|region|show`.
- `dkp.py` DKP: ledger, `/dkp` `/gear` `/loot`, check-in buttons, event award/undo panels (`/dkp event-button`), live full-list board (`/dkp-setup`).
- `db.py` schema + migrations. `config.py` env settings. `health.py` health endpoint. `inspect_timers.py` diagnostic.
- `docs/PLAYER-GUIDE.md` (+PDF) player docs; `docs/GUIDE-full.md` includes hosting notes.
- `deploy/` self-updater (script, systemd service + timer, CI check). `.github/workflows/ci.yml` runs every test script.
- `tests/` plain scripts, run each from the repo root: `python3 tests/test6.py` etc. Background `tasks.loop` "Client has not been properly initialised" tracebacks are harmless noise; each test prints an OK line at the end.

## Design rules and gotchas learned the hard way
- **Per-server data**: every table is keyed by `guild_id`; `db.forget_guild` wipes a server on leave. Only scraped timers (`timer_rules`, `timer_events`) are shared.
- **DynamicItem buttons** (`ClaimButton`, `BatchButton`, `TickButton`, ...): never store your own state in `self.item`, `self.view`, `self.row` etc. — DynamicItem owns those names. (This silently broke checklist ticks; regression test `tests/test9.py`.) Encode state in the custom_id regex.
- **Timer overrides** (`guild_timer_manual`): `/timers set` on a timer the site lists stores `shift_sec`, which moves *every* site start by that amount (persists across refresh/restart; only within 12h of the site time). `only_this_time` / `/timers killed` replace just the nearest site start (`replaces_at`). `repeat:` creates its own cycle. The 30-day purge must skip shift rows.
- **Timer regions**: the site's clock times are each Global region's own local server time (the page it serves is always NA East, UTC-4). Nothing is pre-expanded: `events_between` works out start times from `timer_rules` with the server's timezone (`guild_settings.timer_region`, set by `/setup region`, default `TIMERS_DEFAULT_REGION` = `na`), so daylight saving is followed. If the page shows Taiwan or Korea the last good schedule is kept.
- **Rule formats** the site uses: `every 3h from 02:00`, `every hour at :30`, `Mon, Thu, Sat at 21:30`, `daily at 11:00, 19:00`. An unparsed timing shows as "no upcoming time found" and overrides on it only last once, so add new formats to `parse_rule`.
- **Checklist progress**: `progress.period` holds the time of the last tick; a row counts while that is after the server's last reset.
- **Field bosses** aren't on the site. `/timers killed` takes their respawn from `gamedata.FIELD_BOSSES`; Silent Dartan and High Commander Lagta differ by zone, so their timers are named with the zone.
- **DKP**: balances = SUM of `dkp_ledger.delta` for the current season where `undone=0`. Event panels use `dkp_batches` (`ready -> awarded -> undone`); undo flips `undone` on rows with that `batch_id`. Gear goes to the highest-DKP reserver (earlier reservation breaks ties), unaffordable players skipped, winner charged. No automatic decay (`/dkp decay` is manual).
- **Live boards** edit messages only when the payload changed; DKP board paginates (<=3800 chars per embed) and reposts everything if a page was deleted. The board only lists players with a ledger entry this season (no members intent).
- Python 3.11 forbids reusing the same quote inside f-string expressions; the server image is 3.12 but keep code 3.11-safe.
- Embed limits: 4096 description, 6000 total per message.

## Sam's preferences
- Give exact copy-paste commands for any file edit or server step.
- Keep player-facing text plain and friendly; admin-only features are labelled "(officers)" / "(admins)".

## Status / ideas not done
- Online "AION 2 Bot Guide" Claude Doc (https://claude.ai/artifact/3rdnTD3pcU6XUKxHfP1HPG) mirrors `docs/GUIDE-full.md`, last synced 2026-10-10; update both together. The markdown in `docs/` is current. `docs/AION2-Bot-Guide.pdf` is printed from `docs/PLAYER-GUIDE.md` (see `docs/make_pdf.py`); regenerate it when the guide changes.
- Field boss kills only reach the feed when someone uploads a public log, so timers are as live as uploads are. Japan's official server-list code is a guess (`as`); check `/setup server` shows the right servers for a JP guild.
- Optional: automatic scheduled DKP decay; DKP board could list members with zero DKP only with the privileged members intent.
