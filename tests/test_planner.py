"""Invariants of `plan()`, stated over the whole (capability x state x target) grid.

The migration gate proves what the engine *decides*. Nothing proves what a
motor *does* -- a planner tested against a model of a blind is not tested
against a blind. So these tests state invariants over every combination
rather than enumerating hand-picked cases, and every invariant that could
pass vacuously carries a counter asserting it was actually exercised: an
implementation that returns no commands at all must fail here, not sail
through a suite of "if a command is emitted, then ..." implications.
"""

import itertools

from hypothesis import given, settings, strategies as st
import pytest

from cover_logic.model import KEEP, Action, Blind, Ref
from cover_logic.planner import (
    ARRIVAL_TIMEOUT_FACTOR,
    AXIS_POSITION,
    AXIS_TILT,
    DEAD_BAND,
    SETTLE_SECONDS,
    TOP_THRESHOLD,
    Clamp,
    Plan,
    PlannerError,
    SetPosition,
    SetTilt,
    Settle,
    WaitForPosition,
    plan,
)

ENTITY = "cover.test"

# 0 and 100 are the endstops; 3 is where the living-room terrace blind
# actually seats; 5 and 95 sit exactly on the dead band's edge; 34 and 29 are
# the kitchen's real drift pair; None is an unreadable attribute.
POSITIONS = [None, 0, 3, 5, 29, 34, 50, 95, 97, 100]
TILTS = [None, 0, 5, 50, 95, 99, 100]
# -5 and 105 are the out-of-range values a drifted helper really produces.
TARGETS = [KEEP, -5, 0, 3, 34, 50, 95, 100, 105]
# (has_tilt, tilt_after_arrival)
CAPABILITIES = [(True, True), (True, False), (False, True)]


def _blind(has_tilt=True, tilt_after_arrival=True, travel_time=60.0):
    return Blind(
        entity=ENTITY,
        has_tilt=has_tilt,
        tilt_after_arrival=tilt_after_arrival,
        travel_time=travel_time,
    )


def _every_case():
    """Every (capability, current position, current tilt, action) combination."""
    for (has_tilt, after), position, tilt, want_p, want_t in itertools.product(
        CAPABILITIES, POSITIONS, TILTS, TARGETS, TARGETS
    ):
        yield (
            _blind(has_tilt=has_tilt, tilt_after_arrival=after),
            position,
            tilt,
            Action(position=want_p, tilt=want_t),
        )


def _only(commands, kind):
    found = [c for c in commands if isinstance(c, kind)]
    assert len(found) <= 1, f"{kind.__name__} emitted more than once: {commands}"
    return found[0] if found else None


def _clamp(value):
    return max(0, min(100, value))


def _resulting_position(result, current):
    """Where the blind ends up if `result` runs -- read off the plan, not recomputed."""
    command = _only(result.commands, SetPosition)
    return current if command is None else command.position


# --- the grid ---------------------------------------------------------------


def test_a_position_command_is_only_sent_when_it_is_far_enough_to_be_worth_it():
    sent = skipped = 0
    for blind, position, tilt, action in _every_case():
        command = _only(plan(blind, position, tilt, action).commands, SetPosition)
        if command is None:
            skipped += action.position is not KEEP
            continue
        sent += 1
        assert action.position is not KEEP, "KEEP must never produce a position command"
        assert position is None or abs(position - command.position) > DEAD_BAND
    assert sent > 0, "no position command was ever emitted -- the invariant is vacuous"
    assert skipped > 0, "no position command was ever skipped -- the invariant is vacuous"


def test_a_blind_far_from_its_target_always_gets_a_position_command():
    """The other half of the dead band: being close is the only licence to skip."""
    seen = 0
    for blind, position, tilt, action in _every_case():
        if action.position is KEEP:
            continue
        target = _clamp(action.position)
        if position is not None and abs(position - target) <= DEAD_BAND:
            continue
        seen += 1
        assert SetPosition(ENTITY, target) in plan(blind, position, tilt, action).commands
    assert seen > 0


def test_a_tilt_command_is_only_sent_when_it_is_reachable_and_worth_it():
    sent = skipped = 0
    for blind, position, tilt, action in _every_case():
        result = plan(blind, position, tilt, action)
        command = _only(result.commands, SetTilt)
        if command is None:
            skipped += action.tilt is not KEEP
            continue
        sent += 1
        assert blind.has_tilt
        assert action.tilt is not KEEP, "KEEP must never produce a tilt command"
        assert tilt is None or abs(tilt - command.tilt) > DEAD_BAND
        # The angle is only ever set by movement, so a tilt command is only
        # ever legitimate for a blind that does not end up at the top.
        final = _resulting_position(result, position)
        assert final is None or final < TOP_THRESHOLD
    assert sent > 0
    assert skipped > 0


