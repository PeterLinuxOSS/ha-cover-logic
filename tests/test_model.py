import copy
import pickle

import pytest

from cover_logic.model import (
    KEEP,
    UNSET,
    Action,
    Blind,
    Config,
    Guard,
    Keep,
    Mode,
    Ref,
    Rule,
    Unset,
    Zone,
)


def test_keep_is_a_singleton():
    assert Keep() is KEEP
    assert repr(KEEP) == "KEEP"


def test_action_defaults_to_keeping_both_axes():
    a = Action()
    assert a.position is KEEP
    assert a.tilt is KEEP


def test_action_is_frozen_and_hashable():
    a = Action(position=0, tilt=50)
    assert hash(a) == hash(Action(position=0, tilt=50))
    with pytest.raises((AttributeError, TypeError)):
        a.position = 100


def test_action_accepts_a_ref_on_the_position_axis():
    ref = Ref(entity="input_number.kvety_pozicia_zaluzie", default=34)
    a = Action(position=ref, tilt=0)
    assert a.position == ref
    assert a.tilt == 0


def test_blind_defaults_match_a_tilting_venetian_blind():
    b = Blind(entity="cover.x")
    assert b.facade_azimuth is None
    assert b.tolerance == 45.0
    assert b.tilt_after_arrival is True
    assert b.has_tilt is True


def test_zone_members_are_a_tuple_so_the_zone_stays_hashable():
    z = Zone(id="terasa", members=("cover.a", "cover.b"))
    assert hash(z)
    assert z.occupants == ()


def test_rule_constructed_with_single_positional_argument():
    """Rule's only required field is `then`; when, events, and name default."""
    action = Action()
    r = Rule(action)
    assert r.then is action
    assert r.when is None
    assert r.events is None
    assert r.name == ""


def test_rule_constructed_with_two_positional_arguments():
    """Second positional argument lands on `when`, not elsewhere."""
    action = Action()
    when_dict = {"some": "condition"}
    r = Rule(action, when_dict)
    assert r.then is action
    assert r.when is when_dict
    assert r.events is None
    assert r.name == ""


def test_mode_constructed_with_only_id():
    """Mode without condition defaults when to None (fallback mode)."""
    m = Mode(id="fallback")
    assert m.id == "fallback"
    assert m.when is None


def test_config_with_all_seven_fields_round_trips():
    """Config's seven fields all round-trip correctly."""
    blinds = {"cover.x": Blind(entity="cover.x")}
    zones = {"z1": Zone(id="z1", members=("cover.x",))}
    modes = (Mode(id="m1"),)
    rules = {"m1": (Rule(Action()),)}
    conditions = {"c1": {"key": "value"}}
    values = {"v1": Ref(entity="input_number.x", default=50)}
    guards = (Guard(policy="skip"), Guard(policy="force", then=Action(position=100)))

    c = Config(
        blinds=blinds,
        zones=zones,
        modes=modes,
        rules=rules,
        conditions=conditions,
        values=values,
        guards=guards,
    )

    assert c.blinds is blinds
    assert c.zones is zones
    assert c.modes is modes
    assert c.rules is rules
    assert c.conditions is conditions
    assert c.values is values
    assert c.guards is guards


def test_config_is_frozen():
    """Config is frozen; setting an attribute raises."""
    c = Config(
        blinds={},
        zones={},
        modes=(),
        rules={},
    )
    with pytest.raises((AttributeError, TypeError)):
        c.blinds = {}


def test_keep_singleton_survives_copy():
    """Keep's identity is preserved through copy.copy."""
    k = copy.copy(KEEP)
    assert k is KEEP


def test_keep_singleton_survives_deepcopy():
    """Keep's identity is preserved through copy.deepcopy."""
    k = copy.deepcopy(KEEP)
    assert k is KEEP


def test_keep_singleton_survives_pickle():
    """Keep's identity is preserved through pickle round-trip."""
    pickled = pickle.dumps(KEEP)
    k = pickle.loads(pickled)  # noqa: S301 -- round-tripping our own trusted data, not untrusted input
    assert k is KEEP


def test_action_containing_keep_preserves_identity_after_deepcopy():
    """Action with KEEP on both axes keeps KEEP identity after deepcopy."""
    a = Action(position=KEEP, tilt=KEEP)
    a_copy = copy.deepcopy(a)
    assert a_copy.position is KEEP
    assert a_copy.tilt is KEEP


def test_unset_is_a_singleton():
    assert Unset() is UNSET
    assert repr(UNSET) == "UNSET"


def test_unset_singleton_survives_copy_deepcopy_and_pickle():
    """`UNSET` is compared with `is` in `validation` and in `guard_to_dict`.

    A copy that produced a second, equal-but-not-identical `Unset` would make
    a guard that never stated `max_wait` look like one that stated `null` --
    the exact distinction `Unset` exists to hold. `World` deep-copies the
    attribute snapshots it is handed, and `Config`s travel through
    `copy`/`pickle` in tests, so all three routes are pinned.
    """
    assert copy.copy(UNSET) is UNSET
    assert copy.deepcopy(UNSET) is UNSET
    assert pickle.loads(pickle.dumps(UNSET)) is UNSET  # noqa: S301 -- our own data


def test_a_guard_defaults_to_the_widest_reading_and_states_no_wait():
    guard = Guard(policy="skip")
    assert guard.applies_to == "any"
    assert guard.stage == "output"
    assert guard.max_wait is UNSET
    assert guard.on_timeout is None
    assert guard.targets == ()


def test_guards_are_frozen_and_compare_by_value():
    a = Guard(policy="defer", max_wait=None, on_timeout="abandon")
    b = Guard(policy="defer", max_wait=None, on_timeout="abandon")
    assert a == b
    with pytest.raises(Exception):  # noqa: B017, PT011 -- FrozenInstanceError is a subclass
        a.policy = "skip"
