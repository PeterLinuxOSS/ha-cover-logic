import re

import pytest

from cover_logic.conditions import evaluate_condition
from cover_logic.config_schema import (
    ConfigError,
    dump_config,
    dump_config_file,
    load_config,
    load_config_file,
    referenced_entities,
)
from cover_logic.model import KEEP, UNSET, Action, Ref
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


# --- Fix 3: unknown keys must raise, but not inside condition bodies -------
# (Guards used to be exempt here too, for want of a schema; they are strictly
# key-checked now -- see the guards section at the end of this file.)


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
    cfg = load_config(MINIMAL.replace("position: 100, tilt: keep", "position: 50.0, tilt: keep"))
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
    bad = MINIMAL.replace("position: !ref kvety_poz", "position: !ref cover_down")
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


def test_zone_id_of_a_literal_asterisk_raises_config_error():
    """`"*"` in the zone half of a rules key is reserved to mean "this mode's
    default rule list" (`const.RULE_DEFAULT_ZONE`, read by `engine.evaluate`).
    A real zone allowed to claim that name would make its own key collide
    with the mode's default key and silently steal the default rules meant
    for every zone.
    """
    bad = MINIMAL.replace("terasa:\n    members: [cover.a]", '"*":\n    members: [cover.a]')
    with pytest.raises(ConfigError, match=r"\*"):
        load_config(bad)


def test_same_short_name_in_both_namespaces_resolves_independently():
    text = (
        MINIMAL.replace(
            "conditions:\n  cover_down:",
            "conditions:\n  shared:\n    condition: state\n"
            '    entity_id: input_boolean.cover_down\n    state: "on"\n  cover_down:',
        )
        .replace(
            "values:\n  kvety_poz:",
            "values:\n  shared:\n    entity: input_number.shared_helper\n"
            "    default: 7\n  kvety_poz:",
        )
        .replace("when: !ref cover_down", "when: !ref shared")
        .replace("position: !ref kvety_poz", "position: !ref shared")
    )
    cfg = load_config(text)
    assert cfg.modes[0].when == {"condition": "ref", "name": "shared"}
    rule = cfg.rules["bezny_den.terasa"][1]
    assert rule.then.position == Ref(entity="input_number.shared_helper", default=7)


# --- referenced_entities() (issue #8) ---------------------------------------

REFERENCED_ENTITIES_CONFIG = """
blinds:
  - {entity: cover.a, facade_azimuth: 180}
  - {entity: cover.b, facade_azimuth: 180}
zones:
  zone_one: {members: [cover.a]}
  zone_two: {members: [cover.b]}
modes:
  - {id: noc, when: !ref cover_down}
  - {id: den}
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
  noc.zone_one:
    - {then: {position: keep}}
  noc.zone_two:
    - {then: {position: keep}}
  den.zone_one:
    - if: {condition: state, entity_id: binary_sensor.dvere, attribute: is_open, state: "on"}
      then: {position: !ref kvety_poz}
    - if: {condition: sun_hits_target, azimuth_entity: sensor.az_one}
      then: {position: 0}
    - then: {position: 100}
  den.zone_two:
    - if: {condition: sun_hits_target, azimuth_entity: sensor.az_two, azimuth_attribute: azimuth}
      then: {position: 0}
    - then: {position: 100}
"""


def test_referenced_entities_covers_state_attribute_sun_overrides_and_values():
    """Covers, in one config: a plain state condition (named, reached through
    `!ref` from a mode's `when`), an attribute condition written inline in a
    rule's `if`, two `sun_hits_target` conditions with *different*
    `azimuth_entity` overrides (one of them also using `azimuth_attribute`),
    and a `values:` ref. `sun.sun` itself must appear too -- both
    `sun_hits_target` nodes read it via the default `sun_entity`.
    """
    cfg = load_config(REFERENCED_ENTITIES_CONFIG)
    assert referenced_entities(cfg) == {
        "input_boolean.cover_down",
        ("binary_sensor.dvere", "is_open"),
        "sun.sun",
        "sensor.az_one",
        ("sensor.az_two", "azimuth"),
        "input_number.kvety_pozicia_zaluzie",
    }


