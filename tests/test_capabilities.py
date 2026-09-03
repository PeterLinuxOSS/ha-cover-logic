"""`required_features`: what a configuration will actually ask of each blind.

Written because a clean install offered `cover.garage_door` (open and close,
no position, no tilt) as a blind and nothing refused it -- measured 2026-09-03
on a real Home Assistant. See `capabilities.py` for why the requirement is
derived from the values the rules ask for rather than from a fixed list.
"""

import pytest

from cover_logic.capabilities import (
    CLOSE,
    CLOSE_TILT,
    OPEN,
    OPEN_TILT,
    SET_POSITION,
    SET_TILT_POSITION,
    missing_features,
    required_features,
)
from cover_logic.config_schema import load_config

_HEAD = """
blinds:
  - entity: cover.a
zones:
  z:
    members: [cover.a]
modes:
  - {id: den}
rules:
  den.z:
"""


def _needs(rule_lines: str) -> int:
    return required_features(load_config(_HEAD + rule_lines))["cover.a"]


def test_the_ends_of_the_axis_need_only_open_and_close():
    """The point of deriving this per value: a shutter with no position axis is
    perfectly usable by rules that only ever say 0 or 100, and flagging it
    would be a false alarm on exactly the covers that cannot do more.
    """
    assert _needs("    - {then: {position: 0, tilt: 0}}\n") == CLOSE | CLOSE_TILT
    assert _needs("    - {then: {position: 100, tilt: 100}}\n") == OPEN | OPEN_TILT


def test_a_value_between_needs_the_setter():
    assert _needs("    - {then: {position: 34, tilt: 50}}\n") == SET_POSITION | SET_TILT_POSITION


def test_keep_asks_for_nothing():
    """`KEEP` is not a command, so it cannot make a blind unsuitable."""
    assert _needs("    - {then: {position: keep, tilt: keep}}\n") == 0


def test_every_row_counts_not_only_the_one_that_fires():
    """A rule that fires once a year still needs the blind to be able to do it."""
    needed = _needs(
        "    - {if: {condition: state, entity_id: input_boolean.x, state: 'on'},\n"
        "       then: {position: 50, tilt: keep}}\n"
        "    - {then: {position: 0, tilt: 100}}\n"
    )

    assert needed == SET_POSITION | CLOSE | OPEN_TILT


def test_a_tiltless_blind_is_never_charged_for_tilt():
    """`has_tilt: false` means the runner suppresses the axis, so asking for a
    tilt feature would be a false alarm -- `runner._suppressions`, `no_tilt`.
    """
    config = load_config("""
blinds:
  - entity: cover.a
    has_tilt: false
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
rules:
  den.z:
    - {then: {position: 0, tilt: 50}}
""")

    assert required_features(config)["cover.a"] == CLOSE


def test_a_value_reference_needs_the_setter_because_the_number_is_unknown():
    config = load_config("""
blinds:
  - entity: cover.a
zones:
  z: {members: [cover.a]}
values:
  poz: {entity: input_number.p, default: 34}
modes:
  - {id: den}
rules:
  den.z:
    - {then: {position: !ref poz, tilt: keep}}
""")

    assert required_features(config)["cover.a"] == SET_POSITION


def test_a_force_guard_counts_too():
    """The guard that fires when something has already gone wrong is the worst
    moment to discover the blind cannot carry its command out.
    """
    config = load_config("""
blinds:
  - entity: cover.a
zones:
  z: {members: [cover.a]}
modes:
  - {id: den}
rules:
  den.z:
    - {then: {position: keep, tilt: keep}}
guards:
  - name: wind
    policy: force
    applies_to: any
    when: {condition: state, entity_id: input_boolean.w, state: "on"}
    then: {position: 100, tilt: keep}
""")

    assert required_features(config)["cover.a"] == OPEN


def test_a_default_zone_row_charges_every_blind():
    """`*` rows apply to every zone, so they apply to every blind in them."""
    config = load_config("""
blinds:
  - entity: cover.a
  - entity: cover.b
zones:
  z1: {members: [cover.a]}
  z2: {members: [cover.b]}
modes:
  - {id: den}
rules:
  den.*:
    - {then: {position: 42, tilt: keep}}
""")
    needed = required_features(config)

    assert needed == {"cover.a": SET_POSITION, "cover.b": SET_POSITION}


@pytest.mark.parametrize(
    ("supported", "expected"),
    [
        (191, []),  # the owner's own blinds: everything but STOP_TILT
        (511, []),  # the demo instance's Living Room Window
        (3, ["SET_POSITION", "OPEN_TILT", "SET_TILT_POSITION"]),  # a garage door
        (15, ["OPEN_TILT", "SET_TILT_POSITION"]),  # position, no tilt
        (240, ["OPEN", "SET_POSITION"]),  # tilt only, no position
    ],
)
def test_missing_features_names_the_gaps(supported, expected):
    """Real `supported_features` values, read off live entities on 2026-09-03."""
    needed = OPEN | SET_POSITION | OPEN_TILT | SET_TILT_POSITION

    assert missing_features(needed, supported) == expected
