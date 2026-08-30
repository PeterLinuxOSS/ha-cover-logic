"""Tests for `sensor.CoverLogicModeSensor`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv (`.venv/bin/python -m pytest`) -- see `test_ha_world.py`'s own note.

Properties are read directly off a bare `CoverLogicModeSensor`, the same way
`test_ha_world.py` calls `build_world` directly -- no live entity platform,
no `entity_id`, is needed to exercise `native_value`/`extra_state_attributes`.
`FakeCoordinator` stands in for `CoverLogicCoordinator`: only `.decision`,
`.last_error`, `.last_success` and `.add_listener` are read, the same
FakeHass-style tradeoff `tests/ha/conftest.py` already makes throughout this
suite.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import State
from homeassistant.helpers.entity import EntityCategory

from cover_logic.engine import Decision
from cover_logic.model import KEEP, Action
from cover_logic.sensor import (
    LEGACY_MATRIX_ENTITY,
    LEGACY_TEPLOTNA_OCHRANA_ENTITY,
    CoverLogicModeSensor,
    async_setup_entry,
)


class FakeCoordinator:
    """Stands in for `CoverLogicCoordinator`: only what the sensor reads/calls.

    `add_listener` mirrors the real signature (register, return an unsub) so
    `test_added_to_hass_subscribes_and_removal_unsubscribes` can exercise the
    entity's actual lifecycle methods against it, without pulling in the real
    coordinator's event-loop/debounce machinery (already covered on its own
    in `test_coordinator.py`).

    `dry_run`/`pending`/`last_command` are the executor-facing three the sensor
    gained in task 4. They are plain attributes here on purpose: the real ones
    are read-only properties over a `CommandLog`, a `DeferralRegistry` and a
    `CoverRunner`, and a fake that rebuilt those three would be testing them
    rather than the sensor. That the real coordinator actually *fills* them is
    `tests/ha/test_wiring.py`'s job, driven end to end -- exactly the split
    that stops this file from passing while the layer beneath it is broken.
    """

    def __init__(
        self,
        decision=None,
        last_error=None,
        last_success=None,
        *,
        dry_run=True,
        pending=None,
        last_command=None,
    ):
        """Store the six attributes `CoverLogicModeSensor` reads."""
        self.decision = decision
        self.last_error = last_error
        self.last_success = last_success
        self.dry_run = dry_run
        self.pending = pending if pending is not None else {"deferred": {}, "queued": {}}
        self.last_command = last_command
        self.listeners = []

    def add_listener(self, listener):
        """Register `listener`; return an unsub that removes it."""
        self.listeners.append(listener)

        def _remove():
            self.listeners.remove(listener)

        return _remove


DECISION = Decision(
    mode="bezny_den",
    targets={
        "cover.a": Action(50, 30),
        "cover.b": Action(KEEP, KEEP),
    },
    trace={
        "cover.a": "bezny_den.z#0 rule-a",
        "cover.b": "bezny_den.z#none",
    },
)


def _sensor(decision=DECISION, *, last_error=None, last_success=None, hass=None, entry_id="entry1"):
    sensor = CoverLogicModeSensor(FakeCoordinator(decision, last_error, last_success), entry_id)
    sensor.hass = hass
    return sensor


def _matica_state(mode, ciele):
    return State(LEGACY_MATRIX_ENTITY, mode, {"ciele": json.dumps(ciele)})


# --- state, targets, trace, KEEP serialisation ------------------------------


def test_state_is_the_mode():
    assert _sensor().state == "bezny_den"


def test_unavailable_before_first_evaluation():
    sensor = _sensor(decision=None)
    assert sensor.available is False
    assert sensor.state is None


def test_entity_category_is_diagnostic():
    assert _sensor().entity_category == EntityCategory.DIAGNOSTIC


def test_unique_id_is_derived_from_the_config_entry():
    assert _sensor(entry_id="myentry").unique_id == "myentry_mode"


def test_targets_and_trace_are_exposed():
    attrs = _sensor().extra_state_attributes
    assert attrs["targets"]["cover.a"] == {"position": 50, "tilt": 30}
    assert attrs["trace"] == dict(DECISION.trace)


def test_keep_renders_as_the_string_keep():
    attrs = _sensor().extra_state_attributes
    assert attrs["targets"]["cover.b"] == {"position": "keep", "tilt": "keep"}


def test_targets_and_trace_are_empty_before_first_evaluation():
    attrs = _sensor(decision=None).extra_state_attributes
    assert attrs["targets"] == {}
    assert attrs["trace"] == {}


def test_last_error_and_last_success_are_exposed():
    class FrozenNow:
        def isoformat(self):
            return "2026-08-25T12:00:00+00:00"

    attrs = _sensor(last_error="EngineError: boom", last_success=FrozenNow()).extra_state_attributes
    assert attrs["last_error"] == "EngineError: boom"
    assert attrs["last_success"] == "2026-08-25T12:00:00+00:00"


def test_last_success_is_none_before_first_evaluation():
    assert _sensor(last_success=None).extra_state_attributes["last_success"] is None


# --- matica_diff: the point of this task ------------------------------------


def test_matica_diff_is_empty_list_when_they_agree(fake_hass):
    """`[]` -- checked, and the engine matches the old matrix entity by entity."""
    ciele = {
        "cover.a": {"akcia": "pozicia", "hodnota": 50, "tilt": 30},
        "cover.b": {"akcia": "nechat", "hodnota": None, "tilt": None},
    }
    hass = fake_hass({LEGACY_MATRIX_ENTITY: _matica_state("bezny_den", ciele)})

    attrs = _sensor(hass=hass).extra_state_attributes

    assert attrs["matica_mode"] == "bezny_den"
    assert attrs["matica_diff"] == []


def test_matica_diff_names_exactly_the_differing_entities(fake_hass):
    """A real, constructed disagreement -- not an empty-list assertion in disguise.

    `cover.a`'s old target (hodnota=60) disagrees with the engine's
    `Action(50, 30)`; `cover.b` still agrees (`nechat` -> KEEP/KEEP). Only
    `cover.a` may show up.
    """
    ciele = {
        "cover.a": {"akcia": "pozicia", "hodnota": 60, "tilt": 30},
        "cover.b": {"akcia": "nechat", "hodnota": None, "tilt": None},
    }
    hass = fake_hass({LEGACY_MATRIX_ENTITY: _matica_state("bezny_den", ciele)})

    attrs = _sensor(hass=hass).extra_state_attributes

    assert attrs["matica_diff"] == ["cover.a"]


def test_matica_diff_is_none_when_matrix_entity_is_absent(fake_hass):
    """`None`, never `[]` -- there is nothing to compare against, not "checked, they agree"."""
    hass = fake_hass({})

    attrs = _sensor(hass=hass).extra_state_attributes

    assert attrs["matica_mode"] is None
    assert attrs["matica_diff"] is None


def test_matica_diff_is_none_when_ciele_attribute_is_malformed(fake_hass):
    """A malformed `ciele` must not crash the sensor -- degrade to `None`."""
    state = State(LEGACY_MATRIX_ENTITY, "bezny_den", {"ciele": "{not valid json"})
    hass = fake_hass({LEGACY_MATRIX_ENTITY: state})

    attrs = _sensor(hass=hass).extra_state_attributes  # must not raise

    assert attrs["matica_mode"] == "bezny_den"
    assert attrs["matica_diff"] is None


def test_matica_diff_is_none_when_ciele_is_not_a_json_object(fake_hass):
    """Valid JSON, wrong shape (a list, not an object) -- still must not crash."""
    state = State(LEGACY_MATRIX_ENTITY, "bezny_den", {"ciele": json.dumps([1, 2, 3])})
    hass = fake_hass({LEGACY_MATRIX_ENTITY: state})

    attrs = _sensor(hass=hass).extra_state_attributes

    assert attrs["matica_diff"] is None


def test_matica_diff_is_none_when_ciele_attribute_is_missing(fake_hass):
    hass = fake_hass({LEGACY_MATRIX_ENTITY: State(LEGACY_MATRIX_ENTITY, "bezny_den", {})})

    attrs = _sensor(hass=hass).extra_state_attributes

    assert attrs["matica_mode"] == "bezny_den"
    assert attrs["matica_diff"] is None


def test_matica_diff_is_none_before_first_evaluation(fake_hass):
    """The old matrix may be readable while this engine has nothing yet -- still `None`."""
    ciele = {"cover.a": {"akcia": "nechat", "hodnota": None, "tilt": None}}
    hass = fake_hass({LEGACY_MATRIX_ENTITY: _matica_state("bezny_den", ciele)})

    attrs = _sensor(decision=None, hass=hass).extra_state_attributes

    assert attrs["matica_diff"] is None


def test_matica_diff_respects_the_teplotna_ochrana_fallback_for_missing_tilt(fake_hass):
    """`legacy.to_action`'s subtlety must survive the move: a `pozicia` item with
    `tilt: null` resolves to 50 when `teplotna_ochrana_dom` is on, 100 when off
    -- never a fixed 100. This is exactly the case CLAUDE.md calls out.
    """
    decision = Decision(
        mode="bezny_den",
        targets={"cover.kvety": Action(40, 50)},
        trace={"cover.kvety": "bezny_den.kvety#0"},
    )
    ciele = {"cover.kvety": {"akcia": "pozicia", "hodnota": 40, "tilt": None}}

    hass_on = fake_hass(
        {
            LEGACY_MATRIX_ENTITY: _matica_state("bezny_den", ciele),
            LEGACY_TEPLOTNA_OCHRANA_ENTITY: State(LEGACY_TEPLOTNA_OCHRANA_ENTITY, "on"),
        }
    )
    assert _sensor(decision=decision, hass=hass_on).extra_state_attributes["matica_diff"] == []

    hass_off = fake_hass(
        {
            LEGACY_MATRIX_ENTITY: _matica_state("bezny_den", ciele),
            LEGACY_TEPLOTNA_OCHRANA_ENTITY: State(LEGACY_TEPLOTNA_OCHRANA_ENTITY, "off"),
        }
    )
    assert _sensor(decision=decision, hass=hass_off).extra_state_attributes["matica_diff"] == [
        "cover.kvety"
    ]


# --- lifecycle: subscribe on add, unsubscribe on removal --------------------


def test_added_to_hass_subscribes_and_removal_unsubscribes():
    coordinator = FakeCoordinator(DECISION)
    sensor = CoverLogicModeSensor(coordinator, "entry1")

    asyncio.run(sensor.async_added_to_hass())
    assert len(coordinator.listeners) == 1

    asyncio.run(sensor.async_will_remove_from_hass())
    assert coordinator.listeners == []


# --- platform setup ----------------------------------------------------------


def test_async_setup_entry_adds_one_sensor_bound_to_the_coordinator():
    coordinator = FakeCoordinator(DECISION)
    runtime_data = SimpleNamespace(coordinator=coordinator)
    entry = SimpleNamespace(runtime_data=runtime_data, entry_id="entry1")
    added = []

    asyncio.run(async_setup_entry(None, entry, added.extend))

    assert len(added) == 1
    (added_sensor,) = added
    assert isinstance(added_sensor, CoverLogicModeSensor)
    assert added_sensor.unique_id == "entry1_mode"
    # Bound to the entry's own coordinator, not some other instance -- read
    # through the public `state` property rather than the private attribute.
    assert added_sensor.state == DECISION.mode
