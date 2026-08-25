from __future__ import annotations

import re

import pytest

from cover_logic.conditions import evaluate_condition
from cover_logic.config_schema import ConfigError, load_config, load_config_file
from cover_logic.model import KEEP, Action, Ref
from cover_logic.world import World

MINIMAL = """
blinds:
  - entity: cover.a
    facade_azimuth: 270
zones:
  terasa:
    members: [cover.a]
modes:
  - {id: noc, when: !ref cover_down}
  - {id: bezny_den}
conditions:
  cover_down:
    condition: state
    entity_id: input_boolean.cover_down
    state: "on"
values:
  kvety_poz:
    entity: input_number.kvety_pozicia_zaluzie
    default: 34
rules:
  noc.terasa:
    - {then: {position: keep, tilt: keep}}
  bezny_den.terasa:
    - {if: !ref cover_down, then: {position: 100, tilt: keep}}
    - {then: {position: !ref kvety_poz, tilt: 0}}
"""


def test_blinds_are_keyed_by_entity_id():
    cfg = load_config(MINIMAL)
    assert set(cfg.blinds) == {"cover.a"}
    assert cfg.blinds["cover.a"].facade_azimuth == 270.0


def test_zone_members_become_a_tuple():
    cfg = load_config(MINIMAL)
    assert cfg.zones["terasa"].members == ("cover.a",)


def test_modes_keep_their_order_and_the_last_one_is_the_fallback():
    cfg = load_config(MINIMAL)
    assert [m.id for m in cfg.modes] == ["noc", "bezny_den"]
    assert cfg.modes[-1].when is None


def test_ref_in_a_condition_slot_becomes_a_ref_condition():
    cfg = load_config(MINIMAL)
    assert cfg.modes[0].when == {"condition": "ref", "name": "cover_down"}


def test_ref_in_an_action_slot_becomes_a_value_ref():
    cfg = load_config(MINIMAL)
    rule = cfg.rules["bezny_den.terasa"][1]
    assert rule.then == Action(
        position=Ref(entity="input_number.kvety_pozicia_zaluzie", default=34),
        tilt=0,
    )


def test_keep_parses_to_the_keep_sentinel():
    cfg = load_config(MINIMAL)
    assert cfg.rules["noc.terasa"][0].then == Action(position=KEEP, tilt=KEEP)


def test_a_rule_without_if_has_no_condition():
    cfg = load_config(MINIMAL)
    assert cfg.rules["noc.terasa"][0].when is None


def test_missing_axis_defaults_to_keep():
    cfg = load_config(
        MINIMAL.replace("{then: {position: 100, tilt: keep}}", "{then: {position: 100}}")
    )
    assert cfg.rules["bezny_den.terasa"][0].then == Action(position=100, tilt=KEEP)


def test_unknown_value_ref_raises_config_error():
    bad = MINIMAL.replace("!ref kvety_poz", "!ref nope")
    with pytest.raises(ConfigError, match="nope"):
        load_config(bad)


def test_out_of_range_position_raises_config_error():
    bad = MINIMAL.replace("position: 100, tilt: keep", "position: 140, tilt: keep")
    with pytest.raises(ConfigError, match=re.escape("0..100")):
        load_config(bad)


def test_rule_events_become_a_frozenset():
    cfg = load_config(
        MINIMAL.replace(
            "- {then: {position: !ref kvety_poz, tilt: 0}}",
            "- {events: [arrival], then: {position: !ref kvety_poz, tilt: 0}}",
        )
    )
    assert cfg.rules["bezny_den.terasa"][1].events == frozenset({"arrival"})


# --- Fix 1: !ref inside a named condition's own body -----------------------

COMPOSED_CONDITIONS = """
conditions:
  vietor_ok:
    condition: state
    entity_id: binary_sensor.wind
    state: "off"
  pocasie_otvorene:
    condition: and
    conditions:
      - {condition: state, entity_id: sun.sun, state: above_horizon}
      - !ref vietor_ok
"""


def test_ref_inside_a_condition_body_resolves_to_a_ref_condition():
    cfg = load_config(COMPOSED_CONDITIONS)
    assert cfg.conditions["pocasie_otvorene"] == {
        "condition": "and",
        "conditions": [
            {"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"},
            {"condition": "ref", "name": "vietor_ok"},
        ],
    }


