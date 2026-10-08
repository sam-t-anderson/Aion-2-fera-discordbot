"""Personal daily/weekly checklists (buttons on private messages)."""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db

log = logging.getLogger("aion2.checklist")

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# (id, label, times to complete, optional)
LISTS = {
    "daily": {
        "title": "Daily checklist",
        "items": [
            ("duties", "Duties (J, Duties tab)", 5, False),
            ("nightmare", "Nightmare", 2, False),
            ("odal", "Spend Odal energy", 1, False),
            ("shugo", "Shugo keys", 1, False),
            ("rift", "Space-time rift", 1, True),
            ("abyss", "Abyss farming", 1, True),
        ],
    },
    "weekly": {
        "title": "Weekly checklist",
        "items": [
            ("dd", "Daily Dungeon", 14, False),
            ("asc", "Ascension Trial", 3, False),
            ("cmd", "Command scrolls", 2, False),
            ("alts", "Alts parked at level 22", 1, True),
        ],
    },
}
ITEM_MAX = {k: {i[0]: i[2] for i in v["items"]} for k, v in LISTS.items()}


# ---------------------------------------------------------------- reset math
def period_start(kind: str, now: datetime | None = None) -> str:
    """Identifier (a date) of the current daily/weekly period."""
    now = now or datetime.now(timezone.utc)
    d = (now - timedelta(hours=config.RESET_UTC_HOUR)).date()
    if kind == "weekly":
        d -= timedelta(days=(d.weekday() - config.WEEKLY_RESET_WEEKDAY) % 7)
    return d.isoformat()


