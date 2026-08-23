from __future__ import annotations

import pytest

from cover_logic.config_schema import ConfigError, load_config
from cover_logic.model import KEEP, Action, Ref

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
    with pytest.raises(ConfigError, match="0..100"):
        load_config(bad)


def test_rule_events_become_a_frozenset():
    cfg = load_config(
        MINIMAL.replace(
            "- {then: {position: !ref kvety_poz, tilt: 0}}",
            "- {events: [arrival], then: {position: !ref kvety_poz, tilt: 0}}",
        )
    )
    assert cfg.rules["bezny_den.terasa"][1].events == frozenset({"arrival"})
