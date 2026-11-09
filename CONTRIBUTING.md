# Contributing to Fera

Thanks for your interest in improving Fera, the AION 2 Discord bot. In any Discord server that adds it, Fera gives
each player a private daily and weekly checklist, plans group events with role sign-ups, tracks boss and event
timers, keeps DKP and gear reservations, and posts the official AION 2 news. Contributions of all kinds are
welcome: bug reports, fixes, features, docs and translations.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Please report a security
problem privately, as [SECURITY.md](SECURITY.md) describes, not in an issue.

## What matters most

- **Merging to `main` is the deploy.** The hosted bot installs every `main` commit whose CI passed within about
  15 minutes, so `main` must always be ready to run. CI has to pass on every pull request, and the workflow must
  stay named `CI`: the updater looks for it by that name.
- **Each server's data is its own.** Server data is keyed by `guild_id`. Only the game data read from outside
  (the event schedule and the boss and server lists) is shared between servers. A new per-server table needs a
  `guild_id` column and a line in `db.forget_guild`, which wipes a server's data when the bot leaves it. No change
  may let one server see or change another's data.
- **No made-up game data.** Times and respawns come from the event schedule site, the official patch notes, the
  shared field-boss reference or kill reports. Don't add a time or respawn you can't point to a source for.
  - The field bosses live in `assets/field-bosses.json`, the community reference the
    [Aion 2 tools](https://github.com/sam-t-anderson/Aion-2-tools) client also ships
    (`aion2calc/app/static/field-bosses.json`). Change it there, then copy it over.
  - An official schedule change the site doesn't show yet goes in `gamedata.SCHEDULE_CHANGES`, and an official
    timer the site doesn't list in `gamedata.SCHEDULE_ADDITIONS`. Write each as the site would, in server clock
    time.

## Ways to contribute

- **Report a bug** in an issue. Say which command or button you used, what you expected and what the bot
  replied, your server's game region (`/setup show` lists the settings), and when it happened, with your time
  zone.
- **Review a translation.** The Korean and Traditional Chinese command descriptions in `assets/locales/` are
  drafts awaiting native review, so corrections from native speakers are especially welcome.
- **Improve the guide** in `docs/`.
- **Contribute code**, with the setup and checks below.

## Development setup

The bot runs on Python 3.12 in Docker. Keep the code Python 3.11-safe: 3.11 forbids reusing the same quote
inside an f-string expression.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements.txt
pip install -r requirements-dev.txt
```

The tests need no Discord token. To try the bot in Discord, create your own application and bot in the Discord
Developer Portal, invite it to a test server, and run it with Docker:

```bash
cp .env.example .env            # then set DISCORD_TOKEN in .env
docker compose up -d --build
docker logs -f aion2-checklist
```

Discord can take up to an hour to show a new global slash command. The [README](README.md) describes the
settings, the health check and the database.

## Before you open a pull request

Run what CI runs:

```bash
python -m compileall -q .
python -m pyflakes *.py deploy/*.py docs/*.py tests
python -m pytest -q
```

- `tests/test_*.py` are unit tests.
- `tests/scripts/*.py` are feature scripts named after what they check. `tests/test_scripts.py` runs each in its
  own process; run one by hand with, for example, `python tests/scripts/news_posting.py`.
- CI also builds the Docker image and checks that it hands the data folder to its own user and keeps the
  database in WAL mode.

These rules come from bugs fixed the hard way, and the tests enforce several of them:

- **New buttons, forms and loops.**
  - Put `@errors.guarded` on a new button's `callback` or a modal's `on_submit`.
  - End each round of a new `@tasks.loop` with `heartbeat.beat("<module>.<loop>", self.<loop>)` inside its `try`.

  `tests/test_errors.py` and `tests/test_heartbeat.py` fail when one is missing.
- **DynamicItem buttons.** Never keep your own state in `self.item`, `self.view`, `self.row` or the like:
  DynamicItem owns those names. Encode the state in the `custom_id`.
- **Translations.** A new or changed command description, option description or choice needs its Korean and
  Traditional Chinese entries in `assets/locales/ko.json` and `zh-TW.json`. `tests/test_translations.py` fails
  on a missing or leftover one. Command names stay in English.
- **The database.** A table change raises `SCHEMA_VERSION` in `db.py` and comes with its migration. The database
  migrates itself when the bot starts.
- **The guide.**
  - After any change to `docs/PLAYER-GUIDE.md`, run `python docs/make_pdf.py` (it needs `pip install markdown`
    and Chrome or Edge). Commit `docs/AION2-Bot-Guide.pdf` with its `.source-sha256` stamp in the same change;
    CI fails while they don't match.
  - `docs/GUIDE-full.md` is the same guide plus the hosting notes, so keep the two in step.
- **Dependencies.** Change `requirements.in`, then regenerate `requirements.txt` with the `pip-compile` command
  in its header. Every package is pinned with hashes.
- **Player-facing text.**
  - Keep it plain and friendly, and label admin-only features "(admins)" or "(officers)".
  - An embed holds at most 4096 characters of description and 6000 in all.

Please also:

- Keep each pull request to one change; small ones are reviewed much faster.
- Add or update tests for a change in behavior.
- Update the README and the guide when players would notice the change.
- Keep secrets out of the diff: bot tokens, `.env` files, databases and other people's data.

## Branches and pull requests

- Base your work on `main` and open the pull request against `main`.
- Explain in the commit messages why a change is made, not only what it does.
- CI must pass before a pull request is merged, and merging deploys it.

## Accessibility

Fera should work for people who use a screen reader or a keyboard. Give every button a text label. Say a state in
words or numbers, not with colour or an emoji alone. Write times as Discord timestamps, which each reader sees in
their own time zone. See [ACCESSIBILITY.md](ACCESSIBILITY.md) for what is in place and the known gaps.

## License

Fera is licensed under the **GPL-3.0**. By contributing, you agree that your contributions will be licensed under
the same license. See [LICENSE](LICENSE).

## Questions

Ask in the [Discord server](https://discord.gg/9y6zkUyvBv) or open an issue. Thanks for contributing!
