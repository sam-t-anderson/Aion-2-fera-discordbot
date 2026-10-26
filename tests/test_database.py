"""The database's own settings: write-ahead logging, a lock timeout and the layout version."""
import logging
import sqlite3

import db


def pragma(name):
    return db.conn().execute(f"PRAGMA {name}").fetchone()[0]


def test_a_new_database_uses_wal_and_records_its_layout(tmp_path):
    db.init(str(tmp_path / "checklist.db"))
    assert pragma("journal_mode") == "wal"
    assert pragma("busy_timeout") == 10_000
    assert pragma("user_version") == db.SCHEMA_VERSION >= 1
    db.meta_set("k", "v")
    assert (tmp_path / "checklist.db-wal").exists()            # the data folder holds the db with its -wal and -shm


def test_an_older_database_is_brought_up_to_date(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    old.commit()
    old.close()
    db.init(str(path))
    assert pragma("user_version") == db.SCHEMA_VERSION and pragma("journal_mode") == "wal"


def test_a_database_from_a_newer_bot_is_kept_and_flagged(tmp_path, caplog):
    path = tmp_path / "newer.db"
    newer = sqlite3.connect(path)
    newer.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 5}")
    newer.commit()
    newer.close()
    with caplog.at_level(logging.WARNING, logger="aion2.db"):
        db.init(str(path))
    assert pragma("user_version") == db.SCHEMA_VERSION + 5     # not lowered
    assert "newer version of the bot" in caplog.text


def test_a_lock_held_elsewhere_is_waited_for(tmp_path):
    db.init(str(tmp_path / "checklist.db"))
    other = sqlite3.connect(tmp_path / "checklist.db", timeout=0)
    other.execute("BEGIN IMMEDIATE")                             # someone else is writing
    other.execute("INSERT INTO meta VALUES ('x', '1')")
    other.commit()                                               # ...and finishes before the bot's timeout
    db.meta_set("y", "2")
    assert db.meta_get("x") == "1" and db.meta_get("y") == "2"
    other.close()