def next_reset(kind: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    start = datetime.fromisoformat(period_start(kind, now)).replace(tzinfo=timezone.utc)
    start += timedelta(hours=config.RESET_UTC_HOUR)
    return start + timedelta(days=1 if kind == "daily" else 7)


# ---------------------------------------------------------------- storage
def get_counts(user_id: int, kind: str) -> dict[str, int]:
    rows = db.conn().execute(
        "SELECT item, count FROM progress WHERE user_id=? AND kind=? AND period=?",
        (user_id, kind, period_start(kind)),
    ).fetchall()
    return {r["item"]: r["count"] for r in rows}


def set_count(user_id: int, kind: str, item: str, count: int) -> None:
    db.conn().execute(
        """INSERT INTO progress (user_id, kind, item, count, period) VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, kind, item)
           DO UPDATE SET count=excluded.count, period=excluded.period""",
        (user_id, kind, item, count, period_start(kind)),
    )
    db.conn().commit()


def purge_old(kind: str) -> None:
    db.conn().execute("DELETE FROM progress WHERE kind=? AND period<>?", (kind, period_start(kind)))
    db.conn().commit()


# ---------------------------------------------------------------- rendering
def render(kind: str, counts: dict[str, int]) -> discord.Embed:
    lines, done, total = [], 0, 0
    for item_id, label, n, optional in LISTS[kind]["items"]:
        c = min(counts.get(item_id, 0), n)
        done += c
        total += n
        mark = "✅" if c >= n else "⬜"
        progress = f" {c}/{n}" if n > 1 else ""
        opt = " *(optional)*" if optional else ""
        lines.append(f"{mark} **{label}**{progress}{opt}")
    reset_ts = int(next_reset(kind).timestamp())
    embed = discord.Embed(
        title=f"AION 2 {LISTS[kind]['title']}",
        description="\n".join(lines),
        color=0xA9741A,
    )
    embed.add_field(name="Resets", value=f"<t:{reset_ts}:R> (<t:{reset_ts}:f>)", inline=False)
    embed.set_footer(text=f"{done}/{total} done. Press a button to add one; press a full one to clear it.")
    return embed


def build_view(kind: str, counts: dict[str, int]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for item_id, label, n, _ in LISTS[kind]["items"]:
        c = min(counts.get(item_id, 0), n)
        text = f"{label} {c}/{n}" if n > 1 else label
        style = discord.ButtonStyle.success if c >= n else discord.ButtonStyle.secondary
        view.add_item(TickButton(kind, item_id, text, style))
    view.add_item(BulkButton(kind, "all", "Mark all done", discord.ButtonStyle.primary))
    view.add_item(BulkButton(kind, "clear", "Clear all", discord.ButtonStyle.danger))
    return view


def panel_embed() -> discord.Embed:
    wd = WEEKDAYS[config.WEEKLY_RESET_WEEKDAY]
    hh = f"{config.RESET_UTC_HOUR:02d}:00 UTC"
    return discord.Embed(
        title="AION 2 Checklists",
        description=(
            "Press a button to open **your own** checklist. Only you can see it, "
            "and your progress is saved separately from everyone else's.\n\n"
            f"Daily reset: {hh} every day.\n"
            f"Weekly reset: {wd}s at {hh}."
        ),
        color=0xA9741A,
    )


# ---------------------------------------------------------------- buttons
# DynamicItem buttons carry their state in the custom_id, so they keep working
# after the bot restarts, including on the private checklist messages.
class OpenButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"cl:open:(?P<kind>daily|weekly)",
):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(
            discord.ui.Button(
                label=f"My {kind} checklist",
                style=discord.ButtonStyle.primary,
                custom_id=f"cl:open:{kind}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"])

    async def callback(self, interaction: discord.Interaction):
        counts = get_counts(interaction.user.id, self.kind)
        await interaction.response.send_message(
            embed=render(self.kind, counts),
            view=build_view(self.kind, counts),
            ephemeral=True,
        )


class TickButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"cl:tick:(?P<kind>daily|weekly):(?P<item>[a-z]+)",
):
    def __init__(self, kind, item, label="Item", style=discord.ButtonStyle.secondary):
        self.kind, self.item_id = kind, item   # not self.item: DynamicItem uses that name for the wrapped button
        super().__init__(
            discord.ui.Button(label=label, style=style, custom_id=f"cl:tick:{kind}:{item}")
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"], match["item"])

    async def callback(self, interaction: discord.Interaction):
        uid, n = interaction.user.id, ITEM_MAX[self.kind].get(self.item_id)
        if n is None:
            await interaction.response.defer()
            return
        counts = get_counts(uid, self.kind)
        cur = counts.get(self.item_id, 0)
        counts[self.item_id] = 0 if cur >= n else cur + 1
        set_count(uid, self.kind, self.item_id, counts[self.item_id])
        await interaction.response.edit_message(
            embed=render(self.kind, counts), view=build_view(self.kind, counts)
        )


class BulkButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"cl:bulk:(?P<kind>daily|weekly):(?P<mode>all|clear)",
):
    def __init__(self, kind, mode, label="Bulk", style=discord.ButtonStyle.secondary):
        self.kind, self.mode = kind, mode
        super().__init__(
            discord.ui.Button(label=label, style=style, custom_id=f"cl:bulk:{kind}:{mode}")
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"], match["mode"])

    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        counts = {}
        for item_id, n in ITEM_MAX[self.kind].items():
            counts[item_id] = n if self.mode == "all" else 0
            set_count(uid, self.kind, item_id, counts[item_id])
        await interaction.response.edit_message(
            embed=render(self.kind, counts), view=build_view(self.kind, counts)
        )


# ---------------------------------------------------------------- cog
class Checklist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_period: dict[str, str] = {}

    async def cog_load(self):
        self.last_period = {k: period_start(k) for k in LISTS}
        self.reset_loop.start()

    async def cog_unload(self):
        self.reset_loop.cancel()

    @tasks.loop(seconds=30)
    async def reset_loop(self):
        try:
            for kind in LISTS:
                purge_old(kind)
                current = period_start(kind)
                if self.last_period.get(kind) != current:
                    self.last_period[kind] = current
                    log.info("%s reset", kind)
                    for gid, cid in db.announce_channels():
                        try:
                            channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                            await channel.send(f"The AION 2 **{kind}** checklist has reset.")
                        except discord.HTTPException:
                            log.info("Could not announce the %s reset in server %s", kind, gid)
        except Exception:
            log.exception("Checklist reset loop error")

    @reset_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="checklist-setup", description="Post the checklist buttons in this channel")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def checklist_setup(self, interaction: discord.Interaction):
        view = discord.ui.View(timeout=None)
        view.add_item(OpenButton("daily"))
        view.add_item(OpenButton("weekly"))
        await interaction.response.send_message(embed=panel_embed(), view=view)

    @app_commands.command(name="checklist", description="Show your own AION 2 checklist")
    @app_commands.describe(kind="Which checklist to open")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Daily", value="daily"),
            app_commands.Choice(name="Weekly", value="weekly"),
        ]
    )
    async def checklist(self, interaction: discord.Interaction, kind: app_commands.Choice[str]):
        counts = get_counts(interaction.user.id, kind.value)
        await interaction.response.send_message(
            embed=render(kind.value, counts), view=build_view(kind.value, counts), ephemeral=True
        )


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(OpenButton, TickButton, BulkButton)
    await bot.add_cog(Checklist(bot))
