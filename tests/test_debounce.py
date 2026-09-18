"""`for:` on a numeric threshold, and the memory it needs to mean anything."""

import datetime as dt

import pytest

from cover_logic.conditions import evaluate_condition
from cover_logic.debounce import crosses, is_debounced, resolve, threshold_key
from cover_logic.world import World

LUX = "sensor.lux"
NOW = dt.datetime(2026, 9, 18, 18, 30)
DUSK = {
    "condition": "numeric_state",
    "entity_id": LUX,
    "below": 2800,
    "for": 180,
    "default": 99999,
}
KEY = threshold_key(DUSK)


def world(value, *, now=NOW, since=None) -> World:
    return World(
        states={LUX: str(value)},
        now=now,
        numeric_since={} if since is None else since,
    )


def test_a_condition_without_for_is_not_debounced():
    assert not is_debounced({"condition": "numeric_state", "entity_id": LUX, "below": 2800})


def test_for_on_something_other_than_numeric_state_is_not_this_mechanism():
    # `condition: state` has its own `for:`, measured off `World.since`.
    assert not is_debounced({"condition": "state", "entity_id": LUX, "for": 180})


def test_two_identically_written_thresholds_share_one_memory():
    assert threshold_key(DUSK) == threshold_key(dict(DUSK))


def test_the_bounds_are_part_of_the_key():
    assert threshold_key(DUSK) != threshold_key({**DUSK, "below": 2900})


@pytest.mark.parametrize(("value", "expected"), [(2799, True), (2801, False)])
def test_with_no_memory_at_all_it_is_the_plain_threshold(value, expected):
    """Load-bearing: the pure tests and the migration gate evaluate exactly here.

    A `for:` that failed closed without memory would change every settled
    scenario's answer, which is the one thing this may not do.
    """
    assert evaluate_condition(DUSK, world(value)) is expected
    assert crosses(DUSK, float(value)) is expected


def test_the_moment_it_became_true_is_recorded():
    assert resolve([DUSK], world(2799), {}) == {KEY: NOW}


def test_a_value_back_over_the_threshold_forgets_the_moment():
    assert resolve([DUSK], world(2801), {KEY: NOW}) == {KEY: None}


def test_staying_true_does_not_restart_the_dwell():
    """The whole point: a dwell measures one unbroken stretch."""
    began = NOW - dt.timedelta(minutes=2)

    assert resolve([DUSK], world(2799, now=NOW), {KEY: began}) == {KEY: began}


def test_it_is_false_until_the_dwell_has_passed():
    began = NOW - dt.timedelta(seconds=179)

    assert evaluate_condition(DUSK, world(2799, since={KEY: began})) is False


def test_it_is_true_once_the_dwell_has_passed():
    began = NOW - dt.timedelta(seconds=180)

    assert evaluate_condition(DUSK, world(2799, since={KEY: began})) is True


def test_a_threshold_that_is_not_true_now_is_false_however_long_ago_it_held():
    assert evaluate_condition(DUSK, world(2801, since={KEY: None})) is False


def test_the_incident_this_exists_for():
    """2026-09-18: lux went 2788 -> 2813 -> 2742 in four minutes.

    Each crossing sent a terrace blind on a full 55-second run, so dusk closed
    the house, reopened it and closed it again while the sensor wobbled. With
    a dwell, the blip clears the memory and nothing moves until it settles.
    """
    memory: dict = {}
    moves = []
    for minute, value in ((28, 2788), (30, 2813), (32, 2742), (34, 2671), (36, 2610)):
        at = NOW.replace(minute=minute)
        snapshot = world(value, now=at, since=memory)
        moves.append(evaluate_condition(DUSK, snapshot))
        memory = resolve([DUSK], snapshot, memory)

    # The first reading has no memory yet, so it is the plain threshold; from
    # then on the bounce is absorbed and dusk arrives once, and stays.
    assert moves == [True, False, False, False, True]


def test_only_debounced_conditions_are_resolved():
    plain = {"condition": "numeric_state", "entity_id": LUX, "below": 2800, "default": 0}

    assert resolve([plain], world(1), {}) == {}


def test_an_unreadable_sensor_falls_to_the_stated_default():
    unreadable = World(states={}, now=NOW, numeric_since={KEY: NOW})

    assert resolve([DUSK], unreadable, {KEY: NOW}) == {KEY: None}