def test_ref_inside_a_condition_body_evaluates_end_to_end():
    cfg = load_config(COMPOSED_CONDITIONS)
    cond = cfg.conditions["pocasie_otvorene"]
    world_true = World(states={"sun.sun": "above_horizon", "binary_sensor.wind": "off"})
    world_false = World(states={"sun.sun": "above_horizon", "binary_sensor.wind": "on"})
    assert evaluate_condition(cond, world_true, registry=cfg.conditions) is True
    assert evaluate_condition(cond, world_false, registry=cfg.conditions) is False


def test_ref_inside_a_condition_body_to_unknown_name_raises_config_error():
    bad = COMPOSED_CONDITIONS.replace("!ref vietor_ok", "!ref neexistuje")
    with pytest.raises(ConfigError, match="neexistuje"):
        load_config(bad)


def test_mutually_recursive_condition_refs_parse_without_hanging():
    text = """
conditions:
  a:
    condition: and
    conditions:
      - !ref b
  b:
    condition: and
    conditions:
      - !ref a
"""
    cfg = load_config(text)
    assert cfg.conditions["a"] == {
        "condition": "and",
        "conditions": [{"condition": "ref", "name": "b"}],
    }
    assert cfg.conditions["b"] == {
        "condition": "and",
        "conditions": [{"condition": "ref", "name": "a"}],
    }


# --- Fix 3: unknown keys must raise, but not inside condition bodies/guards -

def test_unknown_top_level_key_raises_config_error():
    bad = MINIMAL.replace("blinds:", "blindz:")
    with pytest.raises(ConfigError, match="blindz"):
        load_config(bad)


def test_unknown_key_inside_blind_entry_raises_config_error():
    bad = MINIMAL.replace(
        "  - entity: cover.a\n    facade_azimuth: 270",
        "  - entity: cover.a\n    facade_azimuth: 270\n    tolerence: 5",
    )
    with pytest.raises(ConfigError, match="tolerence"):
        load_config(bad)


def test_condition_body_with_arbitrary_ha_keys_still_parses():
    text = MINIMAL.replace(
        '    entity_id: input_boolean.cover_down\n    state: "on"',
        '    entity_id: input_boolean.cover_down\n    state: "on"\n'
        "    for: {seconds: 5}\n    attribute: some_attr",
    )
    cfg = load_config(text)
    assert cfg.conditions["cover_down"]["for"] == {"seconds": 5}
    assert cfg.conditions["cover_down"]["attribute"] == "some_attr"


# --- Fix 2: _parse_axis must reject bools and non-integral floats ----------

def test_position_true_raises_config_error_instead_of_becoming_one():
    bad = MINIMAL.replace("position: 100, tilt: keep", "position: true, tilt: keep")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_position_false_raises_config_error():
    bad = MINIMAL.replace("position: 100, tilt: keep", "position: false, tilt: keep")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_position_non_integral_float_raises_config_error():
    bad = MINIMAL.replace("position: 100, tilt: keep", "position: 50.5, tilt: keep")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_position_integral_float_is_accepted():
    cfg = load_config(
        MINIMAL.replace("position: 100, tilt: keep", "position: 50.0, tilt: keep")
    )
    assert cfg.rules["bezny_den.terasa"][0].then.position == 50


def test_out_of_range_value_default_raises_config_error():
    """A `default` is a config-time constant exactly like a literal axis, so
    it must be range-checked identically -- an out-of-range fallback must not
    validate clean just because it is reached through `!ref` instead of
    written directly as `position: 250`.
    """
    bad = MINIMAL.replace(
        "entity: input_number.kvety_pozicia_zaluzie\n    default: 34",
        "entity: input_number.kvety_pozicia_zaluzie\n    default: 250",
    )
    with pytest.raises(ConfigError, match="kvety_poz"):
        load_config(bad)


# --- Fix 3 (continued): shape checks around the strict key checking --------

def test_unknown_key_inside_zone_entry_raises_config_error():
    bad = MINIMAL.replace("members: [cover.a]", "members: [cover.a]\n    extra: 1")
    with pytest.raises(ConfigError, match="extra"):
        load_config(bad)


