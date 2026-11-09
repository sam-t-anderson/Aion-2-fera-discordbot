# Security Policy

Thank you for helping keep Fera, the AION 2 Discord bot, and the Discord servers that use it safe.

## Supported versions

Fera runs as one hosted bot that updates itself from `main`: a `main` commit whose CI passed is installed
within about 15 minutes. Only the latest `main` is supported with security fixes. If you run your own copy,
update it to the latest `main` before reporting.

| Version | Supported |
| ------- | --------- |
| The hosted bot and the latest `main` | ✅ |
| Older commits, forks and copies that are behind `main` | ❌ |

## Reporting a vulnerability

**Please do not open a public issue, pull request or Discord message for a security problem.** Public
disclosure before a fix is available puts every server that uses the bot at risk.

Instead, report privately through GitHub Security Advisories:

> **[Report a vulnerability »](https://github.com/sam-t-anderson/Aion-2-fera-discordbot/security/advisories/new)**

This opens a private channel visible only to you and the maintainers. If you cannot use GitHub advisories,
contact the maintainer privately via the [Discord server](https://discord.gg/9y6zkUyvBv).

When reporting, please include:

- A description of the issue and its potential impact.
- Steps to reproduce (the command, button or form you used and what the bot did), or a proof of concept.
- The affected part (a slash command, a button or form, a background job, the health endpoint, the Docker
  image or the self-updater) and, for your own copy, the commit it runs.
- Any suggested remediation, if you have one.

Please **do not include** a bot token, a GitHub token, the contents of a `.env` file or other secrets, or
other people's messages and Discord IDs: describe where they are rather than pasting them.

## Testing safely

- Test in a Discord server you own or manage, ideally against your own copy of the bot
  (`docker compose up -d --build` with your own bot token, as the [README](README.md) describes).
- Read or change no other server's data beyond the least needed to show the problem, and report as soon as
  you can show it.
- No denial of service: don't flood the bot with commands, button presses or forms, and don't try to get it
  rate limited or banned by Discord.
- Leave other players and server members alone: no spam, mass mentions or social engineering.

## What to expect

This is a small, community-maintained project, so responses are best-effort:

- We aim to acknowledge a report within **7 days**.
- We will work with you on a fix and keep you updated on progress. A fix reaches the hosted bot once it is
  merged to `main` and its CI passes.
- Once the fix is out, we are happy to credit you in the advisory (or keep you anonymous if you prefer).
- We ask that you give us a reasonable chance to release a fix before any public disclosure (coordinated
  disclosure).

## Scope

In scope:

- The bot's code in this repository: its slash commands, buttons and forms, and its background jobs (the
  timers, the timer and DKP boards, event alerts and reminders, the kill feed, the news posts, and the boss
  and server lists).
- The separation between Discord servers and between members. Each server's settings, boss lists, links,
  manual timers, events, DKP and gear reservations are its own, and a member's checklist and reminders are
  private. A way for one server to read or change another's data, for a member to do what only a server
  manager or a DKP officer may, or to see another member's checklist or reminders, is in scope.
- What the bot posts: a way to make it ping `@everyone`, `@here` or a role, or post content it should not.
- Bad data from a source the bot reads that makes it crash, post something harmful or affect other servers.
- The health endpoint, the Docker image and its drop from root (`privileges.py`), the self-updater in
  `deploy/` (it installs only `main` commits whose CI passed) and the CI workflow.
- A known vulnerability in a pinned dependency (`requirements.txt`, with hashes) that affects the bot.

Out of scope / handled elsewhere:

- Discord itself and discord.py: report to Discord or the library's maintainers. Vulnerabilities in other
  third-party dependencies go to their maintainers; if one affects the bot, let us know so we can update.
- The sites the bot reads: gamers4.life (the event schedule), aion2data.com (the boss list) and the official
  AION 2 site (the server list). The log server behind the kill feed and the news lives in
  [Aion-2-tools-server](https://github.com/sam-t-anderson/Aion-2-tools-server), and the website that
  publishes the field-boss list in [Aion-2-tools](https://github.com/sam-t-anderson/Aion-2-tools): report
  issues in those there.
- A Discord server's own permission setup, such as giving everyone Manage Server or the DKP officer role.
- The game and its client.

## A note on how the bot works

Fera answers its own slash commands, buttons and forms. It has neither the message content intent nor the
server members intent and reads no channel messages. It keeps what it needs in one SQLite database on its
host: for each server its settings, lists, manual timers, events with their sign-ups, and the DKP ledger;
for each member their checklist ticks, timezone and timer reminders. The bot token and the other settings
live in the host's `.env` file, never in this repository. The health endpoint reports only the bot's own
state (its connection, uptime, the number of servers it is in, each background job's last good round and
the age of each data source), never a server's data; whether its port is reachable from outside is up to
the host.
