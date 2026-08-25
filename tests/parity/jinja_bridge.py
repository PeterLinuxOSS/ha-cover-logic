"""Thin bridge to the live Jinja matrix in /config.

Deliberately contains no logic of its own: `matica.py` renders the template
straight out of configuration.yaml, so the gate compares against reality rather
than against a second copy of it.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import sys

HA_TESTS = Path(os.environ.get("HA_TESTS_DIR", "/config/tests"))

if str(HA_TESTS) not in sys.path:
    sys.path.insert(0, str(HA_TESTS))

import matica  # noqa: E402

Stav = matica.Stav
ciele = matica.ciele
rezim = matica.rezim
VSETKY = matica.VSETKY


def available() -> bool:
    return (HA_TESTS / "matica.py").exists()


def now_for(stav) -> dt.datetime:
    """Same clock matica._globals uses — 13:00 or 12:00 depending on po_1230."""
    return dt.datetime(2026, 8, 19, 13 if stav.po_1230 else 12, 0)
