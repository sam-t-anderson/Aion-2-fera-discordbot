"""Korean and Traditional Chinese for the slash commands' descriptions and choices.

Discord shows a command's description, its options' descriptions and their choices in each person's own
client language when the bot sends a translation. Command and option names stay English: they are what
people type, and the bot's replies are English. The texts are in assets/locales/<code>.json, keyed by
the English text in the code; anything missing shows in English. They are drafts until a native speaker
has reviewed them."""
import json
import logging
from pathlib import Path

import discord
from discord import app_commands

log = logging.getLogger("aion2.translations")

LOCALES = {discord.Locale.korean: "ko", discord.Locale.taiwan_chinese: "zh-TW"}
FOLDER = Path(__file__).resolve().parent / "assets" / "locales"
TRANSLATED = {
    app_commands.TranslationContextLocation.command_description,
    app_commands.TranslationContextLocation.group_description,
    app_commands.TranslationContextLocation.parameter_description,
    app_commands.TranslationContextLocation.choice_name,
}


def load(code: str) -> dict[str, str]:
    """English text -> translation for one locale; empty when the file is missing or broken."""
    try:
        doc = json.loads((FOLDER / f"{code}.json").read_text(encoding="utf-8"))
        return {k: v.strip() for k, v in doc["strings"].items() if isinstance(v, str) and 0 < len(v.strip()) <= 100}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
        log.warning("No %s translations (%s); commands show in English there", code, err)
        return {}


class Translator(app_commands.Translator):
    def __init__(self):
        super().__init__()
        self.tables = {locale: load(code) for locale, code in LOCALES.items()}

    async def translate(self, string: app_commands.locale_str, locale: discord.Locale,
                        context: app_commands.TranslationContext) -> str | None:
        if context.location not in TRANSLATED:
            return None
        return self.tables.get(locale, {}).get(string.message)
