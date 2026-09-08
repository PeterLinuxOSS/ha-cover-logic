"""Unit tests for the scenario-derivation helpers in `scenarios.py` itself.

These are tests of the test infrastructure: bugs here make the suite blind
without ever failing loudly, so they get the same rigor as production code.
"""

import datetime as dt
import os
import subprocess
import sys

from scenarios import (
    CLOCK_AXIS,
    SUN,
    _clock_probes,
    derive_axes,
    fired_rules,
    pairwise,
    rule_witnesses,
    worlds,
)

from cover_logic.config_schema import load_config


def dead_rules(config, all_worlds):
    fired = fired_rules(config, all_worlds)
    return [
        f"{key}#{index}"
        for key, rules in config.rules.items()
        for index in range(len(rules))
        if f"{key}#{index}" not in fired
    ]


TWO_AZIMUTH_OVERRIDES = """
blinds:
  - {entity: cover.a, facade_azimuth: 180}
  - {entity: cover.b, facade_azimuth: 180}
zones:
  zone_one: {members: [cover.a]}
  zone_two: {members: [cover.b]}
modes:
  - {id: den}
conditions: {}
values: {}
rules:
  den.zone_one:
    - {if: {condition: sun_hits_target, azimuth_entity: sensor.az_one}, then: {position: 0}}
    - {then: {position: 100}}
  den.zone_two:
    - {if: {condition: sun_hits_target, azimuth_entity: sensor.az_two}, then: {position: 0}}
    - {then: {position: 100}}
"""


def test_two_azimuth_entity_overrides_both_get_axes():
    """Two zones using different `azimuth_entity` overrides must each get their
    own axis. The old `_sun_entities()` let the last node win, so
    `sensor.az_one` was never set in any generated world -- its rule looked
    dead not because it was unreachable, but because the harness never
    varied the sensor it actually reads.
    """
    config = load_config(TWO_AZIMUTH_OVERRIDES)
    axes = derive_axes(config)
    assert "sensor.az_one" in axes
    assert "sensor.az_two" in axes
    assert "sun.sun" in axes


def test_two_azimuth_entity_overrides_no_rule_reported_dead():
    config = load_config(TWO_AZIMUTH_OVERRIDES)
    assert not dead_rules(config, worlds(config))


CONDITION_LEVEL_TOLERANCE = """
blinds:
  - {entity: cover.a, facade_azimuth: 180}
zones:
  zone_one: {members: [cover.a]}
modes:
  - {id: den}
conditions: {}
values: {}
rules:
  den.zone_one:
    - {if: {condition: sun_hits_target, tolerance: 10}, then: {position: 0}}
    - {then: {position: 100}}
"""


def test_azimuth_probes_include_condition_level_tolerance_boundary():
    """A rule writing `{condition: sun_hits_target, tolerance: 10}` against a
    blind at facade_azimuth 180 has its real half-open boundary at 170 and
    190 -- not at the blind's own tolerance of 45 (135/225). Deriving probes
    only from `blind.tolerance` never probes 170/190, so an off-by-one right
    at this rule's actual boundary is invisible.
    """
    config = load_config(CONDITION_LEVEL_TOLERANCE)
    axes = derive_axes(config)
    probes = axes["sensor.sun_solar_azimuth"]
    assert "170" in probes
    assert "190" in probes


INTEGER_ATTRIBUTE_STATE = """
blinds:
  - {entity: cover.a, facade_azimuth: 180}
zones:
  zone_one: {members: [cover.a]}
modes:
  - {id: den}
conditions: {}
values: {}
rules:
  den.zone_one:
    - {if: {condition: state, entity_id: cover.x, attribute: current_position, state: 100},
       then: {position: 0}}
    - {then: {position: 100}}
"""


def test_integer_attribute_condition_rule_fires_in_a_generated_world():
    """`{condition: state, attribute: current_position, state: 100}` compares
    with plain `==` in `_state()` -- Home Assistant attributes are typed, so
    a production `World` holds the real int `100`. If the scenario harness
    stringifies the axis value to `'100'`, `100 == '100'` is False and this
    rule looks dead here while it fires for real in production -- exactly
    backwards from what the suite is supposed to prove.
    """
    config = load_config(INTEGER_ATTRIBUTE_STATE)
    all_worlds = worlds(config)
    fired = fired_rules(config, all_worlds)
    assert "den.zone_one#0" in fired


