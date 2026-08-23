from __future__ import annotations

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
