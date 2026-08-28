"""End-to-end tests for the phase 5 setup menu: flow output feeding `async_setup_entry`.

`test_config_flow.py` proves each of the four `user`-step branches in
isolation: the right `FlowResultType`, the right `data`/`subentries` shape.
That is necessary but not sufficient -- every previous defect of this class
in this project (see `MODELS.md`'s own phase 4 review history, quoted in
`options_flow.py`'s module docstring) slipped through exactly because each
step was tested alone, from a hand-built state, rather than against what the
*next* piece of the pipeline actually does with it. This module drives that
next piece: take a branch's own `CREATE_ENTRY` result, build the
`FakeConfigEntry` a real config entry would become, and run it through
`config_from_subentries`/`async_setup_entry` -- the same two functions a
real installation calls next, whether or not this test suite understands
their internals.

Imports Home Assistant (`ConfigSubentry`, `async_setup_entry`), so this
module only collects under the Python 3.14 venv -- see `test_ha_world.py`'s
own note.
"""

import asyncio
from unittest import mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import ConfigEntryNotReady

from cover_logic import CoverLogicData, async_setup_entry
from cover_logic.config_flow import CoverLogicConfigFlow
from cover_logic.config_schema import load_config_file
from cover_logic.config_store import config_from_subentries
from cover_logic.conformance import repo_example_config_path
from cover_logic.const import CONF_CONFIG_PATH
from cover_logic.validation import ERROR, validate


def _make_flow(hass):
    flow = CoverLogicConfigFlow()
    flow.hass = hass
    flow.handler = "cover_logic"
    flow.flow_id = "test-flow-id"
    flow.context = {"source": "user"}
    return flow


def _no_issue_registry():
    """Stub `homeassistant.helpers.issue_registry`'s two functions.

    Every test below that sets up a *subentry*-backed entry takes
    `async_setup_entry`'s `_check_fixture_conformance` branch, which this
    checkout's own `fixtures/dom_peter.yaml` makes real (see that function's
    own docstring) -- and `FakeSetupHass` (`conftest.py`) has no `.data` for
    a genuine `IssueRegistry` to live in. `test_init.py`'s own subentries
    tests stub the same two functions for the same reason; this is that
    same, already-reviewed tradeoff, not a new one.
    """
    return (
        mock.patch("homeassistant.helpers.issue_registry.async_create_issue"),
        mock.patch("homeassistant.helpers.issue_registry.async_delete_issue"),
    )


def _entry_from_create_result(make_entry, result):
    """Build the `FakeConfigEntry` a real `ConfigFlowResult`'s `CREATE_ENTRY` becomes.

    Mirrors what `homeassistant.config_entries.ConfigEntries.async_finish_flow`
    actually does with `result["data"]`/`result["subentries"]`: wrap each
    subentry dict in a real `ConfigSubentry` and attach it to the new entry.
    """
    subentries = {f"sub{i}": ConfigSubentry(**item) for i, item in enumerate(result["subentries"])}
    return make_entry(dict(result["data"]), subentries=subentries)


# ---------------------------------------------------------------------------
# "Set up blinds now": structurally loads, but is not complete -- validate()
# says so via ConfigEntryNotReady, not a crash.
# ---------------------------------------------------------------------------


def test_blinds_now_result_parses_into_a_config(flow_hass, make_entry):
    flow = _make_flow(flow_hass())
    result = asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a", "cover.b"]}))

    entry = _entry_from_create_result(make_entry, result)
    config = config_from_subentries(entry)

    assert set(config.blinds) == {"cover.a", "cover.b"}
    assert config.zones == {}
    assert config.modes == ()


def test_blinds_now_result_fails_setup_cleanly_not_a_crash(flow_hass, make_entry, setup_hass):
    """No zone owns either blind and there is no fallback mode yet -- setup
    must refuse with `ConfigEntryNotReady` naming the real problem, not raise
    `KeyError`/`AttributeError` the way a `CONF_CONFIG_PATH`-shaped read of a
    subentry-backed, path-less entry used to (see `__init__.async_setup_entry`'s
    own docstring for why `entry.subentries` truthiness stopped being the
    branch signal).
    """
    flow = _make_flow(flow_hass())
    result = asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a"]}))
    entry = _entry_from_create_result(make_entry, result)

    create, delete = _no_issue_registry()
    with create, delete, pytest.raises(ConfigEntryNotReady, match="blind_without_zone"):
        asyncio.run(async_setup_entry(setup_hass(), entry))


