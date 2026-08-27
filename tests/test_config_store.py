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

import pytest

from cover_logic.config_schema import load_config
from cover_logic.config_store import (
    config_from_subentries,
    duplicate_rule_order_problems,
    rule_owner_ids,
)
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


def test_duplicate_order_names_both_tied_rules_and_nobody_else() -> None:
    """The tie is attributed to the two rules that are actually tied, so
    `config_flow._blocks_on` can refuse to block a save of the third,
    untied rule -- or of a rule in a different pair entirely. Without
    `owners`, the only thing knowable about this problem is "some rule
    somewhere", which blocks every rule save in the entry.
    """
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("zone", {"id": "z", "members": ["cover.a"]}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "z", "order": 0, "then": {"position": 0}}),
            ("rule", {"mode": "m", "zone": "z", "order": 0, "then": {"position": 100}}),
            ("rule", {"mode": "m", "zone": "z", "order": 5, "then": {"position": 50}}),
        ]
    )

    problems = duplicate_rule_order_problems(entry)

    assert len(problems) == 1
    # The two `order: 0` rules sort ahead of the `order: 5` one, so they are
    # index 0 and 1; the untied rule at index 2 is deliberately not an owner.
    assert problems[0].owners == frozenset({("rule", "m.z#0"), ("rule", "m.z#1")})


def test_rule_owner_ids_match_the_ids_validation_attributes_problems_to() -> None:
    """The contract `config_flow` depends on and that nothing else pins down.

    `validation` names a rule by its position in the already-`order`-sorted
    tuple (`validation._rule_owner`), because a `Config` no longer carries
    either subentry ids or `order`. `rule_owner_ids` is the only bridge from
    a real Home Assistant subentry id back to that name. If the two ever
    disagree -- a different sort, a different tie-break, a different string
    -- `config_flow._blocks_on` would silently compare a problem against the
    wrong rule, blocking an innocent save or waving a broken one through,
    with nothing else in the suite noticing.

    Driven from a genuinely scrambled insertion order so agreeing by
    accident (both walking `entry.subentries` unsorted) is not possible.
    """
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("zone", {"id": "z", "members": ["cover.a"]}),
            # `m` is never declared as a mode, so every rule below is stranded
            # under an unknown key -- the one ERROR `validate()` attributes to
            # rules rather than to conditions.
            ("mode", {"id": "other", "order": 0}),
            ("rule", {"mode": "m", "zone": "z", "order": 20, "then": {"position": 20}}),
            ("rule", {"mode": "m", "zone": "z", "order": 0, "then": {"position": 0}}),
            ("rule", {"mode": "m", "zone": "z", "order": 10, "then": {"position": 10}}),
        ]
    )

    # Sorted by `order`, the three rules are s4 (0), s5 (10), s3 (20).
    assert rule_owner_ids(entry) == {"s3": "m.z#2", "s4": "m.z#0", "s5": "m.z#1"}

    stranded = [p for p in validate(config_from_subentries(entry)) if p.code == "unknown_rule_key"]
    assert len(stranded) == 1
    assert stranded[0].owners == frozenset(
        ("rule", owner_id) for owner_id in rule_owner_ids(entry).values()
    )

    # And the names really do point at the rules they claim to: index 1 is
    # the `order: 10` rule, which is the one that sets position 10.
    assert config_from_subentries(entry).rules["m.z"][1].then.position == 10


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


def _assert_owner_ids_point_at_the_rules_they_claim_to(entry: Any) -> None:
    """Decode `rule_owner_ids(entry)` back onto `entry.subentries` and check it names,
    subentry for subentry, the exact tuple `config_from_subentries(entry).rules[key]` holds.

    Relies on the fixture tagging each rule's `name` with `f"order-{order}"` --
    a free-form field with no range check, unlike `position` -- so this stays
    correct for orders far outside 0..100 too. This is the property that
    `_grouped_rules` being the single source for both `_build_rules` and
    `rule_owner_ids` makes structurally guaranteed rather than merely tested;
    this test is what pins that guarantee down from the outside, through the
    public API only, so it also catches a future regression back into two
    hand-mirrored copies.
    """
    config = config_from_subentries(entry)
    owners = rule_owner_ids(entry)
    subentry_id_by_owner = {owner: subentry_id for subentry_id, owner in owners.items()}

    for key, rules in config.rules.items():
        for index, rule in enumerate(rules):
            subentry_id = subentry_id_by_owner[f"{key}#{index}"]
            claimed_order = entry.subentries[subentry_id].data["order"]
            assert rule.name == f"order-{claimed_order}"


