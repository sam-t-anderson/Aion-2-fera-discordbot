"""The container starts as root, gives the data folder to the bot's user and then runs as that user."""
import logging
import os
import pwd
from types import SimpleNamespace

import pytest

import privileges

BOT = SimpleNamespace(pw_name="bot", pw_uid=1000, pw_gid=1000, pw_dir="/app")


@pytest.fixture
def system(monkeypatch, tmp_path):
    """Root with a ``bot`` user; ownership and identity changes are recorded instead of made."""
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: BOT if name == "bot" else (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr(os, "chown", lambda path, uid, gid, follow_symlinks=True: calls.append(("chown", str(path), uid, gid, follow_symlinks)))
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("setgroups", groups)))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("setgid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("setuid", uid)))
    monkeypatch.setenv("HOME", "/root")
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "checklist.db").write_text("x")
    (data / "sub" / "field-bosses.live.json").write_text("{}")
    (data / "link").symlink_to("/etc/passwd")
    return SimpleNamespace(calls=calls, data=data)


def test_root_hands_over_the_data_folder_then_becomes_the_bot(system):
    assert privileges.drop("bot", str(system.data)) is True
    chowned = {c[1]: c[2:] for c in system.calls if c[0] == "chown"}
    d = system.data
    assert set(chowned) == {str(d), str(d / "sub"), str(d / "checklist.db"), str(d / "sub" / "field-bosses.live.json"), str(d / "link")}
    assert all(v == (1000, 1000, False) for v in chowned.values())    # a link is changed itself, never followed
    assert [c[0] for c in system.calls if c[0] != "chown"] == ["setgroups", "setgid", "setuid"]
    assert os.environ["HOME"] == "/app"


def test_a_missing_data_folder_is_made(system):
    target = system.data / "new"
    assert privileges.drop("bot", str(target)) is True and target.is_dir()


def test_nothing_changes_without_root_or_a_user(system, monkeypatch):
    assert privileges.drop("", str(system.data)) is False
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert privileges.drop("bot", str(system.data)) is False
    assert system.calls == []


def test_an_unknown_user_keeps_root_and_says_so(system, caplog):
    with caplog.at_level(logging.WARNING, logger="aion2.privileges"):
        assert privileges.drop("nobody-here", str(system.data)) is False
    assert system.calls == [] and "No user 'nobody-here'" in caplog.text


def test_a_folder_that_cannot_be_handed_over_keeps_root(system, monkeypatch, caplog):
    def refuse(*args, **kwargs):
        raise PermissionError("read-only file system")
    monkeypatch.setattr(os, "chown", refuse)
    with caplog.at_level(logging.WARNING, logger="aion2.privileges"):
        assert privileges.drop("bot", str(system.data)) is False
    assert not [c for c in system.calls if c[0] in ("setgroups", "setgid", "setuid")]
    assert "running as root" in caplog.text and os.environ["HOME"] == "/root"