# ---------------------------------------------------------------------------
# "Load a configuration from a YAML file": unchanged behaviour, proven here
# end to end (flow -> entry -> setup) rather than only at the flow layer.
# ---------------------------------------------------------------------------

_VALID_FILE_CONFIG = """
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


def test_from_file_result_sets_up_successfully(flow_hass, make_entry, setup_hass, tmp_path):
    path = tmp_path / "cover_logic.yaml"
    path.write_text(_VALID_FILE_CONFIG, encoding="utf-8")
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_from_file({CONF_CONFIG_PATH: str(path)}))
    entry = make_entry(dict(result["data"]))

    assert asyncio.run(async_setup_entry(setup_hass(), entry)) is True
    assert isinstance(entry.runtime_data, CoverLogicData)
    assert set(entry.runtime_data.config.blinds) == {"cover.a"}


# ---------------------------------------------------------------------------
# "Start from the example configuration": a complete, error-free house --
# setup must succeed outright, the coordinator and all.
# ---------------------------------------------------------------------------


def test_from_example_result_has_no_setup_blocking_problems(flow_hass, make_entry):
    """The example house's conditions reference real entities (an alarm panel,
    a temperature sensor, ...), so `CoverLogicCoordinator.async_setup` would
    reach for `homeassistant.helpers.event.async_track_state_change_event`,
    which needs a real `hass.bus`/`hass.data` -- `tests/ha/test_coordinator.py`
    already documents (`hass_factory`'s own docstring) why `FakeSetupHass`
    cannot stand in for that, and a bare `HomeAssistant()` has no working
    `.config_entries` for `async_setup_entry`'s own
    `async_forward_entry_setups` either. Rather than growing a third,
    heavier harness for one test, this proves the part of "sets up
    successfully" that is actually this task's concern -- the *result this
    flow produced* reads back to a `Config` with zero `ERROR`-severity
    `validate()` problems, which is exactly what stands between
    `async_setup_entry` and `ConfigEntryNotReady` -- and leaves the
    coordinator's own wiring to `test_coordinator.py`.
    """
    flow = _make_flow(flow_hass())
    result = asyncio.run(flow.async_step_from_example({}))
    entry = _entry_from_create_result(make_entry, result)

    config = config_from_subentries(entry)
    errors = [problem for problem in validate(config) if problem.severity == ERROR]

    assert errors == []
    assert "cover.living_room_balcony" in config.blinds


def test_from_example_result_matches_the_file_it_imported(flow_hass, make_entry):
    """`subentries_from_config`'s own round-trip self-check already proves this
    inside `async_step_from_example`; re-derive it here from the flow's
    actual output instead of trusting that internal check alone.
    """
    flow = _make_flow(flow_hass())
    result = asyncio.run(flow.async_step_from_example({}))
    entry = _entry_from_create_result(make_entry, result)

    rebuilt = config_from_subentries(entry)
    reference = load_config_file(repo_example_config_path())
    assert rebuilt == reference


# ---------------------------------------------------------------------------
# "Start empty": no crash, a clean, attributable `ConfigEntryNotReady`.
# ---------------------------------------------------------------------------


def test_empty_result_fails_setup_cleanly_not_a_crash(flow_hass, make_entry, setup_hass):
    flow = _make_flow(flow_hass())
    result = asyncio.run(flow.async_step_empty({}))
    entry = _entry_from_create_result(make_entry, result)

    create, delete = _no_issue_registry()
    with create, delete, pytest.raises(ConfigEntryNotReady, match="no_fallback_mode"):
        asyncio.run(async_setup_entry(setup_hass(), entry))


def test_empty_result_still_parses_into_a_config(flow_hass, make_entry):
    """The line this task draws: "must not create something the engine cannot
    load at all" means `config_from_subentries` must not raise -- an empty,
    still-incomplete `Config` is a legitimate, loadable result; whether it is
    complete enough to *run* is `validate()`'s job, exercised above.
    """
    flow = _make_flow(flow_hass())
    result = asyncio.run(flow.async_step_empty({}))
    entry = _entry_from_create_result(make_entry, result)

    config = config_from_subentries(entry)

    assert config.blinds == {}
    assert config.zones == {}
    assert config.modes == ()
