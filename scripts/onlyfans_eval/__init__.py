"""OnlyFans repair evaluation harness (R0-R9 plan section 5). Local only; no OnlyFans client, sender or network.

Run from ``service/`` with ``PYTHONPATH=.``:  ``python ../scripts/onlyfans_eval/run.py --help``.
The case file is read by path argument and is never committed (it holds real account/fan ids).
"""
import os
import sys
import tempfile
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[2] / "service"


def bootstrap():
    """Make ``service`` importable and keep the shared modules away from any real database or dotenv file."""
    if str(SERVICE) not in sys.path:
        sys.path.insert(0, str(SERVICE))
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    if "eval-" not in os.environ.get("DATABASE_URL", ""):
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + tempfile.mkdtemp(prefix="eval-") + "/eval.db"