TWO_TOLERANCE_RULE_LIST = """
blinds:
  - {entity: cover.a, facade_azimuth: 180}
zones:
  zone_one: {members: [cover.a]}
modes:
  - {id: den}
conditions: {}
values: {}
rules:
  den.zone_one:
    - {if: {condition: sun_hits_target, tolerance: 10}, then: {position: 10}}
    - {if: {condition: sun_hits_target, tolerance: 45}, then: {position: 45}}
    - {then: {position: 0}}
"""


def test_second_of_two_tolerance_rules_gets_a_structural_witness():
    """`rule_witnesses` solves each rule's own guard directly from its parsed
    condition tree, independent of whatever `pairwise()` happens to cover --
    tested here in isolation from `pairwise()` so the assertion cannot pass
    by pairwise coverage accidentally landing on the answer.

    Solving rule #1 (tolerance 45) requires rule #0 (tolerance 10) false and
    rule #1 true simultaneously. Rule #1's own guard is solved first and
    pins the sun entity to 'above_horizon'; falsifying rule #0 afterwards
    must then go through the azimuth -- some probe outside rule #0's
    10-degree sector [170, 190) but still inside rule #1's 45-degree sector
    [135, 225), e.g. 135 itself -- not through the sun entity, which is
    already pinned true and so can never falsify anything. The old code only
    ever tried the sun entity for the false branch, so it reported this
    plainly reachable rule as unsolvable (silently skipped by
    `rule_witnesses`, the same way a genuinely dead rule would be).
    """
    config = load_config(TWO_TOLERANCE_RULE_LIST)
    axes = derive_axes(config)
    witnesses = rule_witnesses(config, axes)
    fired = fired_rules(config, witnesses)
    assert "den.zone_one#1" in fired


def test_two_tolerance_rule_list_has_no_dead_rule_end_to_end():
    config = load_config(TWO_TOLERANCE_RULE_LIST)
    assert not dead_rules(config, worlds(config))


AZIMUTH_ATTRIBUTE = """
blinds:
  - {entity: cover.a, facade_azimuth: 180}
zones:
  zone_one: {members: [cover.a]}
modes:
  - {id: den}
conditions: {}
values: {}
rules:
  den.zone_one:
    - {if: {condition: sun_hits_target, azimuth_entity: sun.sun, azimuth_attribute: azimuth},
       then: {position: 0}}
    - {then: {position: 100}}
"""


def test_azimuth_attribute_condition_gets_an_axis():
    """`_sun_entity_pairs` used to assume the azimuth was always a plain
    state; a config reading it off `sun.sun`'s `azimuth` attribute must get
    its own attribute-keyed axis, exactly like any other attribute
    condition, not be silently dropped.
    """
    config = load_config(AZIMUTH_ATTRIBUTE)
    axes = derive_axes(config)
    assert "sun.sun|azimuth" in axes
    assert "sun.sun" in axes


TWO_TOLERANCE_RULE_LIST_AZIMUTH_ATTRIBUTE = """
blinds:
  - {entity: cover.a, facade_azimuth: 180}
zones:
  zone_one: {members: [cover.a]}
modes:
  - {id: den}
conditions: {}
values: {}
rules:
  den.zone_one:
    - {if: {condition: sun_hits_target, azimuth_entity: sun.sun, azimuth_attribute: azimuth,
             tolerance: 10}, then: {position: 10}}
    - {if: {condition: sun_hits_target, azimuth_entity: sun.sun, azimuth_attribute: azimuth,
             tolerance: 45}, then: {position: 45}}
    - {then: {position: 0}}
"""


def test_azimuth_attribute_rule_gets_a_structural_witness_in_a_deep_chain():
    """The same deep-chain reachability `rule_witnesses` proves for the
    state-based path (`test_second_of_two_tolerance_rules_gets_a_structural_
    witness`) must also hold when the azimuth is read off an attribute --
    `_require`'s `sun_hits_target` branch used to build its probe worlds by
    writing the azimuth into `states`, unconditionally, which silently
    never satisfies an `azimuth_attribute` condition and would report a
    reachable rule as dead the moment pairwise coverage alone is not enough.
    """
    config = load_config(TWO_TOLERANCE_RULE_LIST_AZIMUTH_ATTRIBUTE)
    axes = derive_axes(config)
    witnesses = rule_witnesses(config, axes)
    fired = fired_rules(config, witnesses)
    assert "den.zone_one#1" in fired


def test_azimuth_attribute_rule_is_not_reported_dead():
    """Verify end-to-end, the same way `test_every_rule_fires_at_least_once`
    does: a config using `azimuth_attribute` must not have its sun rule
    reported dead just because the harness never varied the attribute it
    actually reads.
    """
    config = load_config(AZIMUTH_ATTRIBUTE)
    assert not dead_rules(config, worlds(config))


