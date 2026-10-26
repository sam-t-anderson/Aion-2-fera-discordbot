"""Korean and Traditional Chinese command texts: complete, within Discord's limits, and names left English."""
import asyncio
import json

import pytest
from discord.ext import tasks

import bot
import health
import translations


@pytest.fixture(scope="module")
def tree():
    """(translated payloads, plain payloads) for every command, as setup_hook builds them (no network, no loops)."""
    async def no_health(_bot):
        return None

    async def no_sync(*args, **kwargs):
        return []

    async def build():
        client = bot.AionBot()
        client.tree.sync = no_sync
        await client.setup_hook()
        commands = client.tree.get_commands()
        translated = [await c.get_translated_payload(client.tree, client.tree.translator) for c in commands]
        plain = [c.to_dict(client.tree) for c in commands]
        for cog in list(client.cogs.values()):
            await client.remove_cog(cog.qualified_name)
        return translated, plain

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tasks.Loop, "start", lambda self, *args, **kwargs: None)
        mp.setattr(health, "start", no_health)
        return asyncio.run(build())


def texts(payloads):
    """Every description and choice name in the plain payloads."""
    found = set()

    def walk(node):
        if node.get("description"):
            found.add(node["description"])
        for choice in node.get("choices") or []:
            found.add(choice["name"])
        for option in node.get("options") or []:
            walk(option)

    for payload in payloads:
        walk(payload)
    return found


def test_every_description_and_choice_has_both_translations(tree):
    needed = texts(tree[1])
    assert len(needed) > 150
    for code in translations.LOCALES.values():
        missing = sorted(needed - set(translations.load(code)))
        assert not missing, f"{code}.json lacks: {missing}"


def test_no_translation_is_left_over(tree):
    needed = texts(tree[1])
    for code in translations.LOCALES.values():
        stale = sorted(set(translations.load(code)) - needed)
        assert not stale, f"{code}.json has text no command uses any more: {stale}"


def test_the_files_are_marked_as_drafts_and_fit_discord():
    for code in translations.LOCALES.values():
        doc = json.loads((translations.FOLDER / f"{code}.json").read_text(encoding="utf-8"))
        assert doc["locale"] == code and "review" in doc["status"]
        assert all(0 < len(v) <= 100 and v == v.strip() for v in doc["strings"].values())


def test_discord_gets_descriptions_and_choices_in_both_languages_and_english_names(tree):
    def walk(node, where):
        assert not node.get("name_localizations"), f"{where} has a translated name"
        if node.get("description"):
            assert set(node.get("description_localizations") or {}) == {"ko", "zh-TW"}, where
        for choice in node.get("choices") or []:
            assert set(choice.get("name_localizations") or {}) == {"ko", "zh-TW"}, (where, choice["name"])
        for option in node.get("options") or []:
            walk(option, f"{where} {option['name']}")

    for payload in tree[0]:
        walk(payload, "/" + payload["name"])
    event = next(p for p in tree[0] if p["name"] == "event")
    assert event["description_localizations"] == {"ko": "그룹 일정 계획", "zh-TW": "規劃團隊活動"}


def test_a_missing_file_leaves_english(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(translations, "FOLDER", tmp_path)
    translator = translations.Translator()
    assert translator.tables == {locale: {} for locale in translations.LOCALES}
    assert "commands show in English" in caplog.text
