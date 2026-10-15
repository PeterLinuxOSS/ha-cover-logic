"""`for:` and `latch: daily` on a numeric threshold, and the memory they need."""

import datetime as dt

import pytest

from cover_logic.conditions import evaluate_condition
from cover_logic.debounce import Dwell, crosses, is_remembered, resolve, threshold_key
from cover_logic.world import World

LUX = "sensor.lux"
NOW = dt.datetime(2026, 9, 18, 18, 30)
DUSK = {
    "condition": "numeric_state",
    "entity_id": LUX,
    "below": 2800,
    "for": 180,
    "latch": "daily",
    "default": 99999,
}
DEBOUNCED = {k: v for k, v in DUSK.items() if k != "latch"}
KEY = threshold_key(DUSK)


def world(value, *, now=NOW, memory=None) -> World:
    return World(states={LUX: str(value)}, now=now, numeric_since=memory or {})


def replay(cond, samples, memory=None):
    """Answer each sample in order, in the order `build_world` really does it.

    Resolve the memory from the finished snapshot first, then evaluate against
    a world that carries it -- evaluating first would read the previous
    sample's answer, which is the bug this ordering exists to prevent.
    """
    memory = memory or {}
    answers = []
    for at, value in samples:
        memory = resolve([cond], world(value, now=at), memory)
        answers.append(evaluate_condition(cond, world(value, now=at, memory=memory)))
    return answers, memory


def test_a_plain_threshold_is_not_remembered():
    assert not is_remembered({"condition": "numeric_state", "entity_id": LUX, "below": 2800})


def test_these_keys_on_something_other_than_numeric_state_are_not_this_mechanism():
    # `condition: state` has its own `for:`, measured off `World.since`.
    assert not is_remembered({"condition": "state", "entity_id": LUX, "for": 180})


def test_two_identically_written_thresholds_share_one_memory():
    assert threshold_key(DUSK) == threshold_key(dict(DUSK))


def test_the_bounds_are_part_of_the_key():
    assert threshold_key(DUSK) != threshold_key({**DUSK, "below": 2900})


def test_the_fallback_is_part_of_the_key():
    """Two conditions that disagree about an unreadable sensor are not one answer."""
    assert threshold_key(DUSK) != threshold_key({**DUSK, "default": 0})


@pytest.mark.parametrize(("value", "expected"), [(2799, True), (2801, False)])
def test_with_no_memory_at_all_it_is_the_plain_threshold(value, expected):
    """Load-bearing: the pure tests and the migration gate evaluate exactly here.

    A `for:` that failed closed without memory would change every settled
    scenario's answer, which is the one thing this may not do.
    """
    assert evaluate_condition(DUSK, world(value)) is expected
    assert crosses(DUSK, float(value)) is expected


def test_it_is_false_until_the_dwell_has_passed():
    began = NOW - dt.timedelta(seconds=179)
    memory = {KEY: Dwell(since=began, answer=False)}

    assert evaluate_condition(DEBOUNCED, world(2799, memory=memory)) is False


def test_it_is_true_once_the_dwell_has_passed():
    began = NOW - dt.timedelta(seconds=180)

    answers, _ = replay(DEBOUNCED, [(began, 2799), (NOW, 2799)])

    assert answers[-1] is True


def test_staying_true_does_not_restart_the_dwell():
    """A dwell measures one unbroken stretch."""
    began = NOW - dt.timedelta(minutes=2)
    memory = {KEY: Dwell(since=began)}

    assert resolve([DEBOUNCED], world(2799), memory)[KEY].since == began


def test_a_value_back_over_the_threshold_forgets_the_moment():
    assert resolve([DEBOUNCED], world(2801), {KEY: Dwell(since=NOW)})[KEY].since is None


def test_the_incident_this_exists_for():
    """2026-09-18: lux read 2788, then 2813, then 2742, two minutes apart.

    Each crossing sent a terrace blind on a full 55-second run, so dusk closed
    the house, reopened it and closed it again while the sensor wobbled.
    """
    samples = [(NOW.replace(minute=m), v) for m, v in ((28, 2788), (30, 2813), (32, 2742))]
    samples += [(NOW.replace(minute=m), v) for m, v in ((34, 2671), (36, 2610))]

    answers, _ = replay(DEBOUNCED, samples)

    # Dusk arrives once, at 18:36, instead of three times in four minutes: the
    # bounce clears the memory, the dwell restarts from the descent at 18:32,
    # and 180 s of it have passed by the third sample after that.
    assert answers == [False, False, False, False, True]


def test_without_the_dwell_a_single_stray_reading_would_latch_the_whole_day():
    """Why `latch: daily` is never used on its own."""
    latch_only = {k: v for k, v in DUSK.items() if k != "for"}
    midday = NOW.replace(hour=13, minute=0)

    answers, _ = replay(latch_only, [(midday, 2799), (midday.replace(minute=10), 9000)])

    assert answers[-1] is True


def test_once_dusk_has_been_earned_no_later_wobble_takes_it_back():
    """The owner's safety: the phase happens once a day, and does not un-happen."""
    earned = [(NOW.replace(minute=m), 2700) for m in (30, 40)]
    wobble = [(NOW.replace(minute=50), 9000), (NOW.replace(hour=20, minute=0), 9000)]

    answers, _ = replay(DUSK, earned + wobble)

    assert answers[-2:] == [True, True]


def test_the_latch_lets_go_when_the_date_rolls_over():
    earned = [(NOW.replace(minute=m), 2700) for m in (30, 40)]
    tomorrow = [(NOW.replace(day=19, hour=13, minute=0), 9000)]

    answers, _ = replay(DUSK, earned + tomorrow)

    assert answers[-1] is False


def test_a_dwell_alone_does_let_go():
    """The counter to the latch test: without `latch:` it is a level again."""
    earned = [(NOW.replace(minute=m), 2700) for m in (30, 40)]

    answers, _ = replay(DEBOUNCED, [*earned, (NOW.replace(minute=50), 9000)])

    assert answers[-1] is False


def test_only_remembered_conditions_are_resolved():
    plain = {"condition": "numeric_state", "entity_id": LUX, "below": 2800, "default": 0}

    assert resolve([plain], world(1), {}) == {}


def test_an_unreadable_sensor_falls_to_the_stated_default():
    unreadable = World(states={}, now=NOW)

    assert resolve([DEBOUNCED], unreadable, {KEY: Dwell(since=NOW)})[KEY].since is None
