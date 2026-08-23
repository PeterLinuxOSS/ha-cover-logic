from __future__ import annotations

from cover_logic.config_schema import load_config
from cover_logic.validation import validate

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
    text = BASE.replace("blinds:\n  - {entity: cover.a}",
                        "blinds:\n  - {entity: cover.a}\n  - {entity: cover.orphan}")
    assert "blind_without_zone" in codes(text)


def test_zone_referring_to_an_unknown_blind_is_an_error():
    text = BASE.replace("members: [cover.a]", "members: [cover.a, cover.ghost]")
    assert "zone_member_unknown" in codes(text)


def test_blind_in_two_zones_is_an_error():
    text = BASE.replace("  z: {members: [cover.a]}",
                        "  z: {members: [cover.a]}\n  z2: {members: [cover.a]}")
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
    text = BASE.replace("  den.z:\n    - {if: !ref vzdy, then: {position: 100}}\n    - {then: {position: 0}}", "  {}")
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


def test_direct_self_referencing_condition_is_an_error():
    """A condition that directly refers to itself."""
    text = BASE.replace(
        "conditions:\n  vzdy: {condition: state, entity_id: x, state: \"on\"}",
        "conditions:\n  vzdy: {condition: ref, name: vzdy}",
    )
    assert "circular_condition_ref" in codes(text)


def test_two_condition_cycle_is_an_error():
    """Condition A refers to B, B refers to A."""
    text = BASE.replace(
        "conditions:\n  vzdy: {condition: state, entity_id: x, state: \"on\"}",
        "conditions:\n  vzdy: {condition: ref, name: niekedy}\n  niekedy: {condition: ref, name: vzdy}",
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
        "conditions:\n  vzdy: {condition: state, entity_id: x, state: \"on\"}",
        "conditions:\n  vzdy: {condition: state, entity_id: x, state: \"on\"}\n  bad: {condition: ref, name: bad}",
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
    """a references both b and c; b and c both reference d. Shared and revisited,
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
        "conditions:\n  vzdy: {condition: state, entity_id: x, state: \"on\"}",
        "conditions:\n  vzdy: {condition: state, entity_id: x, state: \"on\"}\n"
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