def test_a_blind_without_tilt_never_receives_a_tilt_command():
    seen = 0
    for blind, position, tilt, action in _every_case():
        if blind.has_tilt:
            continue
        seen += 1
        result = plan(blind, position, tilt, action)
        assert not any(isinstance(c, SetTilt) for c in result.commands)
        assert all(c.axis != AXIS_TILT for c in result.clamps)
    assert seen > 0


def test_a_tilt_command_never_precedes_the_arrival_wait():
    waited = paired = 0
    for blind, position, tilt, action in _every_case():
        kinds = [type(c) for c in plan(blind, position, tilt, action).commands]
        if WaitForPosition in kinds:
            waited += 1
            at = kinds.index(WaitForPosition)
            assert SetPosition in kinds, "a wait with nothing to wait for"
            assert SetTilt in kinds, "a wait with nothing waiting on it"
            assert kinds.index(SetPosition) < at
            assert kinds[at + 1] is Settle, "the settle pause must follow the wait"
            assert kinds.index(SetTilt) > at
        if SetPosition in kinds and SetTilt in kinds:
            paired += 1
            assert kinds.index(SetPosition) < kinds.index(SetTilt)
            if blind.tilt_after_arrival:
                assert WaitForPosition in kinds, (
                    "a tilt after a move, with no arrival wait between them: "
                    "the motor discards a tilt that lands mid-travel"
                )
    assert waited > 0
    assert paired > 0


def test_no_emitted_value_lies_outside_0_100():
    clamped = 0
    for blind, position, tilt, action in _every_case():
        result = plan(blind, position, tilt, action)
        for command in result.commands:
            if isinstance(command, SetPosition | WaitForPosition):
                assert 0 <= command.position <= 100
            if isinstance(command, SetTilt):
                assert 0 <= command.tilt <= 100
        for clamp in result.clamps:
            clamped += 1
            assert 0 <= clamp.applied <= 100
            assert not 0 <= clamp.requested <= 100, "an in-range value was reported as clamped"
    assert clamped > 0


def test_every_out_of_range_target_is_reported_exactly_once():
    for blind, position, tilt, action in _every_case():
        expected = []
        if isinstance(action.position, int) and not 0 <= action.position <= 100:
            expected.append(Clamp(ENTITY, AXIS_POSITION, action.position, _clamp(action.position)))
        if blind.has_tilt and isinstance(action.tilt, int) and not 0 <= action.tilt <= 100:
            expected.append(Clamp(ENTITY, AXIS_TILT, action.tilt, _clamp(action.tilt)))
        assert list(plan(blind, position, tilt, action).clamps) == expected


def test_keep_on_one_axis_never_leaks_into_a_command_on_that_axis():
    seen = 0
    for blind, position, tilt, action in _every_case():
        result = plan(blind, position, tilt, action)
        if action.position is KEEP:
            seen += 1
            assert _only(result.commands, SetPosition) is None
            assert _only(result.commands, WaitForPosition) is None
        if action.tilt is KEEP:
            assert _only(result.commands, SetTilt) is None
    assert seen > 0


# --- idempotence, as a property ---------------------------------------------

_AXIS = st.one_of(st.just(KEEP), st.integers(min_value=-50, max_value=150))
_CURRENT = st.one_of(st.none(), st.integers(min_value=0, max_value=100))


def _apply(result: Plan, position, tilt):
    """The state the blind would report once `result` has run."""
    for command in result.commands:
        if isinstance(command, SetPosition):
            position = command.position
        elif isinstance(command, SetTilt):
            tilt = command.tilt
    return position, tilt


@given(
    has_tilt=st.booleans(),
    after=st.booleans(),
    position=_CURRENT,
    tilt=_CURRENT,
    want_position=_AXIS,
    want_tilt=_AXIS,
)
@settings(max_examples=1000, deadline=None)
def test_replanning_from_the_state_the_plan_produces_asks_for_nothing(
    has_tilt, after, position, tilt, want_position, want_tilt
):
    """Idempotence: the matrix recomputes every ~10 minutes and must not keep pushing."""
    blind = _blind(has_tilt=has_tilt, tilt_after_arrival=after)
    action = Action(position=want_position, tilt=want_tilt)

    first = plan(blind, position, tilt, action)
    position, tilt = _apply(first, position, tilt)
    second = plan(blind, position, tilt, action)

    assert second.commands == (), f"second pass still wants {second.commands} after {first}"


