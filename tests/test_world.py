import dataclasses
import datetime as dt

import pytest

from cover_logic.model import Blind, Zone
from cover_logic.world import Event, Target, World

NOW = dt.datetime(2026, 8, 19, 13, 0)


def make_world(**states) -> World:
    return World(states=states, attributes={}, now=NOW, event=Event())


def test_state_returns_none_for_unknown_entity():
    w = make_world(**{"input_boolean.a": "on"})
    assert w.state("input_boolean.a") == "on"
    assert w.state("input_boolean.missing") is None


def test_number_falls_back_to_default_when_unparsable():
    w = make_world(**{"sensor.wind": "unavailable", "sensor.ok": "12.5"})
    assert w.number("sensor.wind", default=999.0) == 999.0
    assert w.number("sensor.ok", default=999.0) == 12.5
    assert w.number("sensor.absent", default=7.0) == 7.0


def test_number_falls_back_for_unknown_state():
    w = make_world(**{"sensor.temp": "unknown"})
    assert w.number("sensor.temp", default=20.0) == 20.0


def test_number_falls_back_for_empty_string():
    w = make_world(**{"sensor.temp": ""})
    assert w.number("sensor.temp", default=20.0) == 20.0


def test_number_falls_back_for_none_state():
    w = World(
        states={"sensor.temp": None},  # type: ignore[arg-type]
        attributes={},
        now=NOW,
        event=Event(),
    )
    assert w.number("sensor.temp", default=20.0) == 20.0


def test_number_falls_back_for_non_numeric_attribute():
    w = World(
        states={"weather.forecast_home": "sunny"},
        attributes={("weather.forecast_home", "items"): [1, 2, 3]},
        now=NOW,
        event=Event(),
    )
    assert w.number("weather.forecast_home", default=999.0, attribute="items") == 999.0


def test_number_converts_boolean_without_fallback():
    """Current behaviour: float(True) succeeds and yields 1.0, not the default.

    This is documented as current behaviour, not necessarily desired. A future
    change to `number()` to reject booleans should update this test.
    """
    w = make_world(**{"sensor.bool": "True"})
    # float("True") raises ValueError, so this falls back
    assert w.number("sensor.bool", default=99.0) == 99.0
    # But if somehow we get a raw bool (edge case in attributes), float(True) = 1.0
    w_bool = World(
        states={},
        attributes={("sensor.bool", "raw"): True},
        now=NOW,
        event=Event(),
    )
    assert w_bool.number("sensor.bool", default=99.0, attribute="raw") == 1.0


def test_number_can_read_an_attribute():
    w = World(
        states={"weather.forecast_home": "sunny"},
        attributes={("weather.forecast_home", "wind_speed"): 21.0},
        now=NOW,
        event=Event(),
    )
    got = w.number("weather.forecast_home", default=999.0, attribute="wind_speed")
    assert got == 21.0


def test_world_is_frozen():
    w = make_world()
    with pytest.raises(dataclasses.FrozenInstanceError):
        w.now = NOW


def test_event_defaults_to_state_change():
    assert Event().kind == "state_change"
    assert Event().person is None


def test_target_pairs_a_blind_with_its_zone():
    t = Target(blind=Blind(entity="cover.a"), zone=Zone(id="z", members=("cover.a",)))
    assert t.blind.entity == "cover.a"
    assert t.zone.id == "z"


def test_world_states_snapshot_isolation():
    """World takes a defensive copy of states so caller mutation cannot affect it."""
    original_states = {"cover.a": "open"}
    w = World(states=original_states, attributes={}, now=NOW, event=Event())

    # Mutate the original dict
    original_states["cover.a"] = "closed"
    original_states["cover.b"] = "closed"

    # World should still report the pre-mutation value
    assert w.state("cover.a") == "open"
    assert w.state("cover.b") is None


def test_world_attributes_snapshot_isolation():
    """World takes a defensive copy of attributes so caller mutation cannot affect it."""
    original_attributes = {("weather.forecast", "wind_speed"): 15.0}
    w = World(states={}, attributes=original_attributes, now=NOW, event=Event())

    # Mutate the original dict
    original_attributes[("weather.forecast", "wind_speed")] = 25.0
    original_attributes[("weather.forecast", "temp")] = 20.0

    # World should still report the pre-mutation value
    assert w.attribute("weather.forecast", "wind_speed") == 15.0
    assert w.attribute("weather.forecast", "temp") is None
