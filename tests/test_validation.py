from cover_logic.config_schema import load_config
from cover_logic.validation import WARNING, validate

BASE = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  vzdy: {condition: state, entity_id: x, state: "on"}
values: {}
rules:
  den.z:
    - {if: !ref vzdy, then: {position: 100}}
    - {then: {position: 0}}
"""


def codes(text: str) -> set[str]:
    return {p.code for p in validate(load_config(text))}


def test_a_clean_config_has_no_problems():
    assert validate(load_config(BASE)) == []


def test_blind_without_a_zone_is_an_error():
    text = BASE.replace(
        "blinds:\n  - {entity: cover.a}",
        "blinds:\n  - {entity: cover.a}\n  - {entity: cover.orphan}",
    )
    assert "blind_without_zone" in codes(text)


def test_a_non_positive_travel_time_is_an_error():
    """`planner.plan` derives the arrival wait from this number.

    `travel_time: 0` gives `WaitForPosition(..., timeout=0.0)`: the wait
    expires the instant it starts, the 2 s settle runs against a ~55 s
    journey, and the tilt command lands mid-travel where the motor discards
    it -- the slats silently never arrive. The YAML path only does `float()`,
    so a negative value gets through it too.
    """
    for bad in ("0", "-5", "0.0"):
        text = BASE.replace("- {entity: cover.a}", f"- {{entity: cover.a, travel_time: {bad}}}")
        assert "bad_travel_time" in codes(text), bad


def test_a_positive_travel_time_is_not_reported():
    text = BASE.replace("- {entity: cover.a}", "- {entity: cover.a, travel_time: 0.5}")
    assert validate(load_config(text)) == []


def test_zone_referring_to_an_unknown_blind_is_an_error():
    text = BASE.replace("members: [cover.a]", "members: [cover.a, cover.ghost]")
    assert "zone_member_unknown" in codes(text)


def test_blind_in_two_zones_is_an_error():
    text = BASE.replace(
        "  z: {members: [cover.a]}", "  z: {members: [cover.a]}\n  z2: {members: [cover.a]}"
    )
    assert "blind_in_two_zones" in codes(text)


def test_missing_fallback_mode_is_an_error():
    text = BASE.replace("  - {id: den}", "  - {id: den, when: !ref vzdy}")
    assert "no_fallback_mode" in codes(text)


def test_fallback_mode_must_be_last():
    text = BASE.replace(
        "modes:\n  - {id: den}",
        "modes:\n  - {id: den}\n  - {id: noc, when: !ref vzdy}",
    )
    assert "fallback_mode_not_last" in codes(text)


def test_rule_key_for_an_unknown_zone_is_an_error():
    text = BASE + "\n  den.nezona:\n    - {then: {position: 0}}\n"
    assert "unknown_rule_key" in codes(text)


def test_missing_rule_list_is_a_warning():
    text = BASE.replace(
        "  den.z:\n    - {if: !ref vzdy, then: {position: 100}}\n    - {then: {position: 0}}",
        "  {}",
    )
    problems = validate(load_config(text))
    assert any(p.code == "missing_rule_list" and p.severity == "warning" for p in problems)


def test_a_rule_after_a_catch_all_is_unreachable():
    text = BASE.replace(
        "    - {then: {position: 0}}",
        "    - {then: {position: 0}}\n    - {if: !ref vzdy, then: {position: 50}}",
    )
    problems = validate(load_config(text))
    assert any(p.code == "unreachable_rule" for p in problems)


def test_a_rule_list_without_a_catch_all_is_a_warning():
    text = BASE.replace("    - {then: {position: 0}}", "")
    assert "no_catch_all" in codes(text)


def test_an_event_scoped_catch_all_does_not_shadow_other_events():
    text = """
blinds: [{entity: cover.a}]
zones: {z: {members: [cover.a]}}
modes: [{id: den}]
conditions: {}
values: {}
rules:
  den.z:
    - {events: [arrival], then: {position: 100}}
    - {then: {position: 0}}
"""
    assert "unreachable_rule" not in codes(text)


# --- Inherited (default) rules -------------------------------------------
#
# A rule filed under zone "*" (`const.RULE_DEFAULT_ZONE`) is a mode-wide
# default -- see `validation._check_rule_lists`'s own docstring for how
# `missing_rule_list`/`no_catch_all`/`unreachable_rule` each account for it.

DEFAULT_ONLY = """
blinds: [{entity: cover.a}, {entity: cover.b}]
zones:
  one: {members: [cover.a]}
  two: {members: [cover.b]}