@given(
    has_tilt=st.booleans(),
    after=st.booleans(),
    position=_CURRENT,
    tilt=_CURRENT,
    want_position=_AXIS,
    want_tilt=_AXIS,
)
@settings(max_examples=1000, deadline=None)
def test_a_clamp_is_reported_whether_or_not_it_leads_to_a_command(
    has_tilt, after, position, tilt, want_position, want_tilt
):
    blind = _blind(has_tilt=has_tilt, tilt_after_arrival=after)
    action = Action(position=want_position, tilt=want_tilt)
    result = plan(blind, position, tilt, action)

    out_of_range = isinstance(want_position, int) and not 0 <= want_position <= 100
    assert any(c.axis == AXIS_POSITION for c in result.clamps) == out_of_range


# --- the cases this house learned the hard way -------------------------------


def test_closing_a_raised_blind_waits_for_arrival_before_touching_the_slats():
    result = plan(_blind(), 100, 100, Action(position=0, tilt=0))
    assert result.commands == (
        SetPosition(ENTITY, 0),
        WaitForPosition(ENTITY, 0, DEAD_BAND, 90.0),
        Settle(SETTLE_SECONDS),
        SetTilt(ENTITY, 0),
    )


def test_the_arrival_timeout_comes_from_the_blinds_own_travel_time():
    result = plan(_blind(travel_time=120.0), 100, 100, Action(position=0, tilt=0))
    wait = _only(result.commands, WaitForPosition)
    assert wait.timeout == 120.0 * ARRIVAL_TIMEOUT_FACTOR


def test_a_blind_already_down_gets_its_slats_without_a_wait():
    # No movement was commanded, so there is nothing to wait for -- but the
    # slats still have to be set.
    result = plan(_blind(), 0, 100, Action(position=0, tilt=0))
    assert result.commands == (SetTilt(ENTITY, 0),)


def test_a_blind_that_does_not_need_the_wait_gets_the_tilt_immediately():
    result = plan(_blind(tilt_after_arrival=False), 100, 100, Action(position=0, tilt=0))
    assert result.commands == (SetPosition(ENTITY, 0), SetTilt(ENTITY, 0))


def test_the_kitchen_drift_does_not_produce_a_command():
    # Closing the slats moved the reported position from 34 to 29 on its own.
    # Re-sending the absolute 34 is a visible movement, not a no-op -- this is
    # what Lighting SUN did twice in one evening on 2026-08-27.
    assert plan(_blind(), 29, 0, Action(position=34, tilt=0)).commands == ()


def test_a_blind_exactly_on_the_edge_of_the_dead_band_is_left_alone():
    assert plan(_blind(), 5, 0, Action(position=0, tilt=0)).commands == ()
    assert plan(_blind(), 6, 0, Action(position=0, tilt=0)).commands == (SetPosition(ENTITY, 0),)


def test_a_move_with_no_tilt_behind_it_carries_no_wait():
    # The wait exists to gate the tilt command, nothing else. With the slats
    # already where they belong there is nothing for it to gate.
    assert plan(_blind(), 100, 0, Action(position=0, tilt=0)).commands == (SetPosition(ENTITY, 0),)


def test_no_tilt_is_asked_of_a_blind_that_ends_up_at_the_top():
    # The angle is only ever set by movement; from the top it cannot be set.
    assert plan(_blind(), 40, 0, Action(position=100, tilt=100)).commands == (
        SetPosition(ENTITY, 100),
    )
    assert plan(_blind(), 100, 0, Action(position=KEEP, tilt=100)).commands == ()


def test_a_blind_just_below_the_top_still_counts_as_up():
    assert plan(_blind(), 40, 0, Action(position=95, tilt=100)).commands == (
        SetPosition(ENTITY, 95),
    )


def test_an_unreadable_position_sends_the_command_anyway():
    # Safe direction: a command the motor ignores costs nothing; a command
    # silently dropped because the state could not be read is a blind that
    # never moves.
    assert plan(_blind(), None, 0, Action(position=0, tilt=0)).commands[0] == SetPosition(ENTITY, 0)
    assert plan(_blind(), 0, None, Action(position=0, tilt=0)).commands == (SetTilt(ENTITY, 0),)


