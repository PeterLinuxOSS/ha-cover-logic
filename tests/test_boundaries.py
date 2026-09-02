"""`next_boundary`: the clock the integration did not have for its own conditions.

Every evaluation is triggered by a referenced entity changing, and `state`'s
`for:`, `time` and `sun` change answer at an instant that produces no state
change. Measured on the owner's house on 2026-09-02: nine time-derived
conditions, and only `sunset` coincides with one. See `boundaries.py`'s module
docstring for the list.
"""

import datetime as dt

from cover_logic.boundaries import next_boundary
from cover_logic.config_schema import load_config
from cover_logic.world import SunTimes, World

NOW = dt.datetime(2026, 9, 2, 12, 0, 0)
SUN = SunTimes(sunrise=dt.datetime(2026, 9, 2, 6, 0, 0), sunset=dt.datetime(2026, 9, 2, 19, 24, 0))


def _config(conditions: str) -> object:
    return load_config(
        """
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
        + conditions
    )


def _world(**kwargs) -> World:
    return World(states=kwargs.pop("states", {}), now=kwargs.pop("now", NOW), sun=SUN, **kwargs)


def test_a_configuration_with_no_time_derived_condition_has_no_boundary():
    """The counter to arming a timer for nothing: a still house must cost nothing."""
    config = _config("""
conditions:
  plain:
    condition: state
    entity_id: input_boolean.a
    state: "on"
""")

    assert next_boundary(config, _world()) is None


def test_a_pending_for_is_reported_as_the_seconds_left():
    """`vstali` in the owner's house: bed `off for 120`, and nothing fires at 120 s."""
    config = _config("""
conditions:
  vstali:
    condition: state
    entity_id: binary_sensor.postel
    state: "off"
    for: 120
""")
    world = _world(
        states={"binary_sensor.postel": "off"},
        since={"binary_sensor.postel": NOW - dt.timedelta(seconds=90)},
    )

    assert next_boundary(config, world) == 30.0


def test_an_elapsed_for_reports_nothing():
    """Already true needs no timer -- this evaluation is the one that sees it."""
    config = _config("""
conditions:
  vstali:
    condition: state
    entity_id: binary_sensor.postel
    state: "off"
    for: 120
""")
    world = _world(
        states={"binary_sensor.postel": "off"},
        since={"binary_sensor.postel": NOW - dt.timedelta(seconds=200)},
    )

    assert next_boundary(config, world) is None


def test_an_undatable_entity_reports_nothing():
    """No `since` means `conditions._state` *ignores* `for:`, so there is nothing to wait for."""
    config = _config("""
conditions:
  vstali:
    condition: state
    entity_id: binary_sensor.postel
    state: "off"
    for: 120
""")

    assert next_boundary(config, _world(states={"binary_sensor.postel": "off"})) is None


def test_a_sun_offset_is_reported_even_though_no_state_change_marks_it():
    """`vecer` in the owner's house: `sunset - 20 min`, which `sun.sun` never announces."""
    config = _config("""
conditions:
  vecer:
    condition: sun
    after: sunset
    after_offset: -1200
""")

    # 12:00 -> 19:04 (19:24 sunset, less 20 min) is 7h 4m.
    assert next_boundary(config, _world()) == 7 * 3600 + 4 * 60


def test_the_earliest_of_several_boundaries_wins():
    """One timer, so the answer is a minimum -- a later boundary must not hide a nearer one."""
    config = _config("""
conditions:
  vecer:
    condition: sun
    after: sunset
    after_offset: -1200
  obed:
    condition: time
    after: "12:30"
""")

    assert next_boundary(config, _world()) == 30 * 60


def test_a_boundary_already_past_today_is_not_reported():
    """`je_noc`'s `sunrise - 21 min` at midday is behind us; the floor covers tomorrow.

    `World.sun` carries *today's* sunrise only, so tomorrow's is not knowable
    from the snapshot -- see the module docstring on why that division with
    `RECONCILE_FLOOR_SECONDS` is deliberate rather than a gap.
    """
    config = _config("""
conditions:
  je_noc:
    condition: sun
    before: sunrise
    before_offset: -1260
""")

    assert next_boundary(config, _world()) is None


def test_an_inline_rule_condition_counts_too():
    """Not only the `conditions:` section -- `all_condition_nodes` walks every site.

    A boundary named only by a rule's `if` is exactly as real, and reading only
    named conditions works by accident in a config that routes everything
    through `!ref`.
    """
    config = load_config("""
blinds:
  - entity: cover.a
zones:
  z:
    members: [cover.a]
modes:
  - {id: any}
rules:
  any.z:
    - if: {condition: time, after: "12:30"}
      then: {position: 0, tilt: 0}
    - then: {position: keep, tilt: keep}
""")

    assert next_boundary(config, _world()) == 30 * 60
