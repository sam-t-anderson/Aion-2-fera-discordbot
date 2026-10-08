"""Private link commands, plus an admin command to add your own links."""

import re

import discord
from discord import app_commands
from discord.ext import commands

import db
import util

BUILTIN = {
    "map": ("AION 2 Interactive Map", "https://interactivemap.app/aion2/maps/verteron"),
    "db": ("AION 2 Database", "https://gamers4.life/aion-2/database/en/"),
    "leaderboard": ("AION 2 Leaderboard", "https://gamers4.life/aion-2/database/en/leaderboard/"),
    "daevanion": ("Daevanion Node Planner", "https://gamers4.life/aion-2/database/en/daevanion-planner/"),
    "build": ("Build Planner", "https://gamers4.life/aion-2/database/en/build-calculator/"),
    "news": ("AION 2 News", "https://aion2.online/news/"),
    "twitter": ("AION 2 on X (Twitter)", "https://x.com/AION2Official?lang=en"),
    "abysslogs": ("Abyss Logs (combat logging and uploads)", "https://abysslogs.com/"),
}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def all_links(guild_id: int | None = None) -> dict[str, tuple[str, str]]:
    links = dict(BUILTIN)
    rows = []
    if guild_id:
        rows = db.conn().execute(
            "SELECT name, label, url FROM guild_links WHERE guild_id=? ORDER BY name", (guild_id,)
        ).fetchall()
    for r in rows:
        links.setdefault(r["name"], (r["label"], r["url"]))
    return links


class Links(commands.Cog):
    @app_commands.command(name="link", description="Get a link to an AION 2 site (only you see it)")
    @app_commands.describe(site="Which site")
    async def link(self, interaction: discord.Interaction, site: str):
        links = all_links(interaction.guild_id)
        key = site.strip().lower()
        if key not in links:
            await interaction.response.send_message(
                "I don't have that link. Start typing and pick one from the list, or use `/links`.", ephemeral=True
            )
            return
        label, url = links[key]
        view = discord.ui.View()
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Open", url=url))
        await interaction.response.send_message(
            f"**{label}**\n{url}", view=view, ephemeral=True, suppress_embeds=True
        )

    @link.autocomplete("site")
    async def _link_ac(self, interaction: discord.Interaction, current: str):
        out = []
        for key, (label, _) in all_links(interaction.guild_id).items():
            if current.lower() in key or current.lower() in label.lower():
                out.append(app_commands.Choice(name=f"{key} — {label}"[:100], value=key))
        return out[:25]

    @app_commands.command(name="links", description="List every saved AION 2 link (only you see it)")
    async def links(self, interaction: discord.Interaction):
        lines = [f"**{key}** — [{label}]({url})" for key, (label, url) in all_links(interaction.guild_id).items()]
        embed = discord.Embed(title="AION 2 links", description=util.cut("\n".join(lines), 4000), color=0xA9741A)
        embed.set_footer(text="Open one directly with /link")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    admin = app_commands.Group(
        name="linkadmin",
        description="Admin: manage the link list",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @admin.command(name="add", description="Add or update a link")
    @app_commands.describe(name="Short name, letters and numbers (for example guide)", url="Full web address", label="Display title")
    async def add(self, interaction: discord.Interaction, name: str, url: str, label: str | None = None):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        key = name.strip().lower()
        if not NAME_RE.match(key):
            await interaction.response.send_message(
                "Use a short name with lowercase letters, numbers, `-` or `_` (up to 32 characters).", ephemeral=True
            )
            return
        if key in BUILTIN:
            await interaction.response.send_message(f"`{key}` is a built-in link and can't be changed.", ephemeral=True)
            return
        url = url.strip()
        if not re.match(r"^https?://[^\s]+$", url) or len(url) > 500:
            await interaction.response.send_message("The link must start with http:// or https://.", ephemeral=True)
            return
        db.conn().execute(
            """INSERT INTO guild_links (guild_id, name, label, url) VALUES (?,?,?,?)
               ON CONFLICT(guild_id, name) DO UPDATE SET label=excluded.label, url=excluded.url""",
            (interaction.guild_id, key, (label or key)[:80], url),
        )
        db.conn().commit()
        await interaction.response.send_message(f"Saved link `{key}`. Everyone in this server can open it with `/link`.", ephemeral=True)

    @admin.command(name="remove", description="Remove a link you added")
    @app_commands.describe(name="The link to remove")
    async def remove(self, interaction: discord.Interaction, name: str):
        if not util.is_admin(interaction):
            await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
            return
        key = name.strip().lower()
        if key in BUILTIN:
            await interaction.response.send_message("Built-in links can't be removed.", ephemeral=True)
            return
        cur = db.conn().execute("DELETE FROM guild_links WHERE guild_id=? AND name=?", (interaction.guild_id, key))
        db.conn().commit()
        await interaction.response.send_message(
            f"Removed `{key}`." if cur.rowcount else "There's no custom link with that name.", ephemeral=True
        )

    @remove.autocomplete("name")
    async def _remove_ac(self, interaction: discord.Interaction, current: str):
        rows = db.conn().execute(
            "SELECT name FROM guild_links WHERE guild_id=? AND name LIKE ? ORDER BY name LIMIT 25",
            (interaction.guild_id, f"%{current.lower()}%"),
        ).fetchall()
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


async def setup(bot: commands.Bot):
    await bot.add_cog(Links())