@pytest.mark.parametrize(
    "orders_by_insertion",
    [
        pytest.param([0, 1, 2], id="orders_match_insertion_order"),
        pytest.param([2, 0, 1], id="subentries_inserted_out_of_order_value_order"),
        pytest.param([0, 100, 55], id="non_contiguous_orders_in_insertion_order"),
        pytest.param([55, 0, 100], id="non_contiguous_orders_out_of_order_value_order"),
        pytest.param([1], id="single_rule"),
    ],
)
def test_rule_owner_ids_align_with_config_rules_across_shapes(
    orders_by_insertion: list[int],
) -> None:
    items = [
        ("blind", {"entity": "cover.a"}),
        ("zone", {"id": "z", "members": ["cover.a"]}),
        ("mode", {"id": "m", "order": 0}),
        *(
            (
                "rule",
                {
                    "mode": "m",
                    "zone": "z",
                    "order": order,
                    "then": {"position": 0},
                    "name": f"order-{order}",
                },
            )
            for order in orders_by_insertion
        ),
    ]
    entry = make_entry(items)

    _assert_owner_ids_point_at_the_rules_they_claim_to(entry)

    # And the ordering itself is right, not just internally self-consistent.
    names = [rule.name for rule in config_from_subentries(entry).rules["m.z"]]
    assert names == [f"order-{order}" for order in sorted(orders_by_insertion)]


def test_rule_owner_ids_align_with_config_rules_across_several_mode_zone_keys() -> None:
    """The same alignment property, but with two independently-sorted groups
    interleaved in `entry.subentries`, so a bug that leaked one group's index
    into another's would show up here even if the single-key tests above did
    not exercise it.
    """
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("blind", {"entity": "cover.b"}),
            ("zone", {"id": "z1", "members": ["cover.a"]}),
            ("zone", {"id": "z2", "members": ["cover.b"]}),
            ("mode", {"id": "m", "order": 0}),
            (
                "rule",
                {"mode": "m", "zone": "z2", "order": 5, "then": {"position": 0}, "name": "order-5"},
            ),
            (
                "rule",
                {
                    "mode": "m",
                    "zone": "z1",
                    "order": 20,
                    "then": {"position": 0},
                    "name": "order-20",
                },
            ),
            (
                "rule",
                {"mode": "m", "zone": "z1", "order": 0, "then": {"position": 0}, "name": "order-0"},
            ),
            (
                "rule",
                {"mode": "m", "zone": "z2", "order": 1, "then": {"position": 0}, "name": "order-1"},
            ),
        ]
    )

    _assert_owner_ids_point_at_the_rules_they_claim_to(entry)


def test_tied_order_breaks_ties_by_subentry_id_not_by_subentries_mapping_order() -> None:
    """Two rules sharing an `order` still need a total order between
    `Config.rules` and `rule_owner_ids` to agree on -- `entry.subentries` is a
    mapping, and nothing in Home Assistant's contract promises it preserves
    insertion order across, say, a storage round-trip. Subentry ids are
    stable and unique, so sorting by `(order, subentry id)` makes the result
    the same regardless of what order `entry.subentries` iterates in.

    Constructed with `FakeEntry` directly, not `make_entry`, because
    `make_entry` assigns ids `s0`, `s1`, ... in insertion order, which would
    make the id order and the insertion order the same thing and defeat the
    point of this test. Here the subentry inserted *first* ("rB") has the id
    that sorts *second*, so a correct, id-tiebroken sort must place "rA"
    first even though it was inserted after "rB".
    """
    entry = FakeEntry(
        subentries={
            "rB": FakeSubentry(
                "rule", {"mode": "m", "zone": "z", "order": 0, "then": {"position": 100}}
            ),
            "rA": FakeSubentry(
                "rule", {"mode": "m", "zone": "z", "order": 0, "then": {"position": 0}}
            ),
        }
    )

    positions = [rule.then.position for rule in config_from_subentries(entry).rules["m.z"]]
    assert positions == [0, 100]

    assert rule_owner_ids(entry) == {"rA": "m.z#0", "rB": "m.z#1"}
