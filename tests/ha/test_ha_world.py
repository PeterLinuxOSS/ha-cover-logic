"""Tests for `ha_world.build_world`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv (`.venv/bin/python -m pytest`). Under system Python 3.12, which has no
`homeassistant` installed, `importorskip` turns the whole module into a
single reported skip rather than a collection error -- the system-Python run
must stay green without this suite ever running there.
"""

import datetime as dt

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import State
import homeassistant.util.dt as dt_util

from cover_logic.ha_world import build_world
from cover_logic.world import Event


def test_only_referenced_entities_are_read(config, fake_hass):
    hass = fake_hass(
        {
            "input_boolean.a": State("input_boolean.a", "on"),
            "input_number.kvety_pozicia_zaluzie": State("input_number.kvety_pozicia_zaluzie", "42"),
            "cover.a": State("cover.a", "open", {"current_position": 100}),
            # Not named by referenced_entities(config) -- must not appear.
            "sensor.unrelated": State("sensor.unrelated", "should_not_appear"),
        }
    )

    world = build_world(hass, config)

    assert set(world.states) == {"input_boolean.a", "input_number.kvety_pozicia_zaluzie"}
    assert "sensor.unrelated" not in world.states
    assert world.state("sensor.unrelated") is None


def test_int_attribute_survives_as_int(config, fake_hass):
    hass = fake_hass({"cover.a": State("cover.a", "open", {"current_position": 100})})

    world = build_world(hass, config)

    value = world.attribute("cover.a", "current_position")
    assert value == 100
    assert type(value) is int


def test_missing_entity_is_absent_not_a_placeholder(config, fake_hass):
    hass = fake_hass({})  # input_boolean.a is referenced but never set

    world = build_world(hass, config)

    assert world.state("input_boolean.a") is None
    assert "input_boolean.a" not in world.states


def test_missing_attribute_is_absent(config, fake_hass):
    # cover.a exists, but without the current_position attribute.
    hass = fake_hass({"cover.a": State("cover.a", "open", {"other_attr": 1})})

    world = build_world(hass, config)

    assert world.attribute("cover.a", "current_position") is None
    assert ("cover.a", "current_position") not in world.attributes


def test_now_is_naive_and_matches_ha_local_wall_clock(config, fake_hass):
    """`World.now` must agree with what `conditions._time`/`_parse_hhmm` expect.

    `_parse_hhmm` builds naive `dt.time` objects and `_time` compares them
    against `world.now.time()`, which drops tzinfo regardless of whether
    `world.now` itself is aware -- so the *wall-clock reading* is what must
    match a live Jinja `now()`, not the presence of tzinfo. The pure engine's
    own convention (see pyproject.toml's ruff DTZ note and tests/scenarios.py)
    is a naive `World.now`; `build_world` keeps that convention while sourcing
    the value from `homeassistant.util.dt.now()` -- HA's DST-safe local clock
    -- rather than any hand-rolled UTC-offset conversion.
    """
    hass = fake_hass({})

    before = dt_util.now().replace(tzinfo=None)
    world = build_world(hass, config)
    after = dt_util.now().replace(tzinfo=None)

    assert world.now.tzinfo is None
    assert before <= world.now <= after


def test_now_follows_the_configured_zone_not_utc(config, fake_hass):
    """`World.now` must read the wall clock of Home Assistant's configured zone.

    The test above pins the implementation (`dt_util.now()`), but in a bare venv
    `dt_util` has no zone set and defaults to UTC -- so that test's notion of
    "correct" is UTC, while the house this runs in is on Europe/Prague. Inside a
    real Home Assistant the zone is set during core setup and the two differ by
    an hour or two.

    That gap matters: the `vecer` condition is `after: "12:30"`, compared against
    `world.now.time()`. A two-hour shift moves the evening window by two hours
    and would surface as a spurious disagreement with the old matrix. So pin the
    property, not just the call: set a non-UTC zone and assert the reading
    follows it.
    """
    original = dt_util.get_default_time_zone()
    try:
        dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Prague"))
        hass = fake_hass({})

        world = build_world(hass, config)

        expected = dt.datetime.now(dt_util.get_time_zone("Europe/Prague"))
        assert abs((world.now - expected.replace(tzinfo=None)).total_seconds()) < 5
    finally:
        dt_util.set_default_time_zone(original)


def test_default_event_is_a_plain_state_change(config, fake_hass):
    hass = fake_hass({})

    world = build_world(hass, config)

    assert world.event == Event()


def test_explicit_event_is_carried_through(config, fake_hass):
    hass = fake_hass({})
    event = Event(kind="arrival", person="peter")

    world = build_world(hass, config, event=event)

    assert world.event is event


def test_now_type_is_datetime(config, fake_hass):
    hass = fake_hass({})

    world = build_world(hass, config)

    assert isinstance(world.now, dt.datetime)