modes: [{id: noc}]
conditions: {}
values: {}
rules:
  "noc.*":
    - {then: {position: 0}}
"""


def test_a_zone_with_no_rules_of_its_own_but_a_default_is_not_missing():
    assert "missing_rule_list" not in codes(DEFAULT_ONLY)


def test_a_zone_covered_only_by_the_defaults_catch_all_has_no_catch_all_warning():
    assert "no_catch_all" not in codes(DEFAULT_ONLY)


def test_the_default_key_itself_is_not_an_unknown_rule_key():
    assert "unknown_rule_key" not in codes(DEFAULT_ONLY)


SHADOWED = """
blinds: [{entity: cover.a}]
zones: {z: {members: [cover.a]}}
modes: [{id: noc}]
conditions: {}
values: {}
rules:
  noc.z: [{then: {position: 100}}]
  "noc.*": [{then: {position: 0}}]
"""


def test_a_default_the_zones_own_catch_all_already_shadows_is_a_warning():
    problems = validate(load_config(SHADOWED))
    matches = [p for p in problems if p.code == "unreachable_rule"]
    assert len(matches) == 1
    assert matches[0].severity == WARNING
    assert "noc.*#0" in matches[0].message


NOT_SHADOWED = """
blinds: [{entity: cover.a}]
zones: {z: {members: [cover.a]}}
modes: [{id: noc}]
conditions:
  never: {condition: state, entity_id: nothing.here, state: "on"}
values: {}
rules:
  noc.z: [{if: !ref never, then: {position: 100}}]
  "noc.*": [{then: {position: 0}}]
"""


def test_a_default_is_not_shadowed_when_the_zones_own_rule_is_conditional():
    assert "unreachable_rule" not in codes(NOT_SHADOWED)


SHARED_DEFAULT_WITH_AN_INTERNAL_ISSUE = """
blinds: [{entity: cover.a}, {entity: cover.b}]
zones:
  one: {members: [cover.a]}
  two: {members: [cover.b]}
modes: [{id: noc}]
conditions:
  vzdy: {condition: state, entity_id: x, state: "on"}
values: {}
rules:
  "noc.*":
    - {then: {position: 0}}
    - {if: !ref vzdy, then: {position: 50}}
"""


def test_an_unreachable_row_inside_a_shared_default_is_reported_once_not_per_zone():
    """Two zones ('one', 'two') inherit the same default list, which has an
    unreachable row entirely on its own account (a catch-all before it, no
    zone involved). That is one fact about one subentry group, not one fact
    per zone that happens to inherit it.
    """
    problems = validate(load_config(SHARED_DEFAULT_WITH_AN_INTERNAL_ISSUE))
    matches = [p for p in problems if p.code == "unreachable_rule"]
    assert len(matches) == 1
    assert "noc.*#1" in matches[0].message


THREE_ZONES_SHADOW_TWO_DEFAULTS = """
blinds: [{entity: cover.a}, {entity: cover.b}, {entity: cover.c}]
zones:
  z1: {members: [cover.a]}
  z2: {members: [cover.b]}
  z3: {members: [cover.c]}
modes: [{id: m}]
conditions: {}
values: {}
rules:
  "m.*":
    - {then: {position: 50}}
    - {then: {position: 60}}
  m.z1: [{then: {position: 1}}]
  m.z2: [{then: {position: 2}}]
  m.z3: [{then: {position: 3}}]
"""


def test_a_default_shadowed_by_every_zone_is_one_warning_not_one_per_zone():
    """The exact shape a code review caught: one mode-wide default plus a
    second, dead default row, and three zones each with their own catch-all.
    Before this fix, `_check_default_shadowed_by_zone` reported the shadow
    once per shadowing zone -- three zones turned one broken default into
    three near-identical warnings, and a seven-zone house (the live fixture)
    would have turned it into seven, burying the fact that only one rule is
    actually wrong. The count must not grow with the number of zones.
    """
    problems = validate(load_config(THREE_ZONES_SHADOW_TWO_DEFAULTS))
    matches = [p for p in problems if p.code == "unreachable_rule"]

    # One warning for "m.*#1" (already dead on the default list's own
    # account -- a catch-all before it, no zone involved) and exactly one
    # more for "m.*#0" (shadowed by all three zones' own catch-alls) -- not
    # one "m.*#0" warning per zone, and not "m.*#1" reported again just
    # because every zone's own catch-all also shadows it.
    assert len(matches) == 2
    shadow = next(m for m in matches if "m.*#0" in m.message)
    assert "z1" in shadow.message
    assert "z2" in shadow.message
    assert "z3" in shadow.message
    assert not any(m is not shadow and "m.*#0" in m.message for m in matches)


# --- Coverage gap: `no_catch_all` over the zone's own + inherited default list --
#
# Neither direction was pinned before: a zone whose own rules are entirely
# conditional and whose mode default is also entirely conditional must still
# warn (nothing in the concatenated effective list can ever fall through
# safely); a zone whose own rules are conditional-only but whose mode default
# supplies the catch-all must not warn (the mode default is exactly what
# `no_catch_all` is designed to see, via the same concatenation
# `_check_rule_lists` already builds for `missing_rule_list`).

BOTH_OWN_AND_DEFAULT_ARE_CONDITIONAL_ONLY = """
blinds: [{entity: cover.a}]
zones: {z: {members: [cover.a]}}
modes: [{id: noc}]
conditions:
  never: {condition: state, entity_id: nothing.here, state: "on"}