def test_unknown_key_inside_rule_entry_raises_config_error():
    bad = MINIMAL.replace(
        "{then: {position: keep, tilt: keep}}",
        "{then: {position: keep, tilt: keep}, wrongkey: 1}",
    )
    with pytest.raises(ConfigError, match="wrongkey"):
        load_config(bad)


def test_unknown_key_inside_action_raises_config_error():
    bad = MINIMAL.replace(
        "{then: {position: keep, tilt: keep}}",
        "{then: {position: keep, tilt: keep, angle: 5}}",
    )
    with pytest.raises(ConfigError, match="angle"):
        load_config(bad)


# --- Fix 4: malformed entries raise ConfigError, not raw exceptions --------

def test_zone_entry_as_a_list_raises_config_error_not_attribute_error():
    bad = MINIMAL.replace(
        "zones:\n  terasa:\n    members: [cover.a]",
        "zones:\n  terasa: [cover.a]",
    )
    with pytest.raises(ConfigError):
        load_config(bad)


def test_mode_entry_as_a_bare_string_raises_config_error_not_type_error():
    bad = MINIMAL.replace(
        "modes:\n  - {id: noc, when: !ref cover_down}\n  - {id: bezny_den}",
        "modes:\n  - noc\n  - {id: bezny_den}",
    )
    with pytest.raises(ConfigError):
        load_config(bad)


def test_blind_entry_as_a_bare_string_raises_config_error():
    bad = MINIMAL.replace(
        "blinds:\n  - entity: cover.a\n    facade_azimuth: 270",
        "blinds:\n  - cover.a",
    )
    with pytest.raises(ConfigError):
        load_config(bad)


# --- Minor coverage gaps -----------------------------------------------

def test_load_config_file_reads_and_parses_a_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL, encoding="utf-8")
    cfg = load_config_file(path)
    assert set(cfg.blinds) == {"cover.a"}


def test_value_only_name_referenced_from_a_condition_slot_raises_config_error():
    bad = MINIMAL.replace("when: !ref cover_down", "when: !ref kvety_poz")
    with pytest.raises(ConfigError, match="kvety_poz"):
        load_config(bad)


def test_condition_only_name_referenced_from_an_action_axis_raises_config_error():
    bad = MINIMAL.replace(
        "position: !ref kvety_poz", "position: !ref cover_down"
    )
    with pytest.raises(ConfigError, match="cover_down"):
        load_config(bad)


# --- A '.' in a mode or zone id would misroute rules ------------------

def test_zone_id_containing_dot_raises_config_error():
    """`engine` builds rule keys as f"{mode}.{zone}"; a dot inside the zone id
    itself makes that join ambiguous with a differently-split (mode, zone)
    pair that happens to produce the same string. Reject it at parse time.
    """
    bad = MINIMAL.replace("terasa:\n    members: [cover.a]", "b.c:\n    members: [cover.a]")
    with pytest.raises(ConfigError, match=r"b\.c"):
        load_config(bad)


def test_mode_id_containing_dot_raises_config_error():
    bad = MINIMAL.replace("{id: noc, when: !ref cover_down}", "{id: a.b, when: !ref cover_down}")
    with pytest.raises(ConfigError, match=r"a\.b"):
        load_config(bad)


def test_same_short_name_in_both_namespaces_resolves_independently():
    text = MINIMAL.replace(
        "conditions:\n  cover_down:",
        "conditions:\n  shared:\n    condition: state\n"
        '    entity_id: input_boolean.cover_down\n    state: "on"\n  cover_down:',
    ).replace(
        "values:\n  kvety_poz:",
        "values:\n  shared:\n    entity: input_number.shared_helper\n    default: 7\n  kvety_poz:",
    ).replace(
        "when: !ref cover_down", "when: !ref shared"
    ).replace(
        "position: !ref kvety_poz", "position: !ref shared"
    )
    cfg = load_config(text)
    assert cfg.modes[0].when == {"condition": "ref", "name": "shared"}
    rule = cfg.rules["bezny_den.terasa"][1]
    assert rule.then.position == Ref(entity="input_number.shared_helper", default=7)
