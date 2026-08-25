"""Tests for `__init__.async_setup_entry` / `async_unload_entry`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv (`.venv/bin/python -m pytest`) -- see `test_ha_world.py`'s own note.

`async_setup_entry`/`async_unload_entry` never touch `hass` (no platforms,
no coordinator yet -- that starts in a later phase), so `None` stands in for
it below rather than pulling in `fake_hass`; there is nothing for that fixture
to cover here.
"""

import asyncio

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import ConfigEntryNotReady

from cover_logic import CoverLogicData, async_setup_entry, async_unload_entry
from cover_logic.const import CONF_CONFIG_PATH

# Zero problems of any severity: one blind, one zone that owns it, one
# fallback mode, and a rule list for that (mode, zone) pair ending in an
# unconditional catch-all rule.
VALID_CONFIG = """
blinds:
  - entity: cover.a
zones:
  z:
    members: [cover.a]
modes:
  - {id: any}
rules:
  any.z:
    - {then: {position: keep, tilt: keep}}
"""

# Same shape but no `rules:` section at all -- validate() reports
# `missing_rule_list` for `any.z`, WARNING severity, nothing at ERROR.
WARNING_ONLY_CONFIG = """
blinds:
  - entity: cover.a
zones:
  z:
    members: [cover.a]
modes:
  - {id: any}
"""

# cover.a is declared but no zone claims it as a member -- validate()'s
# `blind_without_zone` check, ERROR severity. This is the exact shape the
# task brief names: "a blind belonging to no zone".
ERROR_CONFIG = """
blinds:
  - entity: cover.a
zones:
  z:
    members: []
modes:
  - {id: any}
rules:
  any.z:
    - {then: {position: keep, tilt: keep}}
"""

CONFIG_FILE_NAME = "cover_logic.yaml"


def _write(tmp_path, text):
    path = tmp_path / CONFIG_FILE_NAME
    path.write_text(text, encoding="utf-8")
    return str(path)


def _entry(make_entry, path):
    return make_entry({CONF_CONFIG_PATH: path})


def test_setup_succeeds_and_populates_runtime_data(tmp_path, make_entry):
    entry = _entry(make_entry, _write(tmp_path, VALID_CONFIG))

    assert asyncio.run(async_setup_entry(None, entry)) is True

    assert isinstance(entry.runtime_data, CoverLogicData)
    assert set(entry.runtime_data.config.blinds) == {"cover.a"}


def test_setup_raises_config_entry_not_ready_on_error_problem(tmp_path, make_entry):
    entry = _entry(make_entry, _write(tmp_path, ERROR_CONFIG))

    with pytest.raises(ConfigEntryNotReady, match="blind_without_zone"):
        asyncio.run(async_setup_entry(None, entry))


def test_setup_fails_cleanly_on_missing_file(tmp_path, make_entry):
    entry = _entry(make_entry, str(tmp_path / "does_not_exist.yaml"))

    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(async_setup_entry(None, entry))


def test_setup_succeeds_with_warning_only_config(tmp_path, make_entry):
    entry = _entry(make_entry, _write(tmp_path, WARNING_ONLY_CONFIG))

    assert asyncio.run(async_setup_entry(None, entry)) is True
    assert isinstance(entry.runtime_data, CoverLogicData)


def test_unload_succeeds(tmp_path, make_entry):
    entry = _entry(make_entry, _write(tmp_path, VALID_CONFIG))
    asyncio.run(async_setup_entry(None, entry))

    assert asyncio.run(async_unload_entry(None, entry)) is True


def test_reload_rereads_the_file_not_a_cached_parse(tmp_path, make_entry):
    """The whole debugging loop for phases 2-4 is: edit the YAML, reload the entry.

    Simulate a config entry reload the way Home Assistant performs one --
    unload, then set up again -- and assert the *edited* file's content, not
    the one read at the first setup, is what ends up in `entry.runtime_data`.
    A module-level cache of the parsed `Config` would make this fail while
    every other test here still passes.
    """
    path = _write(tmp_path, VALID_CONFIG)
    entry = _entry(make_entry, path)

    asyncio.run(async_setup_entry(None, entry))
    assert set(entry.runtime_data.config.blinds) == {"cover.a"}

    _write(tmp_path, VALID_CONFIG.replace("cover.a", "cover.b"))

    asyncio.run(async_unload_entry(None, entry))
    asyncio.run(async_setup_entry(None, entry))

    assert set(entry.runtime_data.config.blinds) == {"cover.b"}
