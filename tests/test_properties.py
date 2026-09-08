"""Invariants that must hold no matter what the configuration says."""

import datetime as dt

from hypothesis import given, settings, strategies as st
import pytest

from cover_logic.config_schema import load_config_file
from cover_logic.engine import evaluate
from cover_logic.model import KEEP
from cover_logic.world import Event, World

NOW = dt.datetime(2026, 8, 19, 13, 0)

ENTITY_VALUES = [
    "on",
    "off",
    "unknown",
    "unavailable",
    "armed_vacation",
    "triggered",
    "above_horizon",
    "below_horizon",
    "sunny",
    "cloudy",
    "34",
    "180",
    "999",
]


@pytest.fixture(scope="module")
def config(fixtures_dir):
    return load_config_file(fixtures_dir / "dom_peter.yaml")


@given(
    values=st.dictionaries(
        keys=st.sampled_from(
            [
                "input_boolean.cover_down",
                "alarm_control_panel.alarmo",
                "input_boolean.teplotna_ochrana_dom",
                "input_boolean.lighting_on",
                "input_boolean.kvety_on",
                "input_boolean.zaluzie_kuchyna_rucne",
                "binary_sensor.is_home",
                "input_boolean.some_sleeping",
                "sun.sun",
                "sensor.sun_solar_azimuth",
                "weather.openweathermap",
            ]
        ),
        values=st.sampled_from(ENTITY_VALUES),
        max_size=11,
    ),
    arrival=st.booleans(),
)
@settings(max_examples=400, deadline=None)
def test_every_blind_always_gets_exactly_one_action(config, values, arrival):
    event = Event(kind="arrival", person="peter") if arrival else Event()
    world = World(states=values, attributes={}, now=NOW, event=event)
    decision = evaluate(config, world)
    assert set(decision.targets) == set(config.blinds)
    assert set(decision.trace) == set(config.blinds)


@given(
    values=st.dictionaries(
        keys=st.sampled_from(
            [
                "input_boolean.cover_down",
                "sun.sun",
                "sensor.sun_solar_azimuth",
                "binary_sensor.is_home",
            ]
        ),
        values=st.sampled_from(ENTITY_VALUES),
    )
)
@settings(max_examples=200, deadline=None)
def test_equal_worlds_always_give_the_same_decision(config, values):
    """Two *separately built* worlds, not one object twice.

    Passing the same instance twice only proves `evaluate` keeps no internal
    memo -- true of any pure function and of plenty of impure ones. The
    property worth holding over the whole generated space is reproducibility
    across equal snapshots, which is what lets a decision be re-derived from a
    recorded state at all. `test_engine.py`'s
    `test_equal_but_separately_built_worlds_give_the_same_decision` states the
    same thing for one hand-built pair; this states it for two hundred
    generated ones.
    """
    first = World(states=dict(values), attributes={}, now=NOW, event=Event())
    second = World(states=dict(values), attributes={}, now=NOW, event=Event())
    assert first is not second
    assert evaluate(config, first) == evaluate(config, second)


@given(
    values=st.dictionaries(
        keys=st.sampled_from(
            [
                "input_boolean.cover_down",
                "sun.sun",
                "sensor.sun_solar_azimuth",
                "input_number.kvety_pozicia_zaluzie",
            ]
        ),
        values=st.sampled_from(ENTITY_VALUES),
    )
)
@settings(max_examples=200, deadline=None)
def test_action_axes_are_always_an_int_or_keep(config, values):
    # The only honest guarantee at this layer is "KEEP or an int" -- see
    # docs/rationale.md -- "Why the engine does not clamp resolved values to
    # 0..100". With `input_number.kvety_pozicia_zaluzie` (the fixture's one
    # `Ref`) in the strategy, a helper value like 999 really does flow
    # straight through as `Action(position=999, ...)`.
    world = World(states=values, attributes={}, now=NOW, event=Event())
    for action in evaluate(config, world).targets.values():
        for axis in (action.position, action.tilt):
            assert axis is KEEP or isinstance(axis, int)


def test_night_mode_closes_everything(config):
    """Phase 7.6: `noc` closes rather than keeps -- see the fixture's own
    `noc.*` comment for why, and `test_fixture_guards` for what keeps a blind
    off an open door now that `keep` no longer does it.
    """
    world = World(
        states={"input_boolean.cover_down": "on"},
        attributes={},
        now=NOW,
        event=Event(),
    )
    flowers = {"cover.kuchyna_zaluzia_1_4", "cover.kuchyna_zaluzia_2_5"}
    for entity, action in evaluate(config, world).targets.items():
        # Flower blinds keep their own height at night; every other blind goes
        # to the floor. Slats are shut on all of them.
        assert action.position == (34 if entity in flowers else 0), (entity, action)
        assert action.tilt == 0, (entity, action)