# --- The clock axis (issue #6) ----------------------------------------------

TIME_GATED_RULE = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: day}
conditions: {}
values: {}
rules:
  day.z:
    - {if: {condition: time, after: "22:00", before: "06:00"}, then: {position: 0}}
    - {then: {position: 100}}
"""

TIME_GATED_MODE = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: night, when: {condition: time, after: "22:00", before: "06:00"}}
  - {id: day}
conditions: {}
values: {}
rules:
  night.z:
    - {then: {position: 0}}
  day.z:
    - {then: {position: 100}}
"""

SUN_GATED_MODE = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: night, when: {condition: sun, before: sunrise, before_offset: -1260, after: sunset}}
  - {id: day}
conditions: {}
values: {}
rules:
  night.z:
    - {then: {position: 0}}
  day.z:
    - {then: {position: 100}}
"""

# The house's own `je_noc` and `vecer` sun windows, side by side: sunset+0 vs
# sunset-1200, sunrise-1260 vs sunrise+600. Telling them apart needs the clock
# to the minute -- docs/rationale.md, "Why the clock is the axis and the sky
# is a constant".
TWO_SUN_WINDOWS_A_MINUTE_APART = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: noc, when: {condition: sun, before: sunrise, before_offset: -1260, after: sunset}}
  - {id: vecer, when: {condition: sun, before: sunrise, before_offset: 600,
                       after: sunset, after_offset: -1200}}
  - {id: den}
conditions: {}
values: {}
rules:
  noc.z:
    - {then: {position: 0}}
  vecer.z:
    - {then: {position: 10}}
  den.z:
    - {then: {position: 100}}
"""

NO_CLOCK_CONDITION = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: day}
conditions: {}
values: {}
rules:
  day.z:
    - {then: {position: 100}}
