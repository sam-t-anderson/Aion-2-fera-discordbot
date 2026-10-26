# AION 2 Discord bot ("Fera") — project context for Claude

Sam's public, multi-server Discord bot for AION 2. Anyone can invite it and it works with no setup by the owner.
Python 3.12 (Docker), discord.py 2.x, SQLite. Dependencies are pinned with hashes in `requirements.txt`, compiled from `requirements.in` with pip-compile (command in its header); Dependabot (`.github/dependabot.yml`) proposes updates weekly as PRs, and CI must pass on them like any change. Test tools: `requirements-dev.txt`.

## Where it runs
- Home server `island` (Ubuntu, 192.168.1.101), user `sam`. Stack dir `/opt/stacks/aion2-checklist`, container `aion2-checklist`.
- Health endpoint: container port 8080, host port 8099 (`curl localhost:8099/health`). Monitored by Uptime Kuma, shown on Homepage.
- Config in `.env` on the server (never in git). Data in `./data/checklist.db` (never in git). Back up `data/` before upgrades. The database runs in WAL mode, so it is `checklist.db` plus `checklist.db-wal` and `-shm`: copy the whole folder, never the `.db` file alone.
- The container starts as root, gives `/data` to the image's `bot` user (uid 1000, `BOT_USER`) and drops root before it connects (`privileges.py`), so on the host `data/` belongs to uid 1000. If the folder can't be handed over it logs a warning and runs as root as before. No `USER` line in the Dockerfile on purpose: a host-owned `data/` would leave the bot unable to write after an upgrade.
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
- `events.py` event planner (`/event`, `/eventlist`, `/timezone`), signups with roles and classes. The sign-up panel's class buttons (`EvClass`, `ev:cls:<id>:<class>`) set `signups.char_class` and remember the pick in `member_classes` (guild_id, user_id) so the member's next signup in that server starts with it; pressing the picked class clears it. The Organizer panel's **Edit time / details** (`ev:edit:<id>`) opens `EditModal` (time in the organizer's timezone, name, size, notes); it and `/event edit` both go through `apply_edit`, and a new time re-arms alerts and is announced in the channel (`time_notice`). Test `tests/scripts/event_classes_and_edit.py`.
- `timers.py` scrapes gamers4.life **Global** schedule (Taiwan/Korea pages refused), `/timers ...`, live `/timer-setup` board, DM notifications, manual overrides.
- `bosscatalog.py` reads aion2data.com's `raw/monsters.json` (Global client files; free to use with a link back) once a day into `boss_catalog`: every open-world boss with level, zone and area. Names feed `/timers` autocomplete and each server's planner boss list. It has no respawn times.
- `gameservers.py` official game server list per region (daily, `game_servers` table) for `/setup server`, and the kill feed: polls the log server's `/api/v1/boss-kills?server=ID` (Sam's `Aion-2-tools-server`) every minute and starts respawn timers as manual rows with `set_by=0` ("from combat logs"). Respawn used = shortest of the feed's measured value, `gamedata` and the guild's remembered one. A local `/timers killed` newer than the kill wins. Zone-split bosses use the server faction's home zone.
- `gamedata.py` regions (key, labels, timezone) and the field bosses (name, zone, faction, respawn/Abyss schedule). Field bosses load from `assets/field-bosses.json` — the same community reference the Aion 2 tools client/Pages ship (`aion2calc/app/static/field-bosses.json`), vendored here; re-sync by copying that file over `assets/field-bosses.json`. A built-in list in `gamedata.py` is the fallback if the JSON can't be read. Spelled as in the Global client. `fieldbosses.py` also reads the copy the Pages site publishes (`FIELD_BOSSES_URL`) once a day, validates it with `gamedata.parse`, caches it as `field-bosses.live.json` next to the DB, and swaps the tables in place (`gamedata.apply` + `timers.FIELD_RESPAWNS/FIELD_FACTIONS`), so new field bosses appear without a redeploy; a bad or unreachable copy keeps whatever is loaded. `/setup show` says the source and age. Test `tests/scripts/field_bosses_daily_refresh.py`.
- `links.py` private links. `guilds.py` welcome message + `/setup announcements|region|server|faction|news|show`.
- `news.py` `/setup news`: polls the log server's `/api/v1/news?region=global|kr|tw` (the feed behind the Aion 2 tools News page) every `NEWS_MINUTES` and posts each unseen article to the server's `news_channel` as `**Board** · Title` + the official URL on its own line (Discord unfurls it; needs Embed Links). `news_region` NULL follows `/setup region` (always the Global boards), else `global|kr|tw|all`. `news_posted` (guild_id, article_id) is the per-server dedupe memory; first run posts only the newest article; at most `NEWS_MAX_PER_CYCLE` per cycle, oldest first. One feed read per region per cycle, shared across servers. Test `tests/scripts/news_posting.py`.
- `dkp.py` DKP: ledger, `/dkp` `/gear` `/loot`, check-in buttons, event award/undo panels (`/dkp event-button`), live full-list board (`/dkp-setup`).
- `db.py` schema + migrations; `SCHEMA_VERSION` is stored as `PRAGMA user_version` (raise it with each table change; a database from a newer bot logs a warning and is left as it is). `config.py` env settings. `inspect_timers.py` diagnostic.
- `health.py` health endpoint: 200 while connected to Discord, else 503 (Docker and the updater go by that). The JSON also has `loops` / `stale_loops` (`heartbeat.py`: each background loop's last good round; stale after three missed rounds and at least 5 min) and `sources_last_ok_age_seconds` (the `*_last_ok` meta stamps of the timers site, kill feed, news, boss list, field bosses and server list). A stale loop does not change the status code.
- `errors.py`: what players see when something fails. `CommandTree.on_error` answers slash commands; `@errors.guarded` on every button `callback` and modal `on_submit` answers those (discord.py only logs a failing DynamicItem). Player mistakes (permissions, cooldowns, Discord refusing) get a plain reply; anything else is logged with guild, channel and user and the player gets a short "try again".
- `docs/PLAYER-GUIDE.md` (+PDF) player docs; `docs/GUIDE-full.md` includes hosting notes.
- `deploy/` self-updater (script, systemd service + timer, CI check). `.github/workflows/ci.yml` runs every test script.
- `tests/`: run everything from the repo root with `python -m pytest` (after `pip install -r requirements.txt -r requirements-dev.txt`). `tests/test_*.py` are unit tests; `tests/scripts/*.py` are feature scripts named after what they check, which `tests/test_scripts.py` runs one per process (they set `DB_PATH` before importing the bot). Run one by hand: `python tests/scripts/news_posting.py`. Background `tasks.loop` "Client has not been properly initialised" tracebacks in a script's output are harmless noise; each script prints an OK line at the end. CI also runs pyflakes and builds the image (`Docker image` job: starts as root, hands over a root-owned data folder, checks uid 1000 and WAL).

## Design rules and gotchas learned the hard way
- **Per-server data**: every table is keyed by `guild_id`; `db.forget_guild` wipes a server on leave. Only scraped timers (`timer_rules`, `timer_events`) are shared.
- **DynamicItem buttons** (`ClaimButton`, `BatchButton`, `TickButton`, ...): never store your own state in `self.item`, `self.view`, `self.row` etc. — DynamicItem owns those names. (This silently broke checklist ticks; regression test `tests/scripts/checklist_buttons.py`.) Encode state in the custom_id regex.
- **New buttons, forms and loops**: put `@errors.guarded` on a new button `callback` or modal `on_submit`, and end a new `@tasks.loop` round with `heartbeat.beat("<module>.<loop>", self.<loop>)` inside its `try`. `tests/test_errors.py` and `tests/test_heartbeat.py` fail when one is missing.
- **Timer overrides** (`guild_timer_manual`): `/timers set` on a timer the site lists stores `shift_sec`, which moves *every* site start by that amount (persists across refresh/restart; only within 12h of the site time). `only_this_time` / `/timers killed` replace just the nearest site start (`replaces_at`). `repeat:` creates its own cycle. The 30-day purge must skip shift rows.
- **Timer regions**: the site's clock times are each Global region's own local server time (the page it serves is always NA East, UTC-4). Nothing is pre-expanded: `events_between` works out start times from `timer_rules` with the server's timezone (`guild_settings.timer_region`, set by `/setup region`, default `TIMERS_DEFAULT_REGION` = `na`), so daylight saving is followed. If the page shows Taiwan or Korea the last good schedule is kept.
- **Rule formats** the site uses: `every 3h from 02:00`, `every hour at :30`, `Mon, Thu, Sat at 21:30`, `daily at 11:00, 19:00`. An unparsed timing shows as "no upcoming time found" and overrides on it only last once, so add new formats to `parse_rule`.
- **Checklist progress**: `progress.period` holds the time of the last tick; a row counts while that is after the server's last reset.
- **Field bosses** aren't on the site. `/timers killed` takes their respawn from `gamedata.FIELD_BOSSES` (loaded from `assets/field-bosses.json`, built-in fallback); Silent Dartan and High Commander Lagta differ by zone, so their timers are named with the zone.
- **DKP**: balances = SUM of `dkp_ledger.delta` for the current season where `undone=0`. Event panels use `dkp_batches` (`ready -> awarded -> undone`); undo flips `undone` on rows with that `batch_id`. Gear goes to the highest-DKP reserver (earlier reservation breaks ties), unaffordable players skipped, winner charged. No automatic decay (`/dkp decay` is manual).
- **Live boards** edit messages only when the payload changed; DKP board paginates (<=3800 chars per embed) and reposts everything if a page was deleted. The board only lists players with a ledger entry this season (no members intent).
- Python 3.11 forbids reusing the same quote inside f-string expressions; the server image is 3.12 but keep code 3.11-safe.
- Embed limits: 4096 description, 6000 total per message.

## Sam's preferences
- The PDF guide in the repo must always be the newest. After any change to `docs/PLAYER-GUIDE.md`, run `python docs/make_pdf.py` (needs `pip install markdown` and Chrome or Edge) and commit `docs/AION2-Bot-Guide.pdf` with its `.source-sha256` stamp in the same change. `tests/scripts/player_guide_pdf.py` fails in CI while they don't match, which blocks the deploy.
- Give exact copy-paste commands for any file edit or server step.
- Keep player-facing text plain and friendly; admin-only features are labelled "(officers)" / "(admins)".

## Status / ideas not done
- Online "AION 2 Bot Guide" Claude Doc (https://claude.ai/artifact/3rdnTD3pcU6XUKxHfP1HPG) mirrors `docs/GUIDE-full.md`, last synced 2026-10-10; update both together. The markdown in `docs/` is current. 
- Field boss kills only reach the feed when someone uploads a public log, so timers are as live as uploads are. Japan's official server-list code is a guess (`as`); check `/setup server` shows the right servers for a JP guild.
- Optional: automatic scheduled DKP decay; DKP board could list members with zero DKP only with the privileged members intent.
