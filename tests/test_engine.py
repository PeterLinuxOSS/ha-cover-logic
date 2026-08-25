import datetime as dt
import re

import pytest

from cover_logic.config_schema import load_config
from cover_logic.engine import EngineError, evaluate
from cover_logic.model import KEEP, Action
from cover_logic.world import Event, World

NOW = dt.datetime(2026, 8, 19, 13, 0)

CFG = load_config("""
blinds:
  - {entity: cover.a, facade_azimuth: 270}
  - {entity: cover.b, facade_azimuth: 90}
zones:
  terasa: {members: [cover.a]}
  kvety:  {members: [cover.b], occupants: [peter]}
modes:
  - {id: noc, when: !ref cover_down}
  - {id: den}
conditions:
  cover_down: {condition: state, entity_id: input_boolean.cover_down, state: "on"}
  doma:       {condition: state, entity_id: binary_sensor.is_home, state: "on"}
values:
  kvety_poz: {entity: input_number.kvety_pozicia_zaluzie, default: 34}
rules:
  noc.terasa: [{then: {position: keep, tilt: keep}}]
  noc.kvety:  [{then: {position: keep, tilt: keep}}]
  den.terasa:
    - {name: doma-hore, if: !ref doma, then: {position: 100, tilt: keep}}
    - {name: inak-dole, then: {position: 0, tilt: 50}}
  den.kvety:
    - {events: [arrival], if: {condition: event_targets_zone},
       then: {position: 100, tilt: 100}}
    - {then: {position: !ref kvety_poz, tilt: 0}}
""")


def world(states, event=None) -> World:
    return World(states=states, attributes={}, now=NOW, event=event or Event())


DEN = {"input_boolean.cover_down": "off", "binary_sensor.is_home": "on"}


def test_mode_is_the_first_matching_one():
    d = evaluate(CFG, world({**DEN, "input_boolean.cover_down": "on"}))
    assert d.mode == "noc"


def test_last_mode_without_a_condition_is_the_fallback():
    assert evaluate(CFG, world(DEN)).mode == "den"


def test_first_matching_rule_wins():
    d = evaluate(CFG, world(DEN))
    assert d.targets["cover.a"] == Action(position=100, tilt=KEEP)


def test_later_rule_applies_when_the_earlier_one_does_not_match():
    d = evaluate(CFG, world({**DEN, "binary_sensor.is_home": "off"}))
    assert d.targets["cover.a"] == Action(position=0, tilt=50)


def test_every_blind_gets_exactly_one_action():
    d = evaluate(CFG, world(DEN))
    assert set(d.targets) == {"cover.a", "cover.b"}


def test_refs_are_resolved_to_integers():
    states = {**DEN, "input_number.kvety_pozicia_zaluzie": "42"}
    d = evaluate(CFG, world(states))
    assert d.targets["cover.b"] == Action(position=42, tilt=0)


def test_ref_falls_back_to_its_default_when_the_helper_is_missing():
    d = evaluate(CFG, world(DEN))
    assert d.targets["cover.b"] == Action(position=34, tilt=0)


def test_ref_truncates_toward_zero_like_jinjas_int_filter():
    # Parity-critical: 50.7 must resolve to 50, not round to 51. See
    # docs/rationale.md -- "Why `_resolve_value` truncates instead of
    # rounding". Must NOT be "improved" to rounding.
    states = {**DEN, "input_number.kvety_pozicia_zaluzie": "50.7"}
    d = evaluate(CFG, world(states))
    assert d.targets["cover.b"] == Action(position=50, tilt=0)


def test_ref_value_above_100_passes_through_unclamped():
    # Deliberate. See docs/rationale.md -- "Why the engine does not clamp
    # resolved values to 0..100".
    states = {**DEN, "input_number.kvety_pozicia_zaluzie": "150"}
    d = evaluate(CFG, world(states))
    assert d.targets["cover.b"] == Action(position=150, tilt=0)


def test_ref_negative_value_passes_through_unclamped():
    # Same reasoning as above, for the other side of the range.
    states = {**DEN, "input_number.kvety_pozicia_zaluzie": "-5"}
    d = evaluate(CFG, world(states))
    assert d.targets["cover.b"] == Action(position=-5, tilt=0)


def test_event_scoped_rule_is_skipped_on_a_plain_state_change():
    d = evaluate(CFG, world(DEN))
    assert d.targets["cover.b"].tilt == 0


def test_event_scoped_rule_applies_on_arrival_of_that_zones_occupant():
    d = evaluate(CFG, world(DEN, Event(kind="arrival", person="peter")))
    assert d.targets["cover.b"] == Action(position=100, tilt=100)


def test_event_scoped_rule_is_skipped_for_another_persons_arrival():
    d = evaluate(CFG, world(DEN, Event(kind="arrival", person="mimka")))
    assert d.targets["cover.b"] == Action(position=34, tilt=0)


def test_trace_names_the_rule_that_fired():
    d = evaluate(CFG, world(DEN))
    assert d.trace["cover.a"] == "den.terasa#0 doma-hore"