values: {}
rules:
  noc.z: [{if: !ref never, then: {position: 100}}]
  "noc.*": [{if: !ref never, then: {position: 0}}]
"""


def test_no_catch_all_fires_when_neither_the_zones_own_rules_nor_the_default_has_one():
    assert "no_catch_all" in codes(BOTH_OWN_AND_DEFAULT_ARE_CONDITIONAL_ONLY)


OWN_CONDITIONAL_DEFAULT_HAS_THE_CATCH_ALL = """
blinds: [{entity: cover.a}]
zones: {z: {members: [cover.a]}}
modes: [{id: noc}]
conditions:
  never: {condition: state, entity_id: nothing.here, state: "on"}
values: {}
rules:
  noc.z: [{if: !ref never, then: {position: 100}}]
  "noc.*": [{then: {position: 0}}]
"""


def test_no_catch_all_stays_silent_when_the_default_supplies_the_catch_all():
    assert "no_catch_all" not in codes(OWN_CONDITIONAL_DEFAULT_HAS_THE_CATCH_ALL)


def test_direct_self_referencing_condition_is_an_error():
    """A condition that directly refers to itself."""
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: ref, name: vzdy}",
    )
    assert "circular_condition_ref" in codes(text)


def test_two_condition_cycle_is_an_error():
    """Condition A refers to B, B refers to A."""
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: ref, name: niekedy}\n"
        "  niekedy: {condition: ref, name: vzdy}",
    )
    assert "circular_condition_ref" in codes(text)


def test_cycle_through_nested_and_condition_is_an_error():
    """Cycle detected even when ref is nested inside an 'and' condition."""
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_a: {condition: and, conditions: [{condition: ref, name: cond_b}]}
  cond_b: {condition: ref, name: cond_a}
values: {}
rules:
  den.z:
    - {if: !ref cond_a, then: {position: 100}}
    - {then: {position: 0}}
"""
    assert "circular_condition_ref" in codes(text)


def test_long_acyclic_chain_does_not_error():
    """A long chain of references without cycles should not produce problems."""
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_a: {condition: ref, name: cond_b}
  cond_b: {condition: ref, name: cond_c}
  cond_c: {condition: ref, name: cond_d}
  cond_d: {condition: state, entity_id: x, state: "on"}
values: {}
rules:
  den.z:
    - {if: !ref cond_a, then: {position: 100}}
    - {then: {position: 0}}
"""
    assert "circular_condition_ref" not in codes(text)


def test_unused_cyclic_condition_is_still_an_error():
    """Even if a cyclic condition is not referenced in rules, it's still an error."""
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}\n'
        "  bad: {condition: ref, name: bad}",
    )
    assert "circular_condition_ref" in codes(text)


def test_cycle_within_or_condition_is_an_error():
    """Cycle detected when ref is nested inside an 'or' condition."""
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_a: {condition: or, conditions: [{condition: ref, name: cond_b}]}
  cond_b: {condition: or, conditions: [{condition: ref, name: cond_a}]}
values: {}
rules:
  den.z:
    - {if: !ref cond_a, then: {position: 100}}
    - {then: {position: 0}}
"""
    assert "circular_condition_ref" in codes(text)


def test_cycle_message_reports_the_real_traversal_order():
    """The message must name the actual chain of references, not an alphabetical
    re-sort of the cycle's members. cond_x refers to cond_z, cond_z refers to
    cond_y, cond_y refers back to cond_x -- alphabetical order (x, y, z) would
    invent an edge (x -> y) that does not exist in the config.
    """
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_x: {condition: ref, name: cond_z}
  cond_z: {condition: ref, name: cond_y}
  cond_y: {condition: ref, name: cond_x}
values: {}
rules:
  den.z:
    - {then: {position: 0}}
"""
    problems = validate(load_config(text))
    messages = [p.message for p in problems if p.code == "circular_condition_ref"]
    assert len(messages) == 1
    assert "cond_x -> cond_z -> cond_y -> cond_x" in messages[0]
    assert "cond_x -> cond_y -> cond_z -> cond_x" not in messages[0]


