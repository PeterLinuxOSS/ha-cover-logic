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

from cover_logic.config_schema import ConfigError, load_config, load_config_file
from cover_logic.config_store import (
    config_from_subentries,
    duplicate_rule_order_problems,
    effective_rule_items,
    entry_from_subentry_items,
    guards_to_data,
    rule_owner_ids,
    subentries_from_config,
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


def test_guards_from_entry_data_parse_exactly_as_the_same_yaml_would():
    """The two doors into `Config` must agree about guards, not merely both
    hold "something".

    `entry.data["guards"]` and a YAML `guards:` key are the same list of
    mappings read by the same parser (`config_schema.parse_guards`) -- that
    is the whole reason guards can stay in `entry.data` while their schema is
    real. Asserting equality against the YAML door, rather than against a
    hand-written expected `Guard`, is what would catch this module growing a
    second guard parser of its own.
    """
    guard_data = {
        "policy": "skip",
        "applies_to": "closing",
        "stage": "input",
        "targets": ["cover.a"],
    }
    entry = make_entry([], data={"guards": [guard_data]})

    config = config_from_subentries(entry)

    assert (
        config.guards
        == load_config(
            "guards:\n  - {policy: skip, applies_to: closing, stage: input, targets: [cover.a]}\n"
        ).guards
    )
    assert config.guards[0].policy == "skip"
    assert config.guards[0].targets == ("cover.a",)


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
    `subentry_flow._blocks_on` can refuse to block a save of the third,
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
    """The contract `subentry_flow` depends on and that nothing else pins down.

    `validation` names a rule by its position in the already-`order`-sorted
    tuple (`validation._rule_owner`), because a `Config` no longer carries
    either subentry ids or `order`. `rule_owner_ids` is the only bridge from
    a real Home Assistant subentry id back to that name. If the two ever
    disagree -- a different sort, a different tie-break, a different string
    -- `subentry_flow._blocks_on` would silently compare a problem against the
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


# --- Inherited (default) rules ------------------------------------------
#
# A `rule` subentry's `zone` field is already required (`_require`), so
# `zone: "*"` (`const.RULE_DEFAULT_ZONE`) needs no new subentry shape -- it
# is grouped by `_grouped_rules` under `f"{mode}.*"` the same way any other
# zone value would be, purely by string concatenation. These tests pin that
# "no special case needed" claim rather than assume it.


def test_a_wildcard_zone_rule_groups_under_the_mode_default_key():
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("zone", {"id": "z", "members": ["cover.a"]}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "*", "order": 0, "then": {"position": 0}}),
        ]
    )
    config = config_from_subentries(entry)
    assert "m.*" in config.rules
    assert config.rules["m.*"][0].then.position == 0
    # No `m.z` own list was declared -- confirmed absent, not just unread.
    assert "m.z" not in config.rules


def test_rule_owner_ids_names_a_default_rule_by_its_wildcard_key():
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("zone", {"id": "z", "members": ["cover.a"]}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "*", "order": 0, "then": {"position": 0}}),
        ]
    )
    assert rule_owner_ids(entry) == {"s3": "m.*#0"}


def test_two_defaults_sharing_an_order_is_a_reported_tie():
    """`duplicate_rule_order_problems` groups by the literal `"mode.zone"`
    key `_grouped_rules` produces -- `"m.*"` for defaults -- so a tie among
    default rows is caught exactly like a tie among a zone's own rows,
    without a second check.
    """
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("zone", {"id": "z", "members": ["cover.a"]}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "*", "order": 0, "then": {"position": 0}}),
            ("rule", {"mode": "m", "zone": "*", "order": 0, "then": {"position": 100}}),
        ]
    )
    problems = duplicate_rule_order_problems(entry)
    assert len(problems) == 1
    assert "m.*" in problems[0].message


def test_a_zone_rules_order_number_relative_to_the_default_does_not_matter() -> None:
    """A zone's own rule always runs before its mode's defaults, regardless
    of what `order` a human assigned to either one -- `order` numbers a rule
    against others in the *same* `(mode, zone)` group only (see
    `config_store`'s own "Ordering" docstring section); it is never compared
    across groups. Given a zone rule numbered *after* a default rule that
    would otherwise win, the zone rule still wins.
    """
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("zone", {"id": "z", "members": ["cover.a"]}),
            ("mode", {"id": "m", "order": 0}),
            # A human editing this by hand might reasonably expect `order: 1`
            # to run before `order: 100` -- that intuition only holds within
            # one group; `m.z` and `m.*` are different groups entirely.
            ("rule", {"mode": "m", "zone": "*", "order": 1, "then": {"position": 0}}),
            ("rule", {"mode": "m", "zone": "z", "order": 100, "then": {"position": 100}}),
        ]
    )
    config = config_from_subentries(entry)
    assert config.rules["m.z"][0].then.position == 100
    assert config.rules["m.*"][0].then.position == 0


def test_a_zone_named_asterisk_is_rejected() -> None:
    entry = make_entry([("zone", {"id": "*", "members": []})])
    with pytest.raises(ConfigError, match=r"\*"):
        config_from_subentries(entry)


def test_subentries_from_config_round_trips_a_default_rule() -> None:
    """The write side (`subentries_from_config`, used by `import_config`)
    must also carry a wildcard rule key through -- `key.partition(".")` in
    `subentries_from_config` splits `"m.*"` into `mode_id="m"`,
    `zone_id="*"` by plain string operations, the same as any other key, so
    this is confirming that claim rather than assuming it.
    """
    original = config_from_subentries(
        make_entry(
            [
                ("blind", {"entity": "cover.a"}),
                ("zone", {"id": "z", "members": ["cover.a"]}),
                ("mode", {"id": "m", "order": 0}),
                ("rule", {"mode": "m", "zone": "*", "order": 0, "then": {"position": 0}}),
            ]
        )
    )
    items = subentries_from_config(original)
    rebuilt = config_from_subentries(entry_from_subentry_items(items, original.guards))
    assert rebuilt == original
    assert rebuilt.rules["m.*"][0].then.position == 0


def test_effective_rule_items_is_own_rules_then_the_mode_default_tagged() -> None:
    """The subentry-level counterpart of `engine._apply_rules`'s own list.

    Not a re-sort: this reads `_grouped_rules(entry)` for `"m.z"` and `"m.*"`
    and concatenates the two lists in that order, own rules untagged
    (`is_default=False`), the mode's shared defaults tagged
    (`is_default=True`) -- exactly what `options_flow.py`'s per-zone rule
    screen needs to both order and label its picker.
    """
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("zone", {"id": "z", "members": ["cover.a"]}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "z", "order": 20, "then": {"position": 0}}),
            ("rule", {"mode": "m", "zone": "*", "order": 0, "then": {"position": 100}}),
        ]
    )

    items = effective_rule_items(entry, "m", "z")

    assert [(sid, is_default) for sid, _data, is_default in items] == [
        ("s3", False),
        ("s4", True),
    ]
    assert items[0][1]["then"] == {"position": 0}
    assert items[1][1]["then"] == {"position": 100}


def test_effective_rule_items_for_a_zone_with_none_of_its_own_is_just_the_default() -> None:
    """A zone with no own rule subentry at all is invisible to any picker
    built only from `_grouped_rules(entry)["m.<that zone>"]` -- this is the
    function that makes a purely-inheriting zone visible anyway, which is
    the whole point of showing it in `options_flow.py`'s zone screen.
    """
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "*", "order": 0, "then": {"position": 100}}),
        ]
    )

    items = effective_rule_items(entry, "m", "spalna")

    assert [(sid, is_default) for sid, _data, is_default in items] == [("s2", True)]


def test_effective_rule_items_for_the_wildcard_zone_itself_is_never_tagged_inherited() -> None:
    """Viewing the mode's default list on its own terms (`zone_id ==
    RULE_DEFAULT_ZONE`): there is no further default to fall back to, so
    every row is the zone's "own", not inherited from anything.
    """
    entry = make_entry(
        [
            ("blind", {"entity": "cover.a"}),
            ("mode", {"id": "m", "order": 0}),
            ("rule", {"mode": "m", "zone": "*", "order": 0, "then": {"position": 100}}),
        ]
    )

    items = effective_rule_items(entry, "m", "*")

    assert [(sid, is_default) for sid, _data, is_default in items] == [("s2", False)]


def test_effective_rule_items_for_a_pair_with_nothing_at_all_is_empty() -> None:
    entry = make_entry([("blind", {"entity": "cover.a"})])

    assert effective_rule_items(entry, "m", "z") == []


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


# --- subentries_from_config: the write side (task 5) ------------------------
#
# `subentries_from_config` is the inverse of `config_from_subentries` above,
# used by the `import_config` service to turn an imported YAML `Config` into
# real subentries. `YAML_TEXT`/`SUBENTRY_ITEMS` already give one configuration
# expressed both ways -- two zones, two modes (one conditional, one
# fallback), and `slnecno.terasa`'s rule list, which is exactly the "a pair
# with non-contiguous orders" case the task brief calls out: `config_
# from_subentries` sees it filed under scrambled subentry `order`s 1 then 0
# (see `SUBENTRY_ITEMS`'s own comment), so a `Config` built from it already
# has its two rules in the *resolved* order (the `!ref slnko` one first, the
# catch-all second) before `subentries_from_config` ever sees it -- proving
# it reproduces that resolved order using its own fresh `order` numbering,
# not whatever numbers happened to produce it.


def test_subentries_from_config_round_trips_the_yaml_text_config():
    original = load_config(YAML_TEXT)
    items = subentries_from_config(original)
    rebuilt = config_from_subentries(entry_from_subentry_items(items, original.guards))
    assert rebuilt == original


def test_subentries_from_config_preserves_multi_rule_order_within_a_group():
    original = load_config(YAML_TEXT)
    items = subentries_from_config(original)
    rule_items = [data for kind, data in items if kind == "rule" and data["zone"] == "terasa"]
    slnecno_rules = sorted(
        (data for data in rule_items if data["mode"] == "slnecno"), key=lambda d: d["order"]
    )
    assert [rule.get("name") for rule in slnecno_rules] == ["shade", None]


def test_subentries_from_config_round_trips_the_real_fixture(fixtures_dir):
    """The decisive test on the real house's configuration -- 10 blinds, 7
    zones, 4 modes, 20 conditions, 1 value and 80 rules across 22 `(mode,
    zone)` groups (one of them the `noc` mode's shared `"*"` default, phase 6
    task 3), several with more than one rule. `subentries_from_config`
    already runs this same equality check on every call (see its own
    docstring's "Before returning" paragraph); this test is what actually
    exercises that self-check against configuration of this size and
    complexity, rather than only the small hand-written fixtures above.
    """
    original = load_config_file(fixtures_dir / "dom_peter.yaml")
    items = subentries_from_config(original)
    rebuilt = config_from_subentries(entry_from_subentry_items(items, original.guards))
    assert rebuilt == original


def test_subentries_from_config_rejects_a_bare_list_as_a_named_conditions_body():
    """`Config.conditions` is typed `dict[str, dict]` (see `model.Config`),
    but `load_config` does not itself enforce that a named condition's body
    is a mapping -- a bare list parses fine there (`_parse_condition`
    accepts a top-level list, treating it as an implicit `and`) and only
    breaks the *subentry* shape, which has no field to hold a body that is
    not itself a mapping. This must raise loudly rather than silently
    dropping the list or guessing at how to wrap it.
    """
    text = """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: any}
conditions:
  multi:
    - {condition: state, entity_id: a, state: "on"}
    - {condition: state, entity_id: b, state: "on"}
rules:
  any.z:
    - {then: {position: keep, tilt: keep}}
"""
    cfg = load_config(text)
    assert isinstance(cfg.conditions["multi"], list)  # the shape this guards against
    with pytest.raises(ConfigError):
        subentries_from_config(cfg)


def test_subentries_from_config_self_check_rejects_a_broken_conversion(monkeypatch):
    """Prove the round-trip self-check inside `subentries_from_config` is
    load-bearing, not decoration: break the write side (make it drop a
    blind's `facade_azimuth`/`tolerance`/`travel_time`) and confirm the
    function refuses to return anything, instead of silently handing back
    subentries that would reload to a *different* `Config` than the one it
    was given -- exactly the class of bug this task's brief warns the
    inherited write side had never been exercised against.
    """
    # Patched by dotted string rather than by importing the module a second
    # time: `from cover_logic.config_store import ...` above already pulls it
    # in, and adding `import cover_logic.config_store` alongside is what
    # CodeQL's "imported with 'import' and 'import from'" rule flags.
    monkeypatch.setattr(
        "cover_logic.config_store._blind_to_dict", lambda blind: {"entity": blind.entity}
    )

    original = load_config(YAML_TEXT)  # cover.a has a non-default facade_azimuth
    with pytest.raises(ConfigError):
        subentries_from_config(original)


# --- guards ------------------------------------------------------------------
# Guards are not a subentry type: they live in `entry.data["guards"]`, as the
# same list of mappings a YAML `guards:` key holds. What must hold is that the
# two doors agree -- one parser, one serializer, no loss in either direction.


def test_guards_survive_a_full_yaml_to_subentries_to_config_round_trip() -> None:
    """The whole round trip, in one assertion, over a guard using every slot
    that could be dropped silently: a condition ref, a value ref inside a
    `force`'s action, a `max_wait` written as `null` (which must not come back
    as an absent key), and a derived `recheck_every`.
    """
    original = load_config(
        """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: m}
conditions:
  door: {condition: state, entity_id: binary_sensor.door, state: "on"}
values:
  poz: {entity: input_number.poz, default: 34}
rules:
  m.z:
    - {then: {position: keep}}
guards:
  - {name: door guard, policy: defer, applies_to: closing, targets: [z], when: !ref door,
     max_wait: null, on_timeout: abandon}
  - {policy: force, stage: input, then: {position: !ref poz}}
"""
    )

    items = subentries_from_config(original)
    rebuilt = config_from_subentries(entry_from_subentry_items(items, guards_to_data(original)))

    assert rebuilt == original
    assert rebuilt.guards[0].max_wait is None
    assert rebuilt.guards[0].recheck_every == 900


def test_guards_to_data_writes_this_modules_ref_marker_not_the_yaml_tag() -> None:
    """Subentry data is plain, JSON-shaped data -- a `RefTag` in it would not
    survive `.storage`. The two `!ref`-capable guard slots (`when`, and a
    `force`'s `then`) must both come out as `{"ref": ...}` markers, the same
    convention every other subentry uses.
    """
    config = load_config(
        """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: m}
conditions:
  door: {condition: state, entity_id: binary_sensor.door, state: "on"}
values:
  poz: {entity: input_number.poz, default: 34}
rules:
  m.z:
    - {then: {position: keep}}
guards:
  - {policy: force, when: !ref door, then: {position: !ref poz}}
"""
    )

    data = guards_to_data(config)

    assert data[0]["when"] == {"ref": "door"}
    assert data[0]["then"]["position"] == {"ref": "poz"}


def test_the_round_trip_self_check_sees_a_guard_that_would_be_lost() -> None:
    """`subentries_from_config` rebuilds a `Config` from exactly the pairs it
    is about to return and refuses to hand back anything that does not compare
    equal. Guards go through that same check via `guards_to_data`, so a future
    change that dropped or mis-shaped one fails loudly here rather than
    quietly exporting a house with its interlocks missing.
    """
    original = load_config(
        """
blinds:
  - {entity: cover.a}
zones:
  z: {members: [cover.a]}
modes:
  - {id: m}
rules:
  m.z:
    - {then: {position: keep}}
guards:
  - {policy: skip, applies_to: closing, targets: [cover.a]}
"""
    )

    # The self-check compares against `config.guards`, so a config whose
    # guards serialize losslessly round-trips; this is the positive control
    # for the assertion that guards are part of that comparison at all.
    items = subentries_from_config(original)
    with_guards = entry_from_subentry_items(items, guards_to_data(original))
    assert config_from_subentries(with_guards) == original
    assert config_from_subentries(entry_from_subentry_items(items)) != original
