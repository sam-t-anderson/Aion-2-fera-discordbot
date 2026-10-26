"""Shared set-up for the tests run by pytest.

The bot reads DB_PATH when config is imported, so it points at a temporary folder before any test imports
the bot. The feature scripts in tests/scripts are run by test_scripts.py, each in a process of its own,
not imported here."""
import os
import tempfile

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="fera-tests-"), "checklist.db"))

collect_ignore_glob = ["scripts/*", "fixtures/*"]
