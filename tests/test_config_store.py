"""`config_store.config_from_subentries` must build the same `Config` that
`config_schema.load_config` builds -- just from Home Assistant config
subentries instead of a YAML file.

Kept pure, no `homeassistant` import: `config_from_subentries` (and the
duplicate-order check alongside it) are written to work against anything
that merely *looks* like a config entry -- `.subentries` (a mapping of id ->
object with `.subentry_type` and `.data`) and `.data` (a mapping) -- which is
exactly the shape of Home Assistant's own `ConfigEntry`/`ConfigSubentry`, but
this module never has to import those classes to say so. `FakeEntry`/
`FakeSubentry` below are that duck-typed stand-in, the same tradeoff
`tests/ha/conftest.py`'s fakes make for other HA types, except this one does
not even need `pytest.importorskip("homeassistant")` -- there is no HA
import anywhere on this path to skip.
"""

from dataclasses import dataclass, field
from typing import Any

from cover_logic.config_schema import load_config
from cover_logic.config_store import config_from_subentries, duplicate_rule_order_problems
from cover_logic.validation import ERROR, validate


@dataclass(frozen=True)
class FakeSubentry:
    """Duck-typed stand-in for `homeassistant.config_entries.ConfigSubentry`."""

    subentry_type: str
    data: dict[str, Any]


@dataclass
class FakeEntry:
    """Duck-typed stand-in for `homeassistant.config_entries.ConfigEntry`."""

    subentries: dict[str, FakeSubentry] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


def make_entry(items: list[tuple[str, dict]], data: dict | None = None) -> FakeEntry:
    """Build a `FakeEntry` from `(subentry_type, data)` pairs, in the given order.

    Dict insertion order is what `config_store` sees as "subentry order" --
    tests that want to prove sorting-by-`order` beats that order construct
    `items` out of logical sequence on purpose.
    """
    subentries = {f"s{i}": FakeSubentry(kind, body) for i, (kind, body) in enumerate(items)}
    return FakeEntry(subentries=subentries, data=data or {})


# The same configuration, expressed both ways. Two blinds, two zones, one
# named condition, one value ref, two modes (one conditional, one fallback),
# and rules exercising a condition ref, a value ref, a bare "keep", plain
# integers, an `events` scope and a rule `name` -- the full vocabulary
# `config_schema._parse_rule`/`_parse_action`/`_parse_condition` support.
YAML_TEXT = """
blinds:
  - entity: cover.a
    facade_azimuth: 270
    tolerance: 30
    travel_time: 45
    tilt_after_arrival: false
  - entity: cover.b
zones:
  terasa:
    members: [cover.a]
    occupants: [peter]
  izba:
    members: [cover.b]
conditions:
  slnko:
    condition: state
    entity_id: sensor.x
    state: "on"
values:
  poz:
    entity: input_number.poz
    default: 40
modes:
  - {id: slnecno, when: !ref slnko}
  - {id: fallback}
rules:
  slnecno.terasa:
    - if: !ref slnko
      then: {position: !ref poz, tilt: keep}
      name: shade
      events: [arrival]
    - then: {position: 0, tilt: 100}
  slnecno.izba:
    - then: {position: keep, tilt: keep}
  fallback.terasa:
    - then: {position: 100, tilt: 100}
  fallback.izba:
    - then: {position: 100, tilt: 100}
"""

# Subentries in a deliberately scrambled order: the second (catch-all) rule
# of slnecno.terasa is listed before the first, and the fallback mode is
# listed before the conditional one. If `config_from_subentries` sorted by
# insertion order instead of `order`, this would build a *different* `Config`
# than `YAML_TEXT` above -- so this is also the "sorted by order, not
# insertion" test.
SUBENTRY_ITEMS: list[tuple[str, dict]] = [
    ("blind", {"entity": "cover.b"}),
    (
        "blind",
        {
            "entity": "cover.a",
            "facade_azimuth": 270,
            "tolerance": 30,
            "travel_time": 45,
            "tilt_after_arrival": False,
        },
    ),
    ("zone", {"id": "izba", "members": ["cover.b"]}),
    ("zone", {"id": "terasa", "members": ["cover.a"], "occupants": ["peter"]}),
    (
        "condition",
        {"id": "slnko", "condition": "state", "entity_id": "sensor.x", "state": "on"},
    ),
    ("value", {"id": "poz", "entity": "input_number.poz", "default": 40}),
    ("mode", {"id": "fallback", "order": 1}),
    ("mode", {"id": "slnecno", "order": 0, "when": {"ref": "slnko"}}),
    (
        "rule",
        {"mode": "slnecno", "zone": "terasa", "order": 1, "then": {"position": 0, "tilt": 100}},
    ),
    (
        "rule",
        {
            "mode": "slnecno",
            "zone": "terasa",
            "order": 0,
            "if": {"ref": "slnko"},
            "then": {"position": {"ref": "poz"}, "tilt": "keep"},
            "name": "shade",
            "events": ["arrival"],
        },
    ),
    (
        "rule",
        {
            "mode": "slnecno",
            "zone": "izba",
            "order": 0,
            "then": {"position": "keep", "tilt": "keep"},
        },
    ),
    (
        "rule",
        {"mode": "fallback", "zone": "terasa", "order": 0, "then": {"position": 100, "tilt": 100}},
    ),
    (
        "rule",
        {"mode": "fallback", "zone": "izba", "order": 0, "then": {"position": 100, "tilt": 100}},
    ),
]


