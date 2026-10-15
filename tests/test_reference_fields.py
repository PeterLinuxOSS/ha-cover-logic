"""fields.yaml must describe exactly what the parser accepts, with the parser's defaults."""

import importlib.util
from pathlib import Path

import pytest
import yaml

from cover_logic import config_schema as cs

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


@pytest.mark.parametrize("field", ["tolerance", "travel_time", "has_tilt", "tilt_after_arrival"])
def test_blind_defaults_match_parser(field):
    blind = cs.load_config(_MINIMAL).blinds["cover.a"]
    assert getattr(blind, field) == _FIELDS["blinds"][field]["default"]


def test_manual_detection_hold_default_matches_parser():
    cfg = cs.load_config(_MINIMAL)
    assert cfg.manual_detection.hold == _FIELDS["manual_detection"]["hold"]["default"]


def test_slat_angle_scale_default_matches_parser():
    text = _MINIMAL.replace(
        "rules:",
        "values:\n  uhol:\n    type: slat_angle\n    default: 50\nrules:",
    )
    cfg = cs.load_config(text)
    assert cfg.values["uhol"].scale == _FIELDS["values_slat_angle"]["scale"]["default"]


def test_committed_reference_is_fresh():
    spec = importlib.util.spec_from_file_location("gen", _ROOT / "scripts/gen_reference.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    committed = (_ROOT / "docs/site/reference/configuration.md").read_text()
    assert committed == gen.render(_FIELDS), "run: python3 scripts/gen_reference.py"
