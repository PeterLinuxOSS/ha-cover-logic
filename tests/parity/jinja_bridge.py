"""Thin bridge to the live Jinja matrix in /config.

Deliberately contains no logic of its own: `matica.py` renders the template
straight out of configuration.yaml, so the gate compares against reality rather
than against a second copy of it.
"""

import datetime as dt
import os
from pathlib import Path
import sys

HA_TESTS = Path(os.environ.get("HA_TESTS_DIR", "/config/tests"))

if str(HA_TESTS) not in sys.path:
    sys.path.insert(0, str(HA_TESTS))

_EXPORTS = ("Stav", "ciele", "rezim", "VSETKY")


def available() -> bool:
    return (HA_TESTS / "matica.py").exists()


def __getattr__(name: str):
    """Re-export matica lazily -- an eager import beats `available()` and every
    `skipif` built on it, so a checkout without `/config/tests/matica.py`
    failed collection instead of skipping the host-only parity tests.
    """
    if name in _EXPORTS:
        import matica  # noqa: PLC0415

        return getattr(matica, name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def now_for(stav) -> dt.datetime:
    """Same clock matica._globals uses — 13:00 or 12:00 depending on po_1230."""
    return dt.datetime(2026, 8, 19, 13 if stav.po_1230 else 12, 0)