"""


def at(hour: int, minute: int) -> dt.datetime:
    return dt.datetime(2026, 8, 19, hour, minute)


def test_time_gated_rule_is_not_reported_dead():
    """Issue #6's first verified symptom: `{condition: time, after: "22:00",
    before: "06:00"}` on a rule reported `dead=['day.z#0']`, because `NOW` was
    a module constant at 13:00 and `_require`'s `time` branch gave up rather
    than choosing an instant.
    """
    config = load_config(TIME_GATED_RULE)
    assert not dead_rules(config, worlds(config))


def test_time_gated_mode_is_not_reported_dead():
    """Issue #6's second symptom, and the one that costs a whole rule set: a
    mode gated on a clock window reported `dead=['night.z#0']`.
    """
    config = load_config(TIME_GATED_MODE)
    assert not dead_rules(config, worlds(config))


def test_sun_gated_mode_is_not_reported_dead():
    """`condition: sun` was the same defect wearing a different hat: its
    `_require` branch said outright that the sun is "fixed by the world, not
    by any entity this generator can vary", so a sky-derived night mode was
    reported dead too.
    """
    config = load_config(SUN_GATED_MODE)
    assert not dead_rules(config, worlds(config))


def test_two_sun_windows_a_minute_apart_are_separable():
    """The failure that sank the sky-probe attempt: `vecer.z#0` needs its own
    sun window TRUE while the preceding `noc` window is FALSE, and those two
    windows differ by 60 seconds at each end. Coarse `SunTimes` probes cannot
    separate them; a clock axis at one-minute resolution can.
    """
    config = load_config(TWO_SUN_WINDOWS_A_MINUTE_APART)
    axes = derive_axes(config)
    witnesses = rule_witnesses(config, axes)
    assert "vecer.z#0" in fired_rules(config, witnesses)


def test_clock_axis_probes_each_time_boundary_plus_minus_a_minute():
    config = load_config(TIME_GATED_RULE)
    probes = derive_axes(config)[CLOCK_AXIS]
    assert at(21, 59) in probes
    assert at(22, 1) in probes
    assert at(5, 59) in probes
    assert at(6, 1) in probes


def test_clock_axis_probes_each_sun_boundary_plus_minus_a_minute():
    """A `sun` clause's boundary is an offset from the FIXED sky, so it lands
    on a knowable instant: sunrise - 1260 s and sunset + 0 s here.
    """
    config = load_config(SUN_GATED_MODE)
    probes = derive_axes(config)[CLOCK_AXIS]
    minute = dt.timedelta(minutes=1)
    dawn = SUN.sunrise - dt.timedelta(seconds=1260)
    assert dawn - minute in probes
    assert dawn + minute in probes
    assert SUN.sunset - minute in probes
    assert SUN.sunset + minute in probes


def test_clock_probes_are_deduplicated_and_sorted():
    """A scenario set that differs between runs cannot be reproduced, so the
    probe list has to be an ordered sequence rather than whatever order a set
    iterated in. Asserted on `_clock_probes` itself: `derive_axes` funnels
    every axis through `set` and `sorted` anyway, so checking it there would
    pass no matter what this function returned.
    """
    config = load_config(TWO_SUN_WINDOWS_A_MINUTE_APART)
    probes = _clock_probes(config)
    assert probes == sorted(set(probes))
    assert probes == derive_axes(config)[CLOCK_AXIS]


def test_config_without_a_clock_condition_still_gets_a_clock_axis():
    """No `time` and no `sun` clause means no derived boundary at all, and an
    empty axis would make `pairwise` produce rows with no `now` in them.
    """
    config = load_config(NO_CLOCK_CONDITION)
    probes = derive_axes(config)[CLOCK_AXIS]
    assert len(probes) >= 2
    assert any(SUN.sunrise < instant < SUN.sunset for instant in probes)
    assert any(instant < SUN.sunrise or instant > SUN.sunset for instant in probes)


def test_the_clock_axis_key_cannot_collide_with_an_entity_axis():
    """Every other axis key is built from an entity id, which is always
    `domain.object_id`; the reserved clock key has no dot, so no configuration
    can name an entity that lands on it.
    """
    assert "." not in CLOCK_AXIS
    for config_text in (TIME_GATED_RULE, TWO_SUN_WINDOWS_A_MINUTE_APART, NO_CLOCK_CONDITION):
        keys = set(derive_axes(load_config(config_text)))
        assert CLOCK_AXIS in keys
        assert all("." in key for key in keys - {CLOCK_AXIS})


# --- pairwise() must not return partial coverage silently -------------------

# Two four-value axes need all sixteen combinations, and the greedy pass
# stalls after seven rows -- it can no longer improve any single coordinate,
# so it used to `break` and hand back a set covering nine pairs short of its
# own documented guarantee.
STALLING_AXES = {"e.0": ["v0", "v1", "v2", "v3"], "e.1": ["v0", "v1", "v2", "v3"]}


def test_pairwise_covers_every_pair_even_when_the_greedy_pass_stalls():
    rows = pairwise(STALLING_AXES)
    covered = {(row["e.0"], row["e.1"]) for row in rows}
    missing = [
        (a, b) for a in STALLING_AXES["e.0"] for b in STALLING_AXES["e.1"] if (a, b) not in covered
    ]
    assert not missing, missing


def test_pairwise_is_deterministic_when_it_has_to_plant_rows():
    """Which pair gets planted is chosen out of a SET, so it has to be chosen
    by an explicit sort key. Two calls in one process would not show this --
    `str` hashing is randomised per interpreter, not per call -- so the check
    runs under two different `PYTHONHASHSEED` values.
    """
    rows = [_pairwise_rows_under_hash_seed(seed) for seed in ("0", "1")]
    assert rows[0] == rows[1]


def _pairwise_rows_under_hash_seed(seed: str) -> str:
    code = (
        "import json\n"
        "from scenarios import pairwise\n"
        f"print(json.dumps(pairwise({STALLING_AXES!r})))\n"
    )
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": os.pathsep.join(sys.path)}
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout


# --- event_targets_zone must be resolved, not waved through -----------------

ARRIVAL_TARGETED_PRIOR_RULE = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a], occupants: [peter]}
modes:
  - {id: day}
conditions: {}
values: {}
rules:
  day.z:
    - if:
        - {condition: event_targets_zone}
        - {condition: not, conditions: [{condition: state, entity_id: input_boolean.x,
                                         state: "on"}]}
      then: {position: 0}
    - {events: [arrival], then: {position: 50}}
    - {then: {position: 100}}
"""


def test_negating_an_arrival_targeted_rule_is_not_a_silent_pass():
    """Rule #1 only runs on an arrival, so its witness carries the zone
    occupant's arrival event -- which makes rule #0's `event_targets_zone`
    unavoidably TRUE. `_require` used to return as satisfied for that
    condition whatever `want_true` said, so negating rule #0 was a no-op: the
    witness left `input_boolean.x` free, rule #0 fired in it, and rule #1
    never did. Resolving it against the chosen event forces the AND-false
    search onto the other child instead.
    """
    config = load_config(ARRIVAL_TARGETED_PRIOR_RULE)
    axes = derive_axes(config)
    witnesses = rule_witnesses(config, axes)
    assert "day.z#1" in fired_rules(config, witnesses)