def test_long_acyclic_chain_does_not_blow_the_recursion_limit():
    """2000 conditions chained a -> b -> ... is legal and must validate, not crash."""
    n = 2000
    names = [f"c{i}" for i in range(n)]
    lines = [f"  {names[i]}: {{condition: ref, name: {names[i + 1]}}}" for i in range(n - 1)]
    lines.append(f'  {names[-1]}: {{condition: state, entity_id: x, state: "on"}}')
    text = f"""
blinds:
  - {{entity: cover.a}}
zones:
  z: {{members: [cover.a]}}
modes:
  - {{id: den}}
conditions:
{chr(10).join(lines)}
values: {{}}
rules:
  den.z:
    - {{then: {{position: 0}}}}
"""
    problems = validate(load_config(text))
    assert "circular_condition_ref" not in {p.code for p in problems}


def test_diamond_shaped_references_are_not_a_cycle():
    """A references both b and c; b and c both reference d. Shared and revisited,
    but acyclic -- must not be flagged. The false-positive case that matters most:
    a detector that blocks legitimate configs is worse than no detector.
    """
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  a: {condition: and, conditions: [{condition: ref, name: b}, {condition: ref, name: c}]}
  b: {condition: ref, name: d}
  c: {condition: ref, name: d}
  d: {condition: state, entity_id: x, state: "on"}
values: {}
rules:
  den.z:
    - {if: !ref a, then: {position: 100}}
    - {then: {position: 0}}
"""
    assert "circular_condition_ref" not in codes(text)


def test_cycle_nested_inside_a_not_condition_is_an_error():
    """Cycle detected when ref is nested inside a 'not' condition's conditions list."""
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_a: {condition: "not", conditions: [{condition: ref, name: cond_b}]}
  cond_b: {condition: ref, name: cond_a}
values: {}
rules:
  den.z:
    - {if: !ref cond_a, then: {position: 100}}
    - {then: {position: 0}}
"""
    assert "circular_condition_ref" in codes(text)


def test_cycle_nested_inside_a_bare_list_condition_body_is_an_error():
    """A named condition's body may itself be a bare list (the parser treats a
    list at the top level of a named condition as AND). A cycle hidden inside
    that list must still be caught.
    """
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_a: [{condition: ref, name: cond_b}, {condition: state, entity_id: x, state: "on"}]
  cond_b: {condition: ref, name: cond_a}
values: {}
rules:
  den.z:
    - {then: {position: 0}}
"""
    assert "circular_condition_ref" in codes(text)


def test_a_three_condition_cycle_yields_exactly_one_problem():
    """Regression guard: reporting one Problem per cycle *member* instead of
    per cycle would still satisfy every membership-only assertion above.
    """
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_a: {condition: ref, name: cond_b}
  cond_b: {condition: ref, name: cond_c}
  cond_c: {condition: ref, name: cond_a}
values: {}
rules:
  den.z:
    - {then: {position: 0}}
"""
    problems = [p for p in validate(load_config(text)) if p.code == "circular_condition_ref"]
    assert len(problems) == 1


def test_two_independent_cycles_yield_exactly_two_problems():
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_a: {condition: ref, name: cond_b}
  cond_b: {condition: ref, name: cond_a}
  cond_c: {condition: ref, name: cond_d}
  cond_d: {condition: ref, name: cond_e}
  cond_e: {condition: ref, name: cond_c}
values: {}
rules:
  den.z:
    - {then: {position: 0}}
"""
    problems = [p for p in validate(load_config(text)) if p.code == "circular_condition_ref"]
    assert len(problems) == 2


def test_unknown_ref_inside_a_named_condition_is_an_error():
    """A hand-written literal ref (not the !ref tag) bypasses parse-time checking."""
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}\n'
        "  bad: {condition: ref, name: neexistuje}",
    )
    assert "unknown_condition_ref" in codes(text)


def test_unknown_ref_in_a_rule_if_is_an_error():
    text = BASE.replace(
        "    - {if: !ref vzdy, then: {position: 100}}",
        "    - {if: {condition: ref, name: neexistuje}, then: {position: 100}}",
    )
    assert "unknown_condition_ref" in codes(text)