def test_referenced_entities_on_minimal_config_has_no_attribute_tuples():
    cfg = load_config(MINIMAL)
    assert referenced_entities(cfg) == {
        "input_boolean.cover_down",
        "input_number.kvety_pozicia_zaluzie",
    }


# --- dump_config / dump_config_file: the write side (task 5) ---------------
#
# `dump_config` is the inverse of `load_config`, used by the `export_config`
# service. The decisive test is always the round trip -- parse, dump,
# reparse, compare by object equality -- never a check of the dumped text's
# exact spelling, which would just re-encode an assumption about PyYAML's
# formatting choices (observed directly: it quotes a `!ref` scalar,
# `!ref 'cover_down'`, not the bare `!ref cover_down` a human would type) as
# if it were part of this project's contract. Text-shape assertions below are
# limited to the one thing that has to be true regardless of quoting: the
# `!ref` tag itself must appear, proving a ref did not silently degrade into
# a plain string it would need re-checking to notice.

ROUND_TRIP_TEXT = """
blinds:
  - entity: cover.a
    facade_azimuth: 270
    tolerance: 30
    travel_time: 45
    has_tilt: false
    tilt_after_arrival: false
  - entity: cover.b
zones:
  terasa:
    members: [cover.a]
    occupants: [peter, mimka]
  izba:
    members: [cover.b]
modes:
  - {id: noc, when: !ref cover_down}
  - {id: bezny_den}
conditions:
  cover_down:
    condition: state
    entity_id: input_boolean.cover_down
    state: "on"
  pocasie:
    condition: and
    conditions:
      - {condition: state, entity_id: sun.sun, state: above_horizon}
      - !ref cover_down
values:
  kvety_poz:
    entity: input_number.kvety_pozicia_zaluzie
    default: 34
rules:
  noc.terasa:
    - {then: {position: keep, tilt: keep}}
  bezny_den.terasa:
    - {if: !ref cover_down, then: {position: 100, tilt: keep}}
    - {events: [arrival], name: "kvety fallback", then: {position: !ref kvety_poz, tilt: 0}}
  bezny_den.izba:
    - {then: {position: keep, tilt: keep}}
guards:
  - name: do not drop the terrace blind onto an open door
    policy: skip
    applies_to: closing
    stage: input
    targets: [terasa]
    when: !ref cover_down
  - name: wait for the sauna
    policy: defer
    applies_to: closing
    stage: output
    targets: [cover.a]
    max_wait: null
    on_timeout: abandon
    recheck_every: 600
  - policy: force
    applies_to: any
    stage: output
    then: {position: !ref kvety_poz, tilt: keep}
"""


def test_dump_config_load_config_round_trips_every_shape():
    """The decisive test for the write side: parse, dump, reparse, compare by
    object equality -- not field by field, so a shape `dump_config` silently
    drops or mis-shapes cannot hide behind a partial assertion. Exercises
    every axis the inherited write side has to get right at once: a named
    mode ref (`when: !ref`) and a rule-level one (`if: !ref`) -- two
    different keys sharing the same `unparse_condition` walk but meaning
    different things per the `!ref` tag's context-sensitivity (see
    `config_schema.RefTag`'s own docstring) -- a `!ref` nested inside another
    condition's own body, a `!ref` value axis sitting next to a plain int and
    a `keep` axis, `events`, `name`, non-default blind fields, two zones (one
    with `occupants`), and three guards between them covering every guard
    field: a `!ref` condition in a `when`, a `!ref` value inside a `force`'s
    `then`, `targets` naming a zone and naming a blind, `max_wait: null`
    (which must survive as `null`, not as an absent key -- see
    `model.Unset`), an explicit `recheck_every`, and a guard with no `name`.
    """
    original = load_config(ROUND_TRIP_TEXT)
    reloaded = load_config(dump_config(original))
    assert reloaded == original


