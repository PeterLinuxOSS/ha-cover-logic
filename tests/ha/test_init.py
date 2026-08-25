"""Tests for `__init__.async_setup_entry` / `async_unload_entry`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv (`.venv/bin/python -m pytest`) -- see `test_ha_world.py`'s own note.

`async_setup_entry`/`async_unload_entry` now forward/unload the `sensor`
platform through `hass.config_entries`, so a real `hass` (or a stand-in for
one) is needed; `setup_hass` (`tests/ha/conftest.py`) is a fake covering
exactly that surface -- see its own docstring for why a real `HomeAssistant`
is not built here instead. `None` still works for `build_world` itself
(`VALID_CONFIG` and friends below reference no entity, so it is never
reached), but `hass.config_entries.async_forward_entry_setups(...)` needs a
real attribute to call, which `None` does not have.
"""

import asyncio

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import ConfigEntryNotReady

from cover_logic import PLATFORMS, CoverLogicData, async_setup_entry, async_unload_entry
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


def test_setup_succeeds_and_populates_runtime_data(tmp_path, make_entry, setup_hass):
    entry = _entry(make_entry, _write(tmp_path, VALID_CONFIG))
    hass = setup_hass()

    assert asyncio.run(async_setup_entry(hass, entry)) is True

    assert isinstance(entry.runtime_data, CoverLogicData)
    assert set(entry.runtime_data.config.blinds) == {"cover.a"}


def test_setup_forwards_the_sensor_platform(tmp_path, make_entry, setup_hass):
    """The whole point of wiring platforms up: `sensor` gets forwarded for this entry."""
    entry = _entry(make_entry, _write(tmp_path, VALID_CONFIG))
    hass = setup_hass()

    asyncio.run(async_setup_entry(hass, entry))

    assert hass.config_entries.forwarded == [(entry, PLATFORMS)]


def test_setup_raises_config_entry_not_ready_on_error_problem(tmp_path, make_entry, setup_hass):
    entry = _entry(make_entry, _write(tmp_path, ERROR_CONFIG))

    with pytest.raises(ConfigEntryNotReady, match="blind_without_zone"):
        asyncio.run(async_setup_entry(setup_hass(), entry))


def test_setup_fails_cleanly_on_missing_file(tmp_path, make_entry, setup_hass):
    entry = _entry(make_entry, str(tmp_path / "does_not_exist.yaml"))

    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(async_setup_entry(setup_hass(), entry))


def test_setup_succeeds_with_warning_only_config(tmp_path, make_entry, setup_hass):
    entry = _entry(make_entry, _write(tmp_path, WARNING_ONLY_CONFIG))

    assert asyncio.run(async_setup_entry(setup_hass(), entry)) is True
    assert isinstance(entry.runtime_data, CoverLogicData)


def test_unload_succeeds(tmp_path, make_entry, setup_hass):
    entry = _entry(make_entry, _write(tmp_path, VALID_CONFIG))
    hass = setup_hass()
    asyncio.run(async_setup_entry(hass, entry))

    assert asyncio.run(async_unload_entry(hass, entry)) is True
    assert hass.config_entries.unloaded == [(entry, PLATFORMS)]


def test_unload_fails_and_leaves_coordinator_alone_when_a_platform_refuses(
    tmp_path, make_entry, setup_hass
):
    """A platform that refuses to unload must abort before the coordinator is touched.

    Home Assistant's own convention: `async_unload_entry` returning `False`
    leaves the entry's state untouched for a retry, not half torn down.
    """
    entry = _entry(make_entry, _write(tmp_path, VALID_CONFIG))
    hass = setup_hass(unload_result=False)
    asyncio.run(async_setup_entry(hass, entry))
    coordinator = entry.runtime_data.coordinator

    assert asyncio.run(async_unload_entry(hass, entry)) is False
    # The coordinator is still the one from setup -- async_unload_entry
    # returned before ever calling CoverLogicCoordinator.async_unload.
    assert entry.runtime_data.coordinator is coordinator


def test_reload_rereads_the_file_not_a_cached_parse(tmp_path, make_entry, setup_hass):
    """The whole debugging loop for phases 2-4 is: edit the YAML, reload the entry.

    Simulate a config entry reload the way Home Assistant performs one --
    unload, then set up again -- and assert the *edited* file's content, not
    the one read at the first setup, is what ends up in `entry.runtime_data`.
    A module-level cache of the parsed `Config` would make this fail while
    every other test here still passes.
    """
    path = _write(tmp_path, VALID_CONFIG)
    entry = _entry(make_entry, path)
    hass = setup_hass()

    asyncio.run(async_setup_entry(hass, entry))
    assert set(entry.runtime_data.config.blinds) == {"cover.a"}

    _write(tmp_path, VALID_CONFIG.replace("cover.a", "cover.b"))

    asyncio.run(async_unload_entry(hass, entry))
    asyncio.run(async_setup_entry(hass, entry))

    assert set(entry.runtime_data.config.blinds) == {"cover.b"}