def test_subentries_and_yaml_produce_an_equal_config():
    from_yaml = load_config(YAML_TEXT)
    from_subentries = config_from_subentries(make_entry(SUBENTRY_ITEMS))

    assert from_subentries == from_yaml


def test_subentries_and_yaml_config_has_no_validation_errors():
    # A sanity check on the fixture itself: this proves the equality above
    # is not two configs that happen to agree while both being broken.
    problems = validate(config_from_subentries(make_entry(SUBENTRY_ITEMS)))
    assert [p for p in problems if p.severity == ERROR] == []


def test_rules_are_sorted_by_order_not_by_subentry_insertion_order():
    scrambled = [
        ("blind", {"entity": "cover.a"}),
        ("zone", {"id": "z", "members": ["cover.a"]}),
        ("mode", {"id": "m", "order": 0}),
        # Listed second, but its `order` says it should run first.
        ("rule", {"mode": "m", "zone": "z", "order": 1, "then": {"position": 0}}),
        ("rule", {"mode": "m", "zone": "z", "order": 0, "then": {"position": 100}}),
    ]
    config = config_from_subentries(make_entry(scrambled))

    positions = [rule.then.position for rule in config.rules["m.z"]]
    assert positions == [100, 0]


def test_empty_entry_produces_an_empty_config():
    assert config_from_subentries(make_entry([])) == load_config("")


def test_guards_are_carried_through_unchanged():
    entry = make_entry([], data={"guards": [{"anything": "goes"}]})

    config = config_from_subentries(entry)

    assert config.guards == ({"anything": "goes"},)
    assert config.guards == load_config("guards:\n  - {anything: goes}\n").guards


def test_action_axis_can_be_keep_or_a_value_ref_not_just_an_integer():
    items = [
        ("blind", {"entity": "cover.a"}),
        ("zone", {"id": "z", "members": ["cover.a"]}),
        ("value", {"id": "v", "entity": "input_number.v", "default": 12}),
        ("mode", {"id": "m", "order": 0}),
        (
            "rule",
            {
                "mode": "m",
                "zone": "z",
                "order": 0,
                "then": {"position": {"ref": "v"}, "tilt": "keep"},
            },
        ),
    ]
    config = config_from_subentries(make_entry(items))

    rule = config.rules["m.z"][0]
    assert rule.then.tilt is config.rules["m.z"][0].then.tilt  # KEEP is a singleton
    assert rule.then.position == config.values["v"]


def test_duplicate_order_in_one_mode_zone_is_reported() -> None:
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("zone", {"id": "z", "members": ["cover.a"]}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "z", "order": 0, "then": {"position": 0}}),
            ("rule", {"mode": "m", "zone": "z", "order": 0, "then": {"position": 100}}),
        ]
    )

    problems = duplicate_rule_order_problems(entry)

    assert len(problems) == 1
    assert problems[0].severity == ERROR
    assert problems[0].code == "duplicate_rule_order"
    assert "m.z" in problems[0].message


def test_no_duplicate_order_across_different_mode_zone_keys() -> None:
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("blind", {"entity": "cover.b"}),
            ("zone", {"id": "z1", "members": ["cover.a"]}),
            ("zone", {"id": "z2", "members": ["cover.b"]}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "z1", "order": 0, "then": {"position": 0}}),
            ("rule", {"mode": "m", "zone": "z2", "order": 0, "then": {"position": 0}}),
        ]
    )

    assert duplicate_rule_order_problems(entry) == []