def test_all_refs_resolved_does_not_trigger_unknown_condition_ref():
    assert "unknown_condition_ref" not in codes(BASE)


# --- issue #3: condition body shape ------------------------------------------


def test_unknown_condition_type_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        'conditions:\n  vzdy: {condition: sate, entity_id: x, state: "on"}',
    )
    assert "bad_condition_shape" in codes(text)


def test_numeric_state_missing_default_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: numeric_state, entity_id: sensor.t, above: 25}",
    )
    assert "bad_condition_shape" in codes(text)


def test_state_missing_state_key_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: state, entity_id: input_boolean.x}",
    )
    assert "bad_condition_shape" in codes(text)


def test_state_missing_entity_id_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        'conditions:\n  vzdy: {condition: state, state: "on"}',
    )
    assert "bad_condition_shape" in codes(text)


def test_numeric_state_missing_above_and_below_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: numeric_state, entity_id: sensor.t, default: 5}",
    )
    assert "bad_condition_shape" in codes(text)


def test_time_missing_after_and_before_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: time}",
    )
    assert "bad_condition_shape" in codes(text)


def test_template_missing_value_template_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: template}",
    )
    assert "bad_condition_shape" in codes(text)


def test_and_missing_conditions_key_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: and}",
    )
    assert "bad_condition_shape" in codes(text)


def test_ref_missing_name_is_an_error():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}\n'
        "  bad: {condition: ref}",
    )
    assert "bad_condition_shape" in codes(text)


def test_sun_hits_target_and_event_targets_zone_have_no_required_keys():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        "conditions:\n  vzdy: {condition: sun_hits_target}\n"
        "  vzdy2: {condition: event_targets_zone}",
    )
    problems = validate(load_config(text))
    assert not any(p.code == "bad_condition_shape" for p in problems)


def test_a_valid_condition_body_reports_no_shape_problem():
    assert "bad_condition_shape" not in codes(BASE)


def test_extra_unknown_key_on_a_condition_body_is_not_reported():
    """Condition bodies may carry keys this dialect ignores (`alias`,
    `enabled`, whatever Home Assistant adds next) -- only unknown types and
    missing required keys are errors.
    """
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on", '
        'alias: "my condition", enabled: true, some_future_key: 1}',
    )
    assert "bad_condition_shape" not in codes(text)


def test_bad_condition_shape_message_names_the_offending_condition():
    text = BASE.replace(
        'conditions:\n  vzdy: {condition: state, entity_id: x, state: "on"}',
        'conditions:\n  vzdy: {condition: sate, entity_id: x, state: "on"}',
    )
    problems = [p for p in validate(load_config(text)) if p.code == "bad_condition_shape"]
    assert len(problems) == 1
    assert "vzdy" in problems[0].message
    assert "sate" in problems[0].message


# ---------------------------------------------------------------------------
# `Problem.owners`: which specific subentry a `condition`-body problem is
# attributable to, not just which subentry *type*. This is the pure-Python
# half of the fix for the "coarse type-level owner blocks an unrelated save"
# defect -- `subentry_flow._blocks_on` (HA-only, driven in
# `tests/ha/test_subentry_flows.py`) is the reader; these prove the producer
# side, runnable without `homeassistant` at all. A condition body can
# originate in three different *places* sharing the same three types
# (`condition`/`mode`/`rule`, see `_CODE_OWNERS`'s own comment in
# `subentry_flow.py`), so knowing the type alone is not enough to say which
# specific subentry save could fix it -- `owners` names the exact one(s).
# ---------------------------------------------------------------------------


def test_unknown_ref_owner_is_the_condition_that_holds_it():
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  a: {condition: ref, name: neexistuje}
values: {}
rules:
  den.z:
    - {then: {position: 0}}
"""
    problems = [p for p in validate(load_config(text)) if p.code == "unknown_condition_ref"]
    assert len(problems) == 1
    assert problems[0].owners == frozenset({("condition", "a")})


def test_unknown_ref_owner_is_the_mode_that_holds_it_not_an_unrelated_one():
    """Two modes exist; only `slnecno`'s `when` has the dangling ref. Its
    owner must name `slnecno` specifically, not `bezny` too -- otherwise a
    save of the unrelated mode would be blocked by a problem it cannot fix.
    """
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: slnecno, when: {condition: ref, name: neexistuje}}
  - {id: bezny}
conditions: {}
values: {}
rules:
  slnecno.z:
    - {then: {position: 0}}
  bezny.z:
    - {then: {position: 0}}
"""
    problems = [p for p in validate(load_config(text)) if p.code == "unknown_condition_ref"]
    assert len(problems) == 1
    assert problems[0].owners == frozenset({("mode", "slnecno")})


