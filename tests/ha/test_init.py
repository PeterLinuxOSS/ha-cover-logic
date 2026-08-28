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
from pathlib import Path
import threading
from unittest import mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import ConfigEntryNotReady

from cover_logic import PLATFORMS, CoverLogicData, async_setup_entry, async_unload_entry
from cover_logic.config_schema import load_config
from cover_logic.config_store import subentries_from_config
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


def test_setup_reads_the_config_file_off_the_event_loop(tmp_path, make_entry, setup_hass):
    """`load_config_file` does a blocking `Path.read_text` -- calling it directly
    from `async_setup_entry` blocks Home Assistant's event loop (its own
    `protect_loop` machinery logs exactly this for a raw `open()` there). It
    must run inside `hass.async_add_executor_job`, on a thread other than the
    caller's -- the same thread `Path.read_text` runs on is proof it never
    went through the executor at all.
    """
    entry = _entry(make_entry, _write(tmp_path, VALID_CONFIG))
    hass = setup_hass()
    read_thread_idents = []
    original_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):
        read_thread_idents.append(threading.get_ident())
        return original_read_text(self, *args, **kwargs)

    async def run():
        with mock.patch.object(Path, "read_text", spy_read_text):
            await async_setup_entry(hass, entry)

    caller_thread_ident = threading.get_ident()
    asyncio.run(run())

    assert read_thread_idents, "load_config_file never read the file at all"
    assert read_thread_idents[0] != caller_thread_ident


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


# --- reading from subentries, and the fixture-conformance repair issue -----
#
# All of these go through `async_setup_entry`'s `if entry.subentries:` branch
# (see that function's own docstring), which also calls
# `_check_fixture_conformance` -- and this checkout's own
# `fixtures/dom_peter.yaml` genuinely exists on disk here (see
# `conformance.repo_fixture_path`'s docstring), so every test below that
# takes this branch must stub `homeassistant.helpers.issue_registry`'s two
# functions rather than let them run against `FakeSetupHass`, which has no
# `.data` for a real `IssueRegistry` to live in.


def _subentries_for_config(config):
    """Every subentry `config` would produce, wrapped as real `ConfigSubentry`s."""
    return {
        f"sub{i}": ConfigSubentry(data=data, subentry_type=kind, title="", unique_id=None)
        for i, (kind, data) in enumerate(subentries_from_config(config))
    }


def _subentries_for(text):
    return _subentries_for_config(load_config(text))


def test_setup_reads_config_from_subentries_when_present(make_entry, setup_hass):
    """Once an entry has subentries, `Config` must come from them -- never from
    `CONF_CONFIG_PATH`, which this entry does not even carry (a version-2
    entry, per `async_migrate_entry`'s own docstring, drops that key).
    """
    entry = make_entry({}, subentries=_subentries_for(VALID_CONFIG))
    hass = setup_hass()

    with (
        mock.patch("homeassistant.helpers.issue_registry.async_create_issue"),
        mock.patch("homeassistant.helpers.issue_registry.async_delete_issue"),
    ):
        assert asyncio.run(async_setup_entry(hass, entry)) is True

    assert set(entry.runtime_data.config.blinds) == {"cover.a"}


def test_setup_creates_fixture_drift_issue_when_subentries_diverge(make_entry, setup_hass):
    """The whole point of this task's conformance check: a subentry-backed
    config that has drifted from this checkout's own `fixtures/dom_peter.yaml`
    must raise a loud, persistent repair issue, not pass unnoticed.
    """
    entry = make_entry({}, subentries=_subentries_for(VALID_CONFIG))
    hass = setup_hass()

    with (
        mock.patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
        mock.patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        asyncio.run(async_setup_entry(hass, entry))

    create.assert_called_once()
    _hass_arg, domain, issue_id = create.call_args.args
    assert (domain, issue_id) == ("cover_logic", "fixture_drift")
    assert create.call_args.kwargs["translation_key"] == "fixture_drift"
    assert "blinds" in create.call_args.kwargs["translation_placeholders"]["fields"]
    delete.assert_not_called()


def test_check_fixture_conformance_clears_the_issue_when_config_matches_the_real_fixture():
    """This checkout's own `fixtures/dom_peter.yaml`, loaded back as a `Config`, must
    compare equal to itself: proof this conformance check does not fire on a
    false positive, only on a real one.

    Calls `_check_fixture_conformance` directly rather than through the full
    `async_setup_entry` -> `CoverLogicCoordinator.async_setup` path: the real
    fixture's many referenced entities would make the coordinator reach for
    `homeassistant.helpers.event.async_track_state_change_event`, which needs
    a real `hass.bus`/`hass.data` `FakeSetupHass` does not provide (see
    `hass_factory`'s own docstring on why `test_coordinator.py` uses a real,
    minimal `HomeAssistant` for exactly that reason) -- disproportionate for
    a test about the conformance check alone, which never touches `hass`
    itself except to pass it through to the two mocked `issue_registry`
    calls below.
    """
    from cover_logic import _check_fixture_conformance  # noqa: PLC0415
    from cover_logic.config_schema import load_config_file  # noqa: PLC0415
    from cover_logic.conformance import repo_fixture_path  # noqa: PLC0415

    fixture = repo_fixture_path()
    assert fixture is not None, "this test suite must run from inside the project checkout"
    config = load_config_file(fixture)

    with (
        mock.patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
        mock.patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        _check_fixture_conformance(mock.sentinel.hass, config)

    create.assert_not_called()
    delete.assert_called_once_with(mock.sentinel.hass, "cover_logic", "fixture_drift")
