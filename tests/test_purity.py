"""The decision core must not import Home Assistant.

This is what makes exhaustive testing possible: no HA, no event loop, no I/O.
If someone adds `from homeassistant...` to one of these modules, this fails.
"""

import ast
from pathlib import Path

import pytest

PURE_MODULES = [
    "model.py",
    "world.py",
    "conditions.py",
    "config_schema.py",
    "config_store.py",
    "conformance.py",
    "engine.py",
    "guards.py",
    "validation.py",
    "legacy.py",
    "starter_config.py",
    "planner.py",
    # Execution-layer, but genuinely HA-free and listed here so it has to stay
    # that way: `command_log.py` holds no clock either (its timestamp is
    # injected). It is one convenience import away from needing `hass`, and
    # that import should have to argue with a failing test first.
    "command_log.py",
]

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "cover_logic"


@pytest.mark.parametrize("filename", PURE_MODULES)
def test_module_does_not_import_homeassistant(filename: str) -> None:
    path = PKG / filename
    assert path.exists(), f"{filename} does not exist yet"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert not name.startswith("homeassistant"), (
                f"{filename} imports {name!r}; pure modules must stay HA-free"
            )