def test_dump_config_keeps_the_ref_tag_for_both_a_condition_and_a_value_slot():
    """Weaker than the round trip above, but points at the specific failure
    mode `MODELS.md`'s "!ref is context-sensitive" note warns about: a `!ref`
    that silently became a plain string would still often *parse* (as a
    literal state/value), just mean something else -- see
    `test_dump_config_round_trips_a_shared_name_used_as_both_a_condition_and_a_value_ref`
    below for the case where getting this wrong is otherwise invisible.
    """
    dumped = dump_config(load_config(ROUND_TRIP_TEXT))
    # mode's when, the nested condition ref, the rule's if, the value ref,
    # plus a guard's `when` (a condition ref) and a guard's `then` (a value
    # ref) -- the two slots a guard has that are `!ref`-capable, and the two
    # that would silently become plain strings if `guard_to_dict` forgot to
    # route them through `unparse_condition`/`unparse_axis`.
    assert dumped.count("!ref") == 6


def test_dump_config_writes_keep_as_the_string_keep_not_the_python_repr():
    """`KEEP` is a singleton (`model.Keep`), not a string -- see its own
    docstring. A dump that wrote `repr(KEEP)` (`"KEEP"`) or left the key out
    would both still produce a `Config` on reload (a missing axis defaults to
    `KEEP` too -- see `test_missing_axis_defaults_to_keep`), so only checking
    the dumped text catches a dump that silently relies on that default
    instead of writing `keep` explicitly.
    """
    dumped = dump_config(load_config(ROUND_TRIP_TEXT))
    assert "position: keep" in dumped
    assert "tilt: keep" in dumped


def test_dump_config_preserves_rule_order_within_a_group():
    """Rule order is semantics, not presentation (`MODELS.md` Sec. 3): the
    first rule in `bezny_den.terasa` must still be the `!ref cover_down` one
    after a round trip, not the fallback -- a tuple written out of order
    would silently change which rule the engine tries first, with no parse
    error to flag it.
    """
    reloaded = load_config(dump_config(load_config(ROUND_TRIP_TEXT)))
    rules = reloaded.rules["bezny_den.terasa"]
    assert rules[0].when == {"condition": "ref", "name": "cover_down"}
    assert rules[1].name == "kvety fallback"


def test_dump_config_round_trips_a_default_rule_key():
    """`"<mode>.*"` (`const.RULE_DEFAULT_ZONE`) is just another string key in
    `Config.rules` -- `dump_config`/`load_config` need no special case for
    it, but that is a claim worth pinning, not assuming: a round trip
    through YAML text must still carry the wildcard rule and its shape.
    """
    text = MINIMAL.replace(
        "rules:\n  noc.terasa:",
        'rules:\n  "noc.*":\n    - {then: {position: 0, tilt: 0}}\n  noc.terasa:',
    )
    original = load_config(text)
    reloaded = load_config(dump_config(original))
    assert reloaded == original
    assert reloaded.rules["noc.*"][0].then == Action(position=0, tilt=0)


