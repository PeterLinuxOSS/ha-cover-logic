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

from .conftest import FakeStateMachine

# This checkout's root, so the blocking-I/O guard below can ignore reads that
# are not ours -- Home Assistant does plenty of its own on the loop.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _is_project_file(path: object) -> bool:
    """True when `path` names a file inside this checkout, else False.

    Deliberately total: `builtins.open` is patched process-wide while the
    guard is armed, and it accepts file descriptors and bytes as well as
    paths, none of which `Path` can take. Anything unrecognisable is somebody
    else's I/O by definition, so it answers False rather than raising.
    """
    try:
        return Path(path).resolve().is_relative_to(_PROJECT_ROOT)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return False


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


def test_check_fixture_conformance_clears_the_issue_when_config_matches_the_real_fixture(
    setup_hass,
):
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
    a test about the conformance check alone, which touches `hass` only for
    its executor and to pass it through to the two mocked `issue_registry`
    calls below.
    """
    from cover_logic import _check_fixture_conformance  # noqa: PLC0415
    from cover_logic.config_schema import load_config_file  # noqa: PLC0415
    from cover_logic.conformance import repo_fixture_path  # noqa: PLC0415

    fixture = repo_fixture_path()
    assert fixture is not None, "this test suite must run from inside the project checkout"
    config = load_config_file(fixture)
    hass = setup_hass()

    with (
        mock.patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
        mock.patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        asyncio.run(_check_fixture_conformance(hass, config))

    create.assert_not_called()
    delete.assert_called_once_with(hass, "cover_logic", "fixture_drift")


def test_setup_from_subentries_does_no_blocking_file_io_on_the_event_loop(make_entry, setup_hass):
    """The subentry branch's own file I/O must go through the executor too.

    `_check_fixture_conformance` used to call `repo_fixture_path` (a stat) and
    `load_config_file` (a read) straight from `async_setup_entry`, and the
    real Home Assistant logged it on every start: two `homeassistant.util.loop`
    "Detected blocking call" warnings for `read_text` and for the `open`
    underneath it, both naming `fixtures/dom_peter.yaml`.

    Guards the calling thread rather than a call count: `Path.read_text`,
    `builtins.open` and `Path.is_file` raise if they are reached from the
    thread that awaits the setup, and are the real functions everywhere else.
    Scoped to this project's own checkout by path, so an unrelated read Home
    Assistant's own internals may do on the loop cannot make this fail (or
    pass) for reasons that have nothing to do with `cover_logic`.
    """
    entry = make_entry({}, subentries=_subentries_for(VALID_CONFIG))
    hass = setup_hass()
    loop_thread_ident = threading.get_ident()
    offences = []

    def guard(name, original, path_of):
        def wrapper(*args, **kwargs):
            if threading.get_ident() == loop_thread_ident and _is_project_file(path_of(*args)):
                offences.append(f"{name}({path_of(*args)})")
            return original(*args, **kwargs)

        return wrapper

    async def run():
        with (
            mock.patch.object(
                Path, "read_text", guard("Path.read_text", Path.read_text, lambda self: self)
            ),
            mock.patch.object(
                Path, "is_file", guard("Path.is_file", Path.is_file, lambda self: self)
            ),
            mock.patch("builtins.open", guard("open", open, lambda file, *_rest: file)),
        ):
            await async_setup_entry(hass, entry)

    with (
        mock.patch("homeassistant.helpers.issue_registry.async_create_issue"),
        mock.patch("homeassistant.helpers.issue_registry.async_delete_issue"),
    ):
        asyncio.run(run())

    assert offences == []


# `_check_referenced_entities`: the one question a fresh install needs
# answered. Both tests call it directly, for the reason
# `test_check_fixture_conformance_clears_the_issue_...` gives -- the check
# touches `hass` only for `.states` and to pass through to `issue_registry`.
_NAMES_TWO_ENTITIES = """
blinds:
  - entity: cover.a
zones:
  z:
    members: [cover.a]
conditions:
  dvere:
    condition: state
    entity_id: binary_sensor.dvere
    state: "on"
modes:
  - {id: any}
rules:
  any.z:
    - {if: !ref dvere, then: {position: 0, tilt: 0}}
    - {then: {position: keep, tilt: keep}}
