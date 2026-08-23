from __future__ import annotations

import datetime as dt

import pytest

from cover_logic.conditions import evaluate_condition
from cover_logic.model import Blind, Zone
from cover_logic.world import Event, Target, World

NOW = dt.datetime(2026, 8, 19, 13, 0)


def world(states=None, attributes=None, now=NOW, event=None) -> World:
    return World(
        states=states or {},
        attributes=attributes or {},
        now=now,
        event=event or Event(),
    )


def target(azimuth: float | None = 90.0, occupants=()) -> Target:
    return Target(
        blind=Blind(entity="cover.a", facade_azimuth=azimuth),
        zone=Zone(id="z", members=("cover.a",), occupants=tuple(occupants)),
    )


def test_none_means_otherwise():
    assert evaluate_condition(None, world()) is True


def test_list_is_and():
    w = world({"a": "on", "b": "off"})
    yes = [
        {"condition": "state", "entity_id": "a", "state": "on"},
        {"condition": "state", "entity_id": "b", "state": "off"},
    ]
    no = [
        {"condition": "state", "entity_id": "a", "state": "on"},
        {"condition": "state", "entity_id": "b", "state": "on"},
    ]
    assert evaluate_condition(yes, w) is True
    assert evaluate_condition(no, w) is False


def test_state_accepts_a_list_of_acceptable_states():
    w = world({"weather.x": "cloudy"})
    cond = {"condition": "state", "entity_id": "weather.x",
            "state": ["cloudy", "rainy"]}
    assert evaluate_condition(cond, w) is True


def test_state_of_a_missing_entity_is_false_not_an_error():
    cond = {"condition": "state", "entity_id": "nope", "state": "on"}
    assert evaluate_condition(cond, world()) is False


def test_numeric_state_uses_default_when_the_sensor_is_dead():
    # Mirrors `| float(999)` — a missing wind sensor must read as a gale,
    # never as calm.
    w = world({"sensor.wind": "unavailable"})
    cond = {"condition": "numeric_state", "entity_id": "sensor.wind",
            "below": 40, "default": 999}
    assert evaluate_condition(cond, w) is False


def test_numeric_state_reads_an_attribute():
    w = world({"weather.f": "sunny"}, {("weather.f", "wind_speed"): 21.0})
    cond = {"condition": "numeric_state", "entity_id": "weather.f",
            "attribute": "wind_speed", "below": 30, "default": 999}
    assert evaluate_condition(cond, w) is True


def test_time_after_is_inclusive():
    cond = {"condition": "time", "after": "13:00"}
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 13, 0))) is True
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 12, 59))) is False


def test_and_or_not():
    w = world({"a": "on"})
    on = {"condition": "state", "entity_id": "a", "state": "on"}
    off = {"condition": "state", "entity_id": "a", "state": "off"}
    assert evaluate_condition({"condition": "or", "conditions": [on, off]}, w) is True
    assert evaluate_condition({"condition": "and", "conditions": [on, off]}, w) is False
    assert evaluate_condition({"condition": "not", "conditions": [off]}, w) is True


def test_ref_resolves_through_the_registry():
    reg = {"vecer": {"condition": "state", "entity_id": "a", "state": "on"}}
    w = world({"a": "on"})
    assert evaluate_condition({"condition": "ref", "name": "vecer"}, w, registry=reg) is True


def test_ref_to_a_missing_name_raises():
    with pytest.raises(KeyError):
        evaluate_condition({"condition": "ref", "name": "nope"}, world(), registry={})


@pytest.mark.parametrize(
    ("azimuth", "expected"),
    [
        (-1.0, False),
        (44.0, False),
        (45.0, True),    # lower bound is inclusive
        (134.0, True),
        (135.0, False),  # upper bound is EXCLUSIVE — this is the parity trap
        (224.0, False),
        (315.0, False),
    ],
)
def test_sun_hits_target_uses_a_half_open_sector(azimuth, expected):
    w = world({"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": str(azimuth)})
    cond = {"condition": "sun_hits_target"}
    assert evaluate_condition(cond, w, target(azimuth=90.0)) is expected


def test_sun_hits_target_is_false_below_the_horizon():
    w = world({"sun.sun": "below_horizon", "sensor.sun_solar_azimuth": "90"})
    assert evaluate_condition({"condition": "sun_hits_target"}, w, target()) is False


def test_sun_hits_target_is_false_without_a_facade():
    w = world({"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": "90"})
    assert evaluate_condition({"condition": "sun_hits_target"}, w, target(azimuth=None)) is False


def test_sun_sector_wraps_around_north():
    w = world({"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": "350"})
    assert evaluate_condition({"condition": "sun_hits_target"}, w, target(azimuth=0.0)) is True


def test_event_targets_zone():
    w = world(event=Event(kind="arrival", person="peter"))
    assert evaluate_condition({"condition": "event_targets_zone"}, w,
                              target(occupants=["peter"])) is True
    assert evaluate_condition({"condition": "event_targets_zone"}, w,
                              target(occupants=["mimka"])) is False


def test_template_condition_sees_the_same_globals_as_home_assistant():
    w = world({"input_boolean.x": "on"})
    cond = {"condition": "template",
            "value_template": "{{ is_state('input_boolean.x', 'on') }}"}
    assert evaluate_condition(cond, w) is True


def test_unknown_condition_type_raises():
    with pytest.raises(ValueError, match="unknown condition"):
        evaluate_condition({"condition": "wibble"}, world())
