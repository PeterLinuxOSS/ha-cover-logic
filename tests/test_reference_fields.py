"""fields.yaml must describe exactly what the parser accepts, with the parser's defaults."""

import importlib.util
from pathlib import Path
import re

import pytest
import yaml

from cover_logic import config_schema as cs
from cover_logic.model import KEEP, UNSET

_ROOT = Path(__file__).parent.parent
_FIELDS = yaml.safe_load((_ROOT / "docs/site/reference/fields.yaml").read_text())
_SECTIONS = {
    "blinds": cs._BLIND_KEYS,
    "zones": cs._ZONE_KEYS,
    "values_entity": cs._VALUE_KEYS_ENTITY,
    "values_slat_angle": cs._VALUE_KEYS_SLAT,
    "modes": cs._MODE_KEYS,
    "rules": cs._RULE_KEYS,
    "action": cs._ACTION_KEYS,
    "guards": cs._GUARD_KEYS,
    "manual_detection": cs._MANUAL_DETECTION_KEYS,
}


@pytest.mark.parametrize("section", sorted(_SECTIONS))
def test_documented_keys_equal_parser_keys(section):
    assert set(_FIELDS[section]) == set(_SECTIONS[section])


_MINIMAL = """
blinds:
  - entity: cover.a
zones:
  z:
    members: [cover.a]
modes:
  - {id: m}
rules:
  m.z:
    - {then: {position: 0}}
"""


# --- Concrete-default equality: every optional field whose parser default is
# an actual value (not None/empty, meaning "unset"), checked against the live
# parser rather than trusted by eye -- this is what would have caught
# `azimuth_entity`'s wrong documented default (review round 1). Fields whose
# real default is None/empty (facade_azimuth, slat_distance, members, when,
# if, events, targets, azimuth_attribute, ...) are "none" in fields.yaml and
# are exercised by test_documented_keys_equal_parser_keys instead: there is
# no parser value to compare against, only the key's presence.


@pytest.mark.parametrize("field", ["tolerance", "travel_time", "has_tilt", "tilt_after_arrival"])
def test_blind_defaults_match_parser(field):
    blind = cs.load_config(_MINIMAL).blinds["cover.a"]
    assert getattr(blind, field) == _FIELDS["blinds"][field]["default"]


def test_manual_detection_hold_default_matches_parser():
    cfg = cs.load_config(_MINIMAL)
    assert cfg.manual_detection.hold == _FIELDS["manual_detection"]["hold"]["default"]


def test_values_entity_type_default_matches_parser():
    """Omitting `type` must parse identically to writing the documented default."""
    implicit = _MINIMAL + "values:\n  v:\n    entity: input_number.x\n    default: 10\n"
    explicit = _MINIMAL + (
        f"values:\n  v:\n    type: {_FIELDS['values_entity']['type']['default']}\n"
        "    entity: input_number.x\n    default: 10\n"
    )
    assert cs.load_config(implicit).values == cs.load_config(explicit).values


_SLAT_ANGLE_DOC = _MINIMAL + "values:\n  uhol:\n    type: slat_angle\n    default: 50\n"


@pytest.mark.parametrize(
    "field", ["scale", "sun_entity", "azimuth_entity", "elevation_entity", "elevation_attribute"]
)
def test_slat_angle_defaults_match_parser(field):
    value = cs.load_config(_SLAT_ANGLE_DOC).values["uhol"]
    assert getattr(value, field) == _FIELDS["values_slat_angle"][field]["default"]


def test_rule_name_default_matches_parser():
    rule = cs.load_config(_MINIMAL).rules["m.z"][0]
    assert rule.name == _FIELDS["rules"]["name"]["default"]


def test_action_position_and_tilt_default_to_keep():
    """A `then:` with neither axis set must resolve to the documented `keep` default."""
    text = _MINIMAL.replace("{then: {position: 0}}", "{then: {}}")
    rule = cs.load_config(text).rules["m.z"][0]
    assert _FIELDS["action"]["position"]["default"] == "keep"
    assert _FIELDS["action"]["tilt"]["default"] == "keep"
    assert rule.then.position is KEEP
    assert rule.then.tilt is KEEP


_GUARD_DOC = _MINIMAL + "guards:\n  - {policy: skip}\n"


@pytest.mark.parametrize("field", ["applies_to", "stage", "name"])
def test_guard_defaults_match_parser(field):
    guard = cs.load_config(_GUARD_DOC).guards[0]
    assert getattr(guard, field) == _FIELDS["guards"][field]["default"]


def test_guard_recheck_every_defaults_to_900_for_a_defer_that_omits_it():
    text = _MINIMAL + "guards:\n  - {policy: defer}\n"
    guard = cs.load_config(text).guards[0]
    assert guard.recheck_every == _FIELDS["guards"]["recheck_every"]["default"] == 900


def test_guard_max_wait_omitted_is_unset_not_a_value():
    """Omitting `max_wait` is UNSET (a `defer` without it is a validation.py error), not `null`."""
    guard = cs.load_config(_GUARD_DOC).guards[0]
    assert guard.max_wait is UNSET
    assert _FIELDS["guards"]["max_wait"]["default"] == "required for defer"


def _import_gen():
    spec = importlib.util.spec_from_file_location("gen", _ROOT / "scripts/gen_reference.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return gen


def test_committed_reference_is_fresh():
    committed = (_ROOT / "docs/site/reference/configuration.md").read_text()
    assert committed == _import_gen().render(_FIELDS), "run: python3 scripts/gen_reference.py"


def test_render_escapes_pipe_so_a_cell_does_not_split_the_table():
    fields = {section: {} for section in _SECTIONS}
    fields["blinds"] = {"entity": {"help": "a | b"}}
    rendered = _import_gen().render(fields)
    row = next(line for line in rendered.splitlines() if line.startswith("| `entity`"))
    assert "a \\| b" in row
    # 5 columns need exactly 6 *unescaped* pipes as delimiters; the literal
    # "|" from the help text must not count as a 7th.
    unescaped = re.findall(r"(?<!\\)\|", row)
    assert len(unescaped) == 6