"""


def _run_entity_check(hass, config, *, registered=()):
    """Run the check with `registered` as the entity registry's contents."""
    registry = mock.Mock()
    registry.async_get = lambda entity: mock.Mock() if entity in registered else None
    with (
        mock.patch("homeassistant.helpers.entity_registry.async_get", return_value=registry),
        mock.patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
        mock.patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        from cover_logic import _check_referenced_entities  # noqa: PLC0415

        asyncio.run(_check_referenced_entities(hass, config))
    return create, delete


def test_an_entity_this_house_does_not_have_raises_a_repair_issue(setup_hass):
    """Nothing in the state machine and nothing in the registry: both names reported.

    The blind is the important half. `_entity_ids` deliberately does *not*
    include blinds -- subscribing to them has a cost -- so a `cover.` that
    does not exist would otherwise be the one thing this check missed, while
    being the one thing that makes a rule permanently uncarryable.
    """
    create, delete = _run_entity_check(setup_hass(), load_config(_NAMES_TWO_ENTITIES))

    delete.assert_not_called()
    assert create.call_args.kwargs["translation_key"] == "unknown_entities"
    placeholders = create.call_args.kwargs["translation_placeholders"]
    assert placeholders["count"] == "2"
    assert "cover.a" in placeholders["entities"]
    assert "binary_sensor.dvere" in placeholders["entities"]


def test_known_in_either_the_states_or_the_registry_is_known_enough(setup_hass):
    """One entity present only as state, one present only in the registry: no issue.

    The counter to checking just one of them, and neither direction is
    hypothetical: a YAML template sensor without a `unique_id` is never in the
    registry, and an entity whose integration has not been set up yet is not
    in the state machine. Requiring both would report entities this house
    does have -- which is worse than not checking, because a repair issue
    nobody can act on is one everybody learns to dismiss.
    """
    hass = setup_hass()
    hass.states = FakeStateMachine({"binary_sensor.dvere": mock.Mock()})
    create, delete = _run_entity_check(
        hass, load_config(_NAMES_TWO_ENTITIES), registered=("cover.a",)
    )

    create.assert_not_called()
    delete.assert_called_once_with(hass, "cover_logic", "unknown_entities")


# `_ConfigReloader`: writing the configuration is not deploying it. Four tests,
# because the decision has three wrong answers -- never reload, always reload,
# and reload once per subentry write.
def _reloader(hass, entry):
    from cover_logic import _ConfigReloader  # noqa: PLC0415

    return _ConfigReloader(hass, entry)


class _RuntimeData:
    """Just the `.config` attribute the listener compares against."""

    def __init__(self, config):
        self.config = config


def _armed(hass, entry):
    """Run the listener with `_arm` recorded rather than performed."""
    reloader = _reloader(hass, entry)
    with mock.patch.object(reloader, "_arm") as arm:
        asyncio.run(reloader.async_entry_updated(hass, entry))
    return arm.called


def test_a_changed_configuration_arms_a_reload(make_entry, setup_hass):
    """The subentries now spell something other than what is running: reload.

    Nothing in this integration reloaded before, and Home Assistant does not
    do it either -- `ConfigSubentryFlowManager.async_finish_flow` calls
    `async_add_subentry` and returns. So editing a rule in the UI changed
    `.storage` and left the house running the rules it replaced, until a
    restart. Measured live on 2026-09-02 through the import path, which has
    the identical cause.
    """
    entry = make_entry({}, subentries=_subentries_for(VALID_CONFIG))
    entry.runtime_data = _RuntimeData(load_config(WARNING_ONLY_CONFIG))

    assert _armed(setup_hass(), entry) is True


def test_an_option_write_arms_nothing(make_entry, setup_hass):
    """The counter to reloading on every entry write, and it protects a design decision.

    `dry_run` lives in `entry.options` precisely so flipping it reaches
    `runner.py` without a reload (see `options_flow.py`). The listener fires
    for that write too, so "did the configuration change" has to be asked
    rather than assumed.
    """
    config = load_config(VALID_CONFIG)
    entry = make_entry({}, subentries=_subentries_for_config(config), options={"dry_run": True})
    entry.runtime_data = _RuntimeData(config)

    assert _armed(setup_hass(), entry) is False


def test_a_file_backed_entry_arms_nothing(make_entry, setup_hass):
    """A legacy entry reads a file, so comparing its subentries would be meaningless."""
    entry = make_entry({CONF_CONFIG_PATH: "/nowhere.yaml"})
    entry.runtime_data = _RuntimeData(load_config(VALID_CONFIG))

    assert _armed(setup_hass(), entry) is False


def test_a_burst_of_writes_produces_exactly_one_reload(make_entry, setup_hass, monkeypatch):
    """Three writes in a row, one reload -- the shape an import needs.

    `async_schedule_reload` is a task per call, not a debounce, and an import
    rewrites every subentry one at a time (141 of them in the owner's house).
    Without the restart-on-change wait this would ask for hundreds of
    sequential reloads.
    """
    monkeypatch.setattr("cover_logic.CONFIG_RELOAD_SETTLE_SECONDS", 0.05)
    entry = make_entry({}, subentries=_subentries_for(VALID_CONFIG))
    entry.runtime_data = _RuntimeData(load_config(WARNING_ONLY_CONFIG))
    hass = setup_hass()
    reloader = _reloader(hass, entry)

    async def run():
        for _ in range(3):
            await reloader.async_entry_updated(hass, entry)
        await asyncio.sleep(0.2)

    asyncio.run(run())

    assert hass.config_entries.scheduled_reloads == [entry.entry_id]