def test_dump_config_round_trips_a_shared_name_used_as_both_a_condition_and_a_value_ref():
    """The write-side twin of `test_same_short_name_in_both_namespaces_resolves_independently`.

    `unparse_condition` and `unparse_axis` are two separate walks (see
    `RefTag`'s docstring: "the two namespaces are separate on purpose"), each
    called from exactly the key `_parse_condition`/`_parse_axis` would read
    on the way back in (`if`/`when` vs. `position`/`tilt`). If either walk
    were wired to the wrong namespace's ref lookup, or if a bug ever
    collapsed the two into one shared substitution, a name used in both
    namespaces at once is the one case where a plain round-trip-equality
    check could still coincidentally pass for the wrong reason (each
    namespace happens to have an entry for that name); asserting *which*
    slot resolved to *which* referent, post round trip, is what actually
    catches a mix-up.
    """
    text = (
        MINIMAL.replace(
            "conditions:\n  cover_down:",
            "conditions:\n  shared:\n    condition: state\n"
            '    entity_id: input_boolean.cover_down\n    state: "on"\n  cover_down:',
        )
        .replace(
            "values:\n  kvety_poz:",
            "values:\n  shared:\n    entity: input_number.shared_helper\n"
            "    default: 7\n  kvety_poz:",
        )
        .replace("when: !ref cover_down", "when: !ref shared")
        .replace("position: !ref kvety_poz", "position: !ref shared")
    )
    original = load_config(text)
    reloaded = load_config(dump_config(original))
    assert reloaded == original
    assert reloaded.modes[0].when == {"condition": "ref", "name": "shared"}
    rule = reloaded.rules["bezny_den.terasa"][1]
    assert rule.then.position == Ref(entity="input_number.shared_helper", default=7)


def test_dump_config_file_round_trips_through_an_actual_file(tmp_path):
    original = load_config(ROUND_TRIP_TEXT)
    path = tmp_path / "out.yaml"
    dump_config_file(path, original)
    assert load_config_file(path) == original


def test_dump_config_of_an_empty_config_reloads_to_an_equal_config():
    """The degenerate case: nothing configured at all still round-trips,
    rather than `dump_config` choking on empty dicts/tuples/`()`.
    """
    empty = load_config("{}")
    assert load_config(dump_config(empty)) == empty


# --- guards ------------------------------------------------------------------
# Shape only lives here; whether a guard makes *sense* (a policy that exists,
# a `defer` that says what to do on timeout, targets that name something real)
# is `validation.py`'s -- see `tests/test_validation.py`'s own guards section.
# The split matters: a house with one questionable guard must still load, so
# it can be repaired through the UI rather than leaving the integration unable
# to start.

GUARDS_BASE = """
blinds:
  - entity: cover.a
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
conditions:
  door_open: {condition: state, entity_id: binary_sensor.door, state: "on"}
values:
  poz: {entity: input_number.poz, default: 34}
rules:
  den.z:
    - {then: {position: keep}}
guards:
"""


def guards_of(body: str):
    return load_config(GUARDS_BASE + body).guards


def test_a_guard_entry_with_an_unknown_key_is_rejected():
    """Guards are strictly key-checked like every other structure this module
    owns the schema of. They used to be exempt only because the schema did not
    exist yet; a `polcy:` typo silently doing nothing is exactly the failure a
    safety interlock cannot afford.
    """
    with pytest.raises(ConfigError, match="unknown key"):
        guards_of("  - {policy: skip, polcy: skip}\n")


def test_a_guard_without_a_policy_is_rejected():
    with pytest.raises(ConfigError, match="without 'policy'"):
        guards_of("  - {applies_to: closing}\n")


def test_guards_must_be_a_list_of_mappings():
    with pytest.raises(ConfigError, match="guard entry must be a mapping"):
        guards_of("  - just_a_string\n")


def test_an_absent_max_wait_and_an_explicit_null_are_different_configurations():
    """The distinction decision 3 of the design brief turns on.

    `max_wait: null` means "wait as long as it takes", which two of the
    house's five defers genuinely mean; an absent `max_wait` means the author
    never said. Collapsing the two would make it impossible for `validation`
    to require that a `defer` states its wait, since the deliberate unlimited
    wait would be spelled the same way as the omission.
    """
    absent = guards_of("  - {policy: defer, on_timeout: proceed}\n")[0]
    explicit = guards_of("  - {policy: defer, max_wait: null, on_timeout: proceed}\n")[0]

    assert absent.max_wait is UNSET
    assert explicit.max_wait is None
    assert absent != explicit


