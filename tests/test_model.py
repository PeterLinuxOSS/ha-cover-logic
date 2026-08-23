from __future__ import annotations

import pytest

from cover_logic.model import KEEP, Action, Blind, Keep, Ref, Zone


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