def test_missing_rule_list_means_keep_and_says_so():
    cfg = load_config("""
blinds: [{entity: cover.a}]
zones: {terasa: {members: [cover.a]}}
modes: [{id: den}]
conditions: {}
values: {}
rules: {}
""")
    d = evaluate(cfg, world({}))
    assert d.targets["cover.a"] == Action(KEEP, KEEP)
    assert d.trace["cover.a"] == "den.terasa#none"


def test_a_blind_in_two_zones_is_an_error():
    cfg = load_config("""
blinds: [{entity: cover.a}]
zones:
  one: {members: [cover.a]}
  two: {members: [cover.a]}
modes: [{id: den}]
conditions: {}
values: {}
rules:
  den.one: [{then: {position: 0}}]
  den.two: [{then: {position: 100}}]
""")
    with pytest.raises(EngineError, match=re.escape("cover.a")):
        evaluate(cfg, world({}))


def test_a_blind_owned_by_no_zone_is_an_error():
    cfg = load_config("""
blinds:
  - {entity: cover.a}
  - {entity: cover.orphan}
zones:
  one: {members: [cover.a]}
modes: [{id: den}]
conditions: {}
values: {}
rules:
  den.one: [{then: {position: 0}}]
""")
    with pytest.raises(EngineError, match=re.escape("cover.orphan")):
        evaluate(cfg, world({}))


def test_no_matching_mode_is_an_error():
    cfg = load_config("""
blinds: [{entity: cover.a}]
zones: {z: {members: [cover.a]}}
modes: [{id: noc, when: !ref never}]
conditions:
  never: {condition: state, entity_id: nothing.here, state: "on"}
values: {}
rules: {den.z: [{then: {position: 0}}]}
""")
    with pytest.raises(EngineError, match="no mode matched"):
        evaluate(cfg, world({}))


def test_the_same_world_always_gives_the_same_decision():
    w = world(DEN)
    assert evaluate(CFG, w) == evaluate(CFG, w)


def test_a_zone_whose_rule_raises_gets_keep_and_other_zones_are_unaffected():
    """A hand-written condition body with an unknown type bypasses validate()
    (config_schema deliberately does not check condition-body keys) and
    raises inside evaluate_condition. That must not take down blinds in
    other, unrelated zones.
    """
    cfg = load_config("""
blinds:
  - {entity: cover.a}
  - {entity: cover.b}
zones:
  broken: {members: [cover.a]}
  fine: {members: [cover.b]}
modes: [{id: den}]
conditions: {}
values: {}
rules:
  den.broken:
    - {if: {condition: sate, entity_id: x, state: "on"}, then: {position: 100}}
    - {then: {position: 0}}
  den.fine:
    - {then: {position: 42}}
""")
    d = evaluate(cfg, world({}))

    # The unaffected zone decided normally.
    assert d.targets["cover.b"] == Action(position=42, tilt=KEEP)
    assert d.trace["cover.b"] == "den.fine#0"

    # The broken zone's blind keeps its position, and the trace names the
    # exception so the cause is recoverable from the output alone.
    assert d.targets["cover.a"] == Action()
    assert d.trace["cover.a"].startswith("den.broken#error ")
    assert "ValueError" in d.trace["cover.a"]
    assert "sate" in d.trace["cover.a"]

    # `fired_rules` recovers the rule key via `label.split(" ")[0]` -- the
    # error label must still split cleanly into a key-shaped first token.
    assert d.trace["cover.a"].split(" ")[0] == "den.broken#error"


def test_zone_naming_an_unknown_blind_still_raises():
    """This is an ownership-class failure (there is no valid decision to
    make at all), not a rule-evaluation failure -- it must propagate out of
    evaluate(), not be contained at the zone boundary.
    """
    cfg = load_config("""
blinds: [{entity: cover.a}]
zones:
  broken: {members: [cover.a, cover.ghost]}
modes: [{id: den}]
conditions: {}
values: {}
rules:
  den.broken: [{then: {position: 0}}]
""")
    with pytest.raises(EngineError, match=re.escape("cover.ghost")):
        evaluate(cfg, world({}))


def test_mode_resolution_error_still_raises_alongside_a_broken_zone():
    """Mode resolution happens before any zone is evaluated, so a broken
    rule elsewhere in the config must not mask a real 'no mode matched'.
    """
    cfg = load_config("""
blinds: [{entity: cover.a}]
zones: {z: {members: [cover.a]}}
modes: [{id: noc, when: !ref never}]
conditions:
  never: {condition: state, entity_id: nothing.here, state: "on"}
values: {}
rules:
  den.z: [{if: {condition: sate, entity_id: x, state: "on"}, then: {position: 0}}]
""")
    with pytest.raises(EngineError, match="no mode matched"):
        evaluate(cfg, world({}))


def test_equal_but_separately_built_worlds_give_the_same_decision():
    # The assertion above only proves idempotency on one object. Reproducibility
    # across equal snapshots is a separate property: two independently
    # constructed World instances with equal contents must still decide alike.
    w1 = world(dict(DEN))
    w2 = world(dict(DEN))
    assert w1 is not w2
    assert evaluate(CFG, w1) == evaluate(CFG, w2)