def test_a_defer_always_carries_a_recheck_interval_even_when_unwritten():
    """Restart resilience is a property of the guard, not a second object.

    `wait_for_trigger` does not survive a restart, and of the house's five
    deferred waits exactly one has a watchdog automation paired with it by
    hand -- the bedroom's had to be built after the gap was found. So every
    parsed `defer` carries the interval a runner needs, whether its author
    wrote one or not, and a runner reading `guard.recheck_every` never has to
    invent a fallback of its own.
    """
    assert (
        guards_of("  - {policy: defer, max_wait: 90, on_timeout: proceed}\n")[0].recheck_every
        == 900
    )


def test_an_explicit_recheck_interval_is_not_overwritten_by_the_default():
    assert (
        guards_of("  - {policy: defer, max_wait: 90, on_timeout: proceed, recheck_every: 60}\n")[
            0
        ].recheck_every
        == 60
    )


def test_a_policy_that_holds_no_state_gets_no_recheck_interval():
    """A `skip` is re-decided from scratch every time the engine runs, so it
    has nothing a restart could lose and nothing to re-check.
    """
    assert guards_of("  - {policy: skip}\n")[0].recheck_every is None


def test_guard_defaults_are_the_widest_reading_not_the_narrowest():
    guard = guards_of("  - {policy: skip}\n")[0]
    assert guard.applies_to == "any"
    assert guard.stage == "output"
    assert guard.targets == ()
    assert guard.when is None


def test_a_guard_when_resolves_refs_like_every_other_condition_slot():
    guard = guards_of("  - {policy: skip, when: !ref door_open}\n")[0]
    assert guard.when == {"condition": "ref", "name": "door_open"}


def test_a_guard_when_naming_an_unknown_condition_is_rejected_at_parse_time():
    with pytest.raises(ConfigError, match="unknown condition ref"):
        guards_of("  - {policy: skip, when: !ref nope}\n")


def test_a_force_action_takes_a_value_ref_like_a_rule_does():
    guard = guards_of("  - {policy: force, then: {position: !ref poz}}\n")[0]
    assert guard.then == Action(position=Ref(entity="input_number.poz", default=34), tilt=KEEP)


def test_a_force_action_is_range_checked_like_a_rules_action():
    with pytest.raises(ConfigError, match=re.escape("action axis must be 0..100")):
        guards_of("  - {policy: force, then: {position: 250}}\n")


def test_a_wait_must_be_a_whole_non_negative_number_of_seconds():
    for bad in ("-1", "yes", "1.5", "'soon'"):
        with pytest.raises(ConfigError):
            guards_of(f"  - {{policy: defer, max_wait: {bad}, on_timeout: proceed}}\n")


def test_a_guard_target_must_be_a_string():
    with pytest.raises(ConfigError, match="guard target must be a string"):
        guards_of("  - {policy: skip, targets: [{entity: cover.a}]}\n")


def test_referenced_entities_includes_a_guards_own_condition_entities():
    """The coordinator subscribes to exactly what this reports.

    A door sensor named only by a guard and by nothing else would otherwise
    never wake the integration: the guard would be re-examined only when some
    unrelated entity happened to change, which for an interlock is
    indistinguishable from its not being there.
    """
    cfg = load_config(
        GUARDS_BASE
        + "  - {policy: skip, when: {condition: state, "
        + "entity_id: binary_sensor.sauna, state: 'on'}}\n"
    )
    assert "binary_sensor.sauna" in referenced_entities(cfg)


def test_referenced_entities_reaches_a_condition_nested_inside_a_guards_when():
    cfg = load_config(
        GUARDS_BASE
        + """  - policy: skip
    when:
      condition: or
      conditions:
        - {condition: state, entity_id: binary_sensor.door, state: "on"}
        - {condition: numeric_state, entity_id: sensor.sauna_temp, above: 50, default: 0}
"""
    )
    assert {"binary_sensor.door", "sensor.sauna_temp"} <= referenced_entities(cfg)
