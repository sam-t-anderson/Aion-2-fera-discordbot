# Accessibility statement

_Last updated: 9 November 2026._

Fera aims to be usable by everyone in the Discord servers that add it, including people who use a screen reader, a
keyboard or other assistive technology. This statement describes what is in place, the known limitations, and how
to tell us about a barrier. It describes the current state honestly rather than claiming a certification the bot
has not earned.

## Scope

This statement covers what the bot itself shows and asks for in Discord: its slash commands, buttons, forms,
messages and direct messages. It also covers its guide, `docs/PLAYER-GUIDE.md`, and the PDF printed from it.

Discord's own app is outside it. The bot appears inside Discord's interface, so Discord's accessibility settings
apply to the bot's messages as to any others: screen reader support, keyboard navigation, text size, saturation
and reduced motion. The websites the bot links to are outside it too.

## What is in place

- **Discord's standard parts only.** Everything happens through slash commands with descriptions, buttons, forms
  and embedded messages, which Discord exposes to screen readers and keyboards. The bot posts no images of text.
- **Every button says what it does.** Buttons carry text labels, such as *My daily checklist*, *Mark all done*,
  *Check in* or *Undo*. None is an icon alone, and a link opens from a labelled *Open* button.
- **Words and numbers, not colour alone.** Button and embed colours only repeat what the text says:
  - a checklist item shows a mark and a count (✅ or ⬜, *2/5*), and the list says how many are done;
  - a sign-up reads *Yes* or *Maybe*;
  - an event that ended or was cancelled says so in its title.
- **Times in your own time zone.**
  - Times are Discord timestamps, which Discord shows in each reader's own time zone and date format.
  - Countdowns read as, say, *in 5 minutes*.
  - Times you type (*8pm*, *tomorrow 7:30pm*, *in 2h*) use the time zone you set with `/timezone set`.
- **Your own things stay private.** Your checklists, reminders and most command replies are shown only to you, so
  you don't have to find them among other people's messages.
- **Reminders on your terms.** DM reminders are opt-in: for one timer or a whole category, when it starts or 1, 3,
  5 or 10 minutes before.
- **Tables you can take with you.** Officers can export the DKP standings with `/dkp export` as a CSV file, which
  any spreadsheet or screen reader can open.
- **Plain language.** Replies are short and plain, and the features for server managers and officers are labelled
  as such.
- **Other languages.** For people whose Discord is set to Korean or Traditional Chinese, the command descriptions
  and choices are in that language. These are draft translations awaiting native review. Command names and the
  bot's replies stay in English.

## Known limitations

- **No formal audit.** The bot has not been independently audited, nor tested systematically with screen readers
  on every platform Discord supports.
- **Emoji are read aloud.** Screen readers read emoji marks by their names, such as "check mark button", which
  makes long lists wordy.
- **Live boards change silently.** The timer and DKP boards are edited in place every couple of minutes, and
  Discord does not announce an edited message. `/timers show` and `/dkp top` give a fresh copy as a reply.
- **English replies.** The bot's replies and its guide are in English only.
- **Discord decides the presentation.** How messages, buttons and forms look and read is up to Discord's app.

## Feedback

If something in the bot is hard to use, please tell us. Reports shape what we work on next:

- open an issue on [GitHub](https://github.com/sam-t-anderson/Aion-2-fera-discordbot/issues) and mention
  "accessibility", or
- reach us on the [Discord server](https://discord.gg/9y6zkUyvBv).

Say which command, button or message it was, what you were trying to do and the barrier you hit. If it helps, say
which assistive technology, device or Discord app you use.
