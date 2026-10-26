"""Runs each feature script in tests/scripts in a Python process of its own.

The scripts set DB_PATH (and some change settings) before they import the bot, so two of them can't share
a process. Each ends with an OK line; a failed check exits non-zero. Run one by hand from the repo root:
python tests/scripts/news_posting.py"""
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = sorted((ROOT / "tests" / "scripts").glob("*.py"))


def test_the_scripts_are_found():
    assert len(SCRIPTS) >= 22


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.stem for s in SCRIPTS])
def test_script(script):
    run = subprocess.run([sys.executable, str(script)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, (f"{script.name} exited with {run.returncode}\n"
                                 f"--- output\n{run.stdout[-4000:]}\n--- errors\n{run.stderr[-8000:]}")