def test_an_impossible_target_is_clamped_and_reported_even_with_nothing_to_do():
    result = plan(_blind(), 100, 100, Action(position=105, tilt=105))
    assert result.commands == ()
    assert result.clamps == (
        Clamp(ENTITY, AXIS_POSITION, 105, 100),
        Clamp(ENTITY, AXIS_TILT, 105, 100),
    )


def test_a_negative_target_is_clamped_to_zero_and_reported():
    result = plan(_blind(), 100, 100, Action(position=-5, tilt=0))
    assert _only(result.commands, SetPosition) == SetPosition(ENTITY, 0)
    assert result.clamps == (Clamp(ENTITY, AXIS_POSITION, -5, 0),)


def test_keeping_both_axes_plans_nothing_at_all():
    assert plan(_blind(), 50, 50, Action()) == Plan()


def test_an_unresolved_ref_raises_instead_of_being_treated_as_keep():
    ref = Ref(entity="input_number.kvety_pozicia_zaluzie", default=34)
    with pytest.raises(PlannerError, match="unresolved"):
        plan(_blind(), 50, 50, Action(position=ref, tilt=0))


def test_the_plan_and_its_commands_are_frozen():
    result = plan(_blind(), 100, 100, Action(position=0, tilt=0))
    with pytest.raises((AttributeError, TypeError)):
        result.commands = ()
    with pytest.raises((AttributeError, TypeError)):
        result.commands[0].position = 50


# --- Ukotvenie konstant proti domu -------------------------------------
#
# Kazdy iny test v tomto subore importuje konstanty z `planner.py` a overuje
# vztahy MEDZI nimi. To dokazuje, ze planovac je vnutorne konzistentny -- a
# nedokaze nic o tom, ci sedi s domom. `assert wait.timeout == travel_time *
# ARRIVAL_TIMEOUT_FACTOR` je pravda pre AKUKOLVEK hodnotu toho faktora.
#
# Tieto tri testy preto pripinaju holé čísla, ktoré dnes naozaj bežia v
# `/config/scripts.yaml`. Nie su to duplicity -- su to jediné miesto, kde sa
# tichy drift zmeni na padnuty test. Ked sa cislo v dome legitimne zmeni,
# tento test padne a donuti niekoho zmenit oboje naraz. Presne tu ulohu hra
# `conformance.diff_configs` pre konfiguracnu polovicu projektu; pre
# vykonavaciu ziadne orakulum neexistuje (MODELS.md), takze toto je nahrada.


def test_constants_still_match_the_house():
    """Cisla, ktore dnes bezia v `/config/scripts.yaml`, pripnute na tvrdo."""
    # `- delay: {seconds: 2}` medzi dojazdom a tiltom (scripts.yaml 1457, 1517)
    assert SETTLE_SECONDS == 2.0
    # `dol = 95 if c >= 100 else c - 5` -- dom drzi obe cisla oddelene
    assert DEAD_BAND == 5
    assert TOP_THRESHOLD == 95


def test_the_arrival_wait_is_the_ninety_seconds_the_house_waits():
    """`timeout: "00:01:30"` (scripts.yaml 1455, 1515) pri default travel_time."""
    blind = Blind(entity=ENTITY)
    assert blind.travel_time == 60.0
    result = plan(blind, 100, 100, Action(position=0, tilt=0))
    wait = next(c for c in result.commands if isinstance(c, WaitForPosition))
    assert wait.timeout == 90.0


def test_the_top_threshold_is_not_derived_from_the_dead_band():
    """Su to nezavisle fakty; odvodene by sa hybali spolu.

    Preladenie `DEAD_BAND` na 3 (legitimna oprava pre kmitajucu zaluziu) by
    pri odvodeni ticho posunulo hranicu hore na 97, kym dom zostane na 95.
    """
    # Cez verejne API, nie cez `_at_top`: zaujima nas spravanie, nie vnutro.
    blind = _blind()
    at_threshold = plan(blind, TOP_THRESHOLD, 100, Action(position=KEEP, tilt=0))
    assert at_threshold.commands == (), "na hranici uz su lamely nedosiahnutelne"
    below = plan(blind, TOP_THRESHOLD - 1, 100, Action(position=KEEP, tilt=0))
    assert below.commands == (SetTilt(ENTITY, 0),), "tesne pod hranicou sa este daju nastavit"
