"""Invariants that must hold no matter what the configuration says."""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings, strategies as st
import pytest

from cover_logic.config_schema import load_config_file
from cover_logic.engine import evaluate
from cover_logic.model import KEEP
from cover_logic.world import Event, World

NOW = dt.datetime(2026, 8, 19, 13, 0)

ENTITY_VALUES = ["on", "off", "unknown", "unavailable", "armed_vacation",
                 "triggered", "above_horizon", "below_horizon", "sunny",
                 "cloudy", "34", "180", "999"]


@pytest.fixture(scope="module")
def config(fixtures_dir):
    return load_config_file(fixtures_dir / "dom_peter.yaml")


@given(
    values=st.dictionaries(
        keys=st.sampled_from([
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
        ]),
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
        keys=st.sampled_from(["input_boolean.cover_down", "sun.sun",
                              "sensor.sun_solar_azimuth", "binary_sensor.is_home"]),
        values=st.sampled_from(ENTITY_VALUES),
    )
)
@settings(max_examples=200, deadline=None)
def test_the_same_world_always_gives_the_same_decision(config, values):
    world = World(states=values, attributes={}, now=NOW, event=Event())
    assert evaluate(config, world) == evaluate(config, world)


@given(
    values=st.dictionaries(
        keys=st.sampled_from(["input_boolean.cover_down", "sun.sun",
                              "sensor.sun_solar_azimuth",
                              "input_number.kvety_pozicia_zaluzie"]),
        values=st.sampled_from(ENTITY_VALUES),
    )
)
@settings(max_examples=200, deadline=None)
def test_action_axes_are_always_an_int_or_keep(config, values):
    # The engine deliberately does not clamp axis values to 0..100: the Jinja
    # template it replaces does not clamp either, so clamping here would break
    # parity with it. Clamping is the execution layer's job, later. The only
    # honest guarantee at this layer is "KEEP or an int" -- with
    # `input_number.kvety_pozicia_zaluzie` (the fixture's one `Ref`) in the
    # strategy, a helper value like 999 really does flow straight through as
    # `Action(position=999, ...)`.
    world = World(states=values, attributes={}, now=NOW, event=Event())
    for action in evaluate(config, world).targets.values():
        for axis in (action.position, action.tilt):
            assert axis is KEEP or isinstance(axis, int)


def test_night_mode_never_moves_anything(config):
    world = World(
        states={"input_boolean.cover_down": "on"},
        attributes={}, now=NOW, event=Event(),
    )
    for action in evaluate(config, world).targets.values():
        assert action.position is KEEP
        assert action.tilt is KEEP
