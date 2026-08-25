"""Unit tests for the scenario-derivation helpers in `scenarios.py` itself.

These are tests of the test infrastructure: bugs here make the suite blind
without ever failing loudly, so they get the same rigor as production code.
"""

from __future__ import annotations

from cover_logic.config_schema import load_config
from scenarios import derive_axes, fired_rules, rule_witnesses, worlds

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
    all_worlds = worlds(config)
    fired = fired_rules(config, all_worlds)
    dead = [
        f"{key}#{index}"
        for key, rules in config.rules.items()
        for index in range(len(rules))
        if f"{key}#{index}" not in fired
    ]
    assert not dead


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
    all_worlds = worlds(config)
    fired = fired_rules(config, all_worlds)
    dead = [
        f"{key}#{index}"
        for key, rules in config.rules.items()
        for index in range(len(rules))
        if f"{key}#{index}" not in fired
    ]
    assert not dead


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
    all_worlds = worlds(config)
    fired = fired_rules(config, all_worlds)
    dead = [
        f"{key}#{index}"
        for key, rules in config.rules.items()
        for index in range(len(rules))
        if f"{key}#{index}" not in fired
    ]
    assert not dead