def test_unknown_ref_owner_is_the_rule_that_holds_it():
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions: {}
values: {}
rules:
  den.z:
    - {if: {condition: ref, name: neexistuje}, then: {position: 100}}
    - {then: {position: 0}}
"""
    problems = [p for p in validate(load_config(text)) if p.code == "unknown_condition_ref"]
    assert len(problems) == 1
    assert problems[0].owners == frozenset({("rule", "den.z#0")})


def test_bad_condition_shape_owner_is_the_holding_condition_at_any_nesting_depth():
    """The malformed node is nested inside an `and`, not the condition's own
    top-level shape -- `owners` must still point at the whole subentry that
    holds it (`_check_condition_shape` is called per-node, but every node
    under one site shares that site's single owner).
    """
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  a: {condition: and, conditions: [{condition: sate, entity_id: x, state: "on"}]}
values: {}
rules:
  den.z:
    - {then: {position: 0}}
"""
    problems = [p for p in validate(load_config(text)) if p.code == "bad_condition_shape"]
    assert len(problems) == 1
    assert problems[0].owners == frozenset({("condition", "a")})


def test_circular_condition_ref_owner_names_every_member_of_the_cycle():
    """Any single member of a cycle can break it by editing its own outgoing
    ref -- so every name on the cycle is an owner, not just one of them.
    """
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  cond_a: {condition: ref, name: cond_b}
  cond_b: {condition: ref, name: cond_a}
values: {}
rules:
  den.z:
    - {then: {position: 0}}
"""
    problems = [p for p in validate(load_config(text)) if p.code == "circular_condition_ref"]
    assert len(problems) == 1
    assert problems[0].owners == frozenset({("condition", "cond_a"), ("condition", "cond_b")})


# ---------------------------------------------------------------------------
# `guards:`
#
# The guard schema exists because the house being migrated re-implements one
# rule -- "do not drive a door blind down onto an open door or a running
# sauna" -- seven times, in seven slightly different ways. Validation is what
# stops the single re-implementation from acquiring the same defects the
# seven had: a `defer` that never says what to do when its wait runs out, a
# `max_wait` on a policy that ignores it (which is live in the house today as
# `continue_on_timeout: true` with no `timeout` key at all), a target naming
# a blind that no longer exists, a guard written after another that already
# answers for it.
# ---------------------------------------------------------------------------

GUARD_BASE = (
    BASE
    + """
guards:
"""
)


def guard_codes(body: str) -> set[str]:
    return codes(GUARD_BASE + body)


def test_a_config_with_a_well_formed_guard_has_no_problems():
    assert validate(load_config(GUARD_BASE + "  - {policy: skip, applies_to: closing}\n")) == []


def test_an_unknown_policy_is_an_error():
    assert "guard_unknown_policy" in guard_codes("  - {policy: block}\n")


def test_an_unknown_policy_suppresses_the_per_policy_field_checks():
    """One wrong word, one message.

    A guard with `policy: blok` and a `max_wait` is not *also* guilty of
    putting `max_wait` on a policy that ignores it -- nobody knows yet
    whether the intended policy ignores it. Reporting both would send the
    reader to fix the field rather than the typo.
    """
    assert guard_codes("  - {policy: blok, max_wait: 90}\n") == {"guard_unknown_policy"}


def test_a_defer_without_on_timeout_is_an_error():
    """The hardest single finding in the inventory.

    The house has both variants and they do opposite things: two 3-hour waits
    that abandon the rest of the sequence on timeout, and 90-second waits that
    proceed anyway. Any default here silently picks one of two opposites, so
    the key is required and the message says why.
    """
    problems = [
        p
        for p in validate(load_config(GUARD_BASE + "  - {policy: defer, max_wait: 90}\n"))
        if p.code == "guard_defer_needs_timeout"
    ]
    assert len(problems) == 1
    assert "on_timeout" in problems[0].message
    assert "opposite" in problems[0].message


def test_a_defer_without_max_wait_is_an_error():
    problems = [
        p
        for p in validate(load_config(GUARD_BASE + "  - {policy: defer, on_timeout: proceed}\n"))
        if p.code == "guard_defer_needs_timeout"
    ]
    assert len(problems) == 1
    assert "max_wait: null" in problems[0].message


def test_max_wait_null_is_a_stated_value_not_a_missing_key():
    """Two of the house's five defers wait without a limit on purpose.

    `max_wait: null` must therefore satisfy the requirement that a `defer`
    states its wait -- if it did not, the only way to express "wait as long
    as it takes" would be to leave the key out, which is exactly what a guard
    that forgot to state anything looks like.
    """
    assert (
        validate(
            load_config(GUARD_BASE + "  - {policy: defer, max_wait: null, on_timeout: abandon}\n")
        )
        == []
    )


def test_an_unknown_on_timeout_is_an_error():
    codes_found = guard_codes("  - {policy: defer, max_wait: 90, on_timeout: maybe}\n")
    assert "guard_defer_needs_timeout" in codes_found


def test_a_defer_field_on_a_policy_that_ignores_it_is_an_error():
    """Dead configuration that reads as if it does something.

    `automation.spalna_dvere` in the house carries `continue_on_timeout: true`
    with no `timeout` key: Home Assistant ignores it, the wait is unlimited,
    and the line that looks like it bounds the wait does nothing at all. A
    schema that silently accepted the mirror image of that would reproduce the
    same class of defect.
    """
    for field in ("max_wait: 90", "on_timeout: proceed", "recheck_every: 60"):
        assert "guard_unused_field" in guard_codes(f"  - {{policy: skip, {field}}}\n"), field


def test_a_force_without_an_action_is_an_error():
    assert "guard_force_needs_action" in guard_codes("  - {policy: force}\n")


def test_a_force_with_an_action_is_accepted():
    assert validate(load_config(GUARD_BASE + "  - {policy: force, then: {position: 100}}\n")) == []


def test_an_action_on_a_policy_that_imposes_nothing_is_an_error():
    assert "guard_unused_field" in guard_codes("  - {policy: skip, then: {position: 100}}\n")


def test_an_unknown_direction_is_an_error():
    assert "guard_bad_direction" in guard_codes("  - {policy: skip, applies_to: down}\n")


def test_an_unknown_stage_is_an_error():
    assert "guard_bad_stage" in guard_codes("  - {policy: skip, stage: later}\n")


def test_a_target_naming_neither_a_blind_nor_a_zone_is_an_error():
    problems = [
        p
        for p in validate(load_config(GUARD_BASE + "  - {policy: skip, targets: [cover.ghost]}\n"))
        if p.code == "guard_unknown_target"
    ]
    assert len(problems) == 1
    assert "cover.ghost" in problems[0].message


def test_a_target_may_be_a_blind_or_a_zone():
    assert validate(load_config(GUARD_BASE + "  - {policy: skip, targets: [z, cover.a]}\n")) == []


def test_a_guard_an_earlier_unconditional_one_covers_can_never_fire():
    """Order is the only referee guards have.

    First match wins, exactly as for rules, so a guard written after an
    unconditional one covering the same blinds is dead -- and unlike a dead
    rule, a dead safety interlock looks present in the config right up until
    the day it was supposed to fire.
    """
    problems = [
        p
        for p in validate(
            load_config(
                GUARD_BASE
                + "  - {policy: skip, targets: [z]}\n"
                + "  - {policy: force, targets: [cover.a], then: {position: 100}}\n"
            )
        )
        if p.code == "guard_unreachable"
    ]
    assert len(problems) == 1
    assert problems[0].severity == WARNING
    assert "guard #1" in problems[0].message


def test_an_earlier_conditional_guard_does_not_shadow_a_later_one():
    assert (
        validate(
            load_config(
                GUARD_BASE
                + "  - {policy: skip, targets: [z], when: !ref vzdy}\n"
                + "  - {policy: skip, targets: [cover.a]}\n"
            )
        )
        == []
    )


def test_an_earlier_guard_at_the_other_stage_does_not_shadow_a_later_one():
    """An `input` guard removes a target before the engine is asked; an
    `output` guard overrides the answer it gave. They are asked at different
    moments, so neither can hide the other however broadly it is written.
    """
    assert (
        validate(
            load_config(
                GUARD_BASE
                + "  - {policy: skip, stage: input}\n"
                + "  - {policy: skip, stage: output}\n"
            )
        )
        == []
    )


def test_an_earlier_opening_guard_does_not_shadow_a_later_closing_one():
    assert (
        validate(
            load_config(
                GUARD_BASE
                + "  - {policy: skip, applies_to: opening}\n"
                + "  - {policy: skip, applies_to: closing}\n"
            )
        )
        == []
    )


def test_an_earlier_any_direction_guard_does_shadow_a_later_closing_one():
    assert "guard_unreachable" in guard_codes(
        "  - {policy: skip, applies_to: any}\n  - {policy: skip, applies_to: closing}\n"
    )


def test_a_guard_naming_an_unknown_target_does_not_widen_its_own_shadow():
    """A typo must not make the guard before it look broader than it is.

    The first guard here names one blind that exists and one that does not.
    If the unknown name were treated as a blind, the second guard would be
    reported unreachable on the strength of a target that is itself already
    an error -- two problems for one typo, the second of them wrong.
    """
    text = (
        GUARD_BASE
        + "  - {policy: skip, targets: [cover.ghost]}\n"
        + "  - {policy: skip, targets: [cover.a]}\n"
    )
    assert {p.code for p in validate(load_config(text))} == {"guard_unknown_target"}


def test_a_dangling_condition_ref_inside_a_guards_when_is_an_error():
    """A guard's `when` is checked by the same passes every other condition
    slot is, not by a guard-specific copy of them.
    """
    problems = [
        p
        for p in validate(
            load_config(GUARD_BASE + "  - {policy: skip, when: {condition: ref, name: nope}}\n")
        )
        if p.code == "unknown_condition_ref"
    ]
    assert len(problems) == 1
    assert problems[0].owners == frozenset({("guard", "guard#0")})


def test_a_broken_condition_shape_inside_a_guards_when_is_an_error():
    problems = [
        p
        for p in validate(
            load_config(GUARD_BASE + "  - {policy: skip, when: {condition: nonsense}}\n")
        )
        if p.code == "bad_condition_shape"
    ]
    assert len(problems) == 1
    assert problems[0].owners == frozenset({("guard", "guard#0")})


def test_a_guard_problem_names_the_guard_by_position_and_name():
    problems = [
        p
        for p in validate(load_config(GUARD_BASE + "  - {name: wind, policy: force}\n"))
        if p.code == "guard_force_needs_action"
    ]
    assert len(problems) == 1
    assert problems[0].message.startswith("guard #0 'wind'")
    assert problems[0].owners == frozenset({("guard", "guard#0")})


TILTLESS = """
blinds:
  - {entity: cover.a, has_tilt: false}
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
values: {}
rules:
  den.z:
    - {then: {position: 0, tilt: 50}}
"""


def test_setting_tilt_on_a_blind_without_tilt_is_a_warning():
    """`planner.plan` gates the tilt half on `has_tilt`, so it is simply never sent.

    That is the right runtime behaviour -- there is no slat -- but until this
    check nothing told the author, so a rule that reads as if it sets the slats
    quietly did not. Every blind in this house has tilt, so the case that
    matters is a foreign install, which is the whole point of phase 7.
    """
    problems = validate(load_config(TILTLESS))

    assert [p.code for p in problems] == ["tilt_on_tiltless_blind"]
    assert problems[0].severity == WARNING
    assert "cover.a" in problems[0].message
    assert problems[0].owners == frozenset({("rule", "den.z#0")})


def test_keeping_tilt_on_a_blind_without_tilt_is_silent():
    """`tilt: keep` asks for nothing, so there is nothing to warn about.

    The counter-test: without it the one above would pass on a check that
    fires for every rule regardless of what it asks for.
    """
    text = TILTLESS.replace("tilt: 50", "tilt: keep")

    assert validate(load_config(text)) == []


def test_tilt_is_not_reported_for_a_blind_that_has_it():
    text = TILTLESS.replace("has_tilt: false", "has_tilt: true")

    assert validate(load_config(text)) == []


def test_a_mode_default_list_names_the_tiltless_blinds_it_reaches():
    """A default list reaches every zone, so the message has to say which blinds.

    Filed under the default key, its own `owners` still points at that list --
    the form that can fix it -- while the message names the blinds the author
    would otherwise have to go and find.
    """
    text = TILTLESS.replace("  den.z:", '  "den.*":')

    problems = validate(load_config(text))

    assert [p.code for p in problems] == ["tilt_on_tiltless_blind"]
    assert "cover.a" in problems[0].message
    assert "default list" in problems[0].message


def test_a_broken_ownership_map_does_not_make_validate_raise():
    """`resolve_ownership` raises on an orphan; `validate` must still return problems.

    Regression: the tilt check called it directly and turned two ownership
    errors into an exception -- breaking `validate` on exactly the class of
    configuration it exists to report on.
    """
    orphan = TILTLESS.replace("z: {members: [cover.a]}", "z: {members: []}")

    codes_found = {p.code for p in validate(load_config(orphan))}

    assert "blind_without_zone" in codes_found
    assert "tilt_on_tiltless_blind" not in codes_found
