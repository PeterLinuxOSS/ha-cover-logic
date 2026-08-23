"""Shared fixtures. Phase 1 needs almost nothing — the engine is pure."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return REPO_ROOT / "fixtures"
