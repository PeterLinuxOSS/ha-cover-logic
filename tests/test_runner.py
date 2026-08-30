"""Everything about `runner.py` that does not need a Home Assistant runtime.

The division is the one `runner.py`'s own docstring draws: the translation
table, the arrival predicate, queue arbitration, the tilt hand-over and the log
line are module-level functions over plain values, and they are tested here,
under system Python 3.12 with no `homeassistant` installed at all. Only the
task-and-queue machinery itself needs an event loop and a `hass`, and that
lives in `tests/ha/test_runner.py`.

Several assertions here are deliberately written twice -- once for the case
that must hold, once for the case that must *not*. An implication-shaped test
("when X, then Y") passes for an implementation that always does Y, and this
repository has already shipped three defects past a green suite that way.
"""

import pytest

from cover_logic.model import KEEP, Action, Blind, Ref
from cover_logic.planner import (
    DEAD_BAND,
    Clamp,
    Plan,
    SetPosition,
    SetTilt,
    Settle,
    WaitForPosition,
    plan,
)
from cover_logic.runner import (
    CANCEL,
    DROP,
    PEND,
    REASON_DEAD_BAND,
    REASON_NO_TILT,
    Priority,
    _arbitrate,
    _arrived,
    _call_for,
    _carry_over_tilt,
    _command_fields,
    _command_repr,
    _format_data,
    _format_line,
    _remaining_seconds,
    _reported,
    _Request,
    _service_for_position,
    _service_for_tilt,
    _suppressed_fields,
    _suppressions,
)

BLIND = Blind(entity="cover.a")
NO_TILT_BLIND = Blind(entity="cover.b", has_tilt=False)


class FakeState:
    """The two attributes `_reported`/`_arrived` read off a `homeassistant.core.State`.

    A real `State` is constructible without an event loop, but it also
    validates its own entity id and coerces attributes -- neither of which this
    predicate cares about, and both of which would obscure the cases that
    matter here (a missing attribute, a string where a number belongs).
    """

    def __init__(self, state="open", attributes=None):
        self.state = state
        self.attributes = attributes or {}


KEEP_BOTH = Action()


def _request(action=KEEP_BOTH, priority=Priority.SCHEDULED, source="coordinator", mode="vecer"):
    return _Request(blind=BLIND, action=action, priority=priority, source=source, mode=mode)


# ---------------------------------------------------------------------------
# 1. The translation table.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("position", range(101))
def test_every_position_maps_to_exactly_one_service(position):
    """The whole 0..100 grid, the way `test_planner.py` covers its own."""
    service, data = _service_for_position(position)
    if position == 0:
        assert (service, data) == ("close_cover", {})
    elif position == 100:
        assert (service, data) == ("open_cover", {})
    else:
        assert (service, data) == ("set_cover_position", {"position": position})


@pytest.mark.parametrize("tilt", range(101))
def test_every_tilt_maps_to_exactly_one_service(tilt):
    service, data = _service_for_tilt(tilt)
    if tilt == 0:
        assert (service, data) == ("close_cover_tilt", {})
    elif tilt == 100:
        assert (service, data) == ("open_cover_tilt", {})
    else:
        assert (service, data) == ("set_cover_tilt_position", {"tilt_position": tilt})


def test_tilt_100_is_open_cover_tilt_and_never_the_setpoint_service():
    """The measured fact from 2026-07-29: `set_cover_tilt_position: 100` lands on 99.

    Written as two assertions on purpose. The positive one alone would still
    pass if a future refactor made *both* spellings acceptable somewhere
    downstream; the negative one is what actually forbids the setpoint service
    at this value, and it is the one the first mutation flips.
    """
    assert _service_for_tilt(100) == ("open_cover_tilt", {})
    assert _service_for_tilt(100)[0] != "set_cover_tilt_position"


def test_position_0_is_close_cover_and_never_the_setpoint_service():
    """`close_cover` seats on the end stop; `set_cover_position: 0` may stop at 2-3."""
    assert _service_for_position(0) == ("close_cover", {})
    assert _service_for_position(0)[0] != "set_cover_position"


def test_no_service_call_ever_carries_more_than_one_value():
    """A payload with two keys would be a merged command; there is no such thing here."""
    for value in range(101):
        assert len(_service_for_position(value)[1]) <= 1
        assert len(_service_for_tilt(value)[1]) <= 1


def test_only_the_two_acting_commands_translate_to_a_call():
    """A wait is not a service call, and must not accidentally become one."""
    assert _call_for(SetPosition("cover.a", 34)) == ("set_cover_position", {"position": 34})
    assert _call_for(SetTilt("cover.a", 50)) == ("set_cover_tilt_position", {"tilt_position": 50})
    assert _call_for(WaitForPosition("cover.a", 0, DEAD_BAND, 90.0)) is None
    assert _call_for(Settle(2.0)) is None


# ---------------------------------------------------------------------------
# 2. The arrival predicate.
# ---------------------------------------------------------------------------


def test_arrived_is_true_inside_the_tolerance_and_false_outside_it():
    """The positive case, plus the negative one that stops "always True" passing."""
    assert _arrived(FakeState(attributes={"current_position": 3}), 0, DEAD_BAND) is True
    assert _arrived(FakeState(attributes={"current_position": 5}), 0, DEAD_BAND) is True
    assert _arrived(FakeState(attributes={"current_position": 6}), 0, DEAD_BAND) is False


@pytest.mark.parametrize(
    "state",
    [
        None,
        FakeState("unavailable", {"current_position": 0}),
        FakeState("unknown", {"current_position": 0}),
        FakeState(attributes={}),
        FakeState(attributes={"current_position": None}),
        FakeState(attributes={"current_position": "0"}),
        FakeState(attributes={"current_position": True}),
    ],
    ids=["no-state", "unavailable", "unknown", "missing", "none", "string", "bool"],
)
def test_an_unreadable_position_is_never_an_arrival(state):
    """Every way a cover can fail to answer makes the predicate `False`.

    Note the targets: each of these would report "arrived" for target 0 if the
    unreadable value were treated as 0, and `True` in the `bool` case is
    literally `1`. A premature arrival fires the tilt mid-travel and the motor
    throws it away -- which is how slats get lost.
    """
    assert _arrived(state, 0, DEAD_BAND) is False
    assert _arrived(state, 1, DEAD_BAND) is False


def test_a_readable_position_still_arrives_after_all_those_negatives():
    """Counterweight: the predicate is not simply always `False`."""
    assert _arrived(FakeState(attributes={"current_position": 0}), 0, DEAD_BAND) is True


def test_reported_reads_both_axes_and_truncates_a_float():
    state = FakeState(attributes={"current_position": 34.9, "current_tilt_position": 50})
    assert _reported(state, "current_position") == 34
    assert _reported(state, "current_tilt_position") == 50
    assert _reported(state, "current_missing") is None


# ---------------------------------------------------------------------------
# 3. Queue arbitration.
# ---------------------------------------------------------------------------

CLOSE = Action(position=0, tilt=0)
OPEN = Action(position=100, tilt=100)


@pytest.mark.parametrize("running", list(Priority))
@pytest.mark.parametrize("incoming", list(Priority))
def test_arbitration_over_the_whole_priority_grid(running, incoming):
    """Higher priority cancels, equal or lower waits -- and nothing is ever dropped.

    `DROP` is asserted absent on purpose: the actions differ here, so an
    implementation that dropped anything at all would be discarding a genuinely
    different intention, which is silent data loss rather than arbitration.
    """
    verdict = _arbitrate(running, CLOSE, incoming, OPEN)
    assert verdict == (CANCEL if incoming > running else PEND)
    assert verdict != DROP


def test_a_guard_outranks_a_person():
    """Wind protection beats a voice command. The reverse must not hold."""
    assert _arbitrate(Priority.MANUAL, CLOSE, Priority.GUARD, OPEN) == CANCEL
    assert _arbitrate(Priority.GUARD, CLOSE, Priority.MANUAL, OPEN) == PEND


def test_a_person_outranks_a_recompute():
    assert _arbitrate(Priority.SCHEDULED, CLOSE, Priority.MANUAL, OPEN) == CANCEL
    assert _arbitrate(Priority.MANUAL, CLOSE, Priority.SCHEDULED, OPEN) == PEND


def test_priority_values_are_ordered_and_named():
    assert Priority.GUARD > Priority.MANUAL > Priority.SCHEDULED
    assert (Priority.SCHEDULED, Priority.MANUAL, Priority.GUARD) == (10, 20, 30)


@pytest.mark.parametrize("priority", list(Priority))
def test_an_identical_request_at_the_same_standing_is_dropped(priority):
    """The blind is already doing exactly this; queueing it again would re-move it."""
    assert _arbitrate(priority, CLOSE, priority, Action(position=0, tilt=0)) == DROP


def test_an_identical_action_at_a_higher_standing_is_not_dropped():
    """Only equal standing drops. A guard repeating the running action still takes over."""
    assert _arbitrate(Priority.SCHEDULED, CLOSE, Priority.GUARD, CLOSE) == CANCEL


# ---------------------------------------------------------------------------
# 4. The tilt hand-over -- the 2026-08-21 incident, as a function.
# ---------------------------------------------------------------------------

ABANDONED = SetTilt("cover.a", 0)


def test_an_abandoned_tilt_is_carried_when_the_successor_keeps_the_slat_axis():
    """`keep` is delegation, not "do nothing": nobody else owns the slats."""
    successor = Plan(commands=(SetPosition("cover.a", 100),))
    carried = _carry_over_tilt(ABANDONED, successor, Action(position=100, tilt=KEEP))
    assert carried.commands == (SetPosition("cover.a", 100), ABANDONED)


def test_an_abandoned_tilt_is_carried_even_onto_an_empty_plan():
    """The blind is already where the successor wants it -- the slats still are not."""
    carried = _carry_over_tilt(ABANDONED, Plan(), Action(position=100, tilt=KEEP))
    assert carried.commands == (ABANDONED,)


def test_an_abandoned_tilt_is_dropped_when_the_successor_names_a_tilt():
    successor = Plan(commands=(SetPosition("cover.a", 100), SetTilt("cover.a", 100)))
    carried = _carry_over_tilt(ABANDONED, successor, Action(position=100, tilt=100))
    assert carried.commands == successor.commands
    assert ABANDONED not in carried.commands


def test_an_abandoned_tilt_is_dropped_when_the_dead_band_swallowed_the_successors_tilt():
    """The condition is on the `Action`, not on the `Plan` -- this is why.

    The successor owns the slat axis (`tilt=100`) but `plan()` emitted no
    `SetTilt`, because the blind already reports 100. Asking the plan "does it
    contain a `SetTilt`?" would answer no and re-send the abandoned command --
    moving the slats for no reason, which is the 2026-08-27 class of bug. The
    action is what says who owns the axis.
    """
    blind = Blind(entity="cover.a", tilt_after_arrival=False)
    action = Action(position=100, tilt=100)
    successor = plan(blind, 100, 100, action)
    assert successor.commands == ()

    carried = _carry_over_tilt(ABANDONED, successor, action)
    assert carried.commands == ()


def test_nothing_is_carried_when_nothing_was_abandoned():
    successor = Plan(commands=(SetPosition("cover.a", 100),))
    assert _carry_over_tilt(None, successor, Action(position=100, tilt=KEEP)) is successor


def test_carrying_preserves_the_successors_clamps():
    """A hand-over must not quietly lose the successor's own out-of-range report."""
    clamp = Clamp("cover.a", "position", 105, 100)
    successor = Plan(commands=(SetPosition("cover.a", 100),), clamps=(clamp,))
    carried = _carry_over_tilt(ABANDONED, successor, Action(position=105, tilt=KEEP))
    assert carried.clamps == (clamp,)


# ---------------------------------------------------------------------------
# 5. Suppressed commands.
# ---------------------------------------------------------------------------


def test_a_dead_banded_axis_is_reported_as_suppressed():
    action = Action(position=34, tilt=KEEP)
    computed = plan(BLIND, 32, None, action)
    assert computed.commands == ()

    suppressed = _suppressions(BLIND, action, computed, 32, None)
    assert [(s.axis, s.target, s.current, s.reason) for s in suppressed] == [
        ("position", 34, 32, REASON_DEAD_BAND)
    ]


def test_an_axis_that_did_produce_a_command_is_not_reported():
    """Counterweight: this is a report of omissions, not of every axis."""
    action = Action(position=0, tilt=KEEP)
    computed = plan(BLIND, 100, None, action)
    assert _suppressions(BLIND, action, computed, 100, None) == ()


def test_a_kept_axis_is_never_reported():
    """Nobody asked for it; a line saying so would be noise on every recompute."""
    action = Action(position=KEEP, tilt=KEEP)
    assert _suppressions(BLIND, action, plan(BLIND, 50, 50, action), 50, 50) == ()


def test_a_tilt_on_a_slatless_blind_is_reported_with_its_own_reason():
    action = Action(position=KEEP, tilt=100)
    computed = plan(NO_TILT_BLIND, None, None, action)
    suppressed = _suppressions(NO_TILT_BLIND, action, computed, None, None)
    assert [(s.axis, s.reason) for s in suppressed] == [("tilt", REASON_NO_TILT)]


def test_a_clamped_target_is_reported_at_the_value_actually_planned():
    """`target=` is the comparison key against the old scripts, so it must be the applied one."""
    action = Action(position=105, tilt=KEEP)
    computed = plan(BLIND, 100, None, action)
    assert computed.commands == ()
    suppressed = _suppressions(BLIND, action, computed, 100, None)
    assert [(s.target, s.reason) for s in suppressed] == [(100, REASON_DEAD_BAND)]


def test_an_unresolved_ref_never_reaches_the_suppression_report():
    """`plan()` raises on one first; this only asserts the report cannot invent a target."""
    action = Action(position=Ref("input_number.x", 34), tilt=KEEP)
    assert _suppressions(BLIND, action, Plan(), None, None) == ()


# ---------------------------------------------------------------------------
# 6. The log line.
# ---------------------------------------------------------------------------


def test_the_dry_run_and_live_lines_differ_only_in_the_prefix_and_the_verb():
    """One formatter, one switch -- otherwise the two logs cannot be compared."""
    request = _request(action=Action(position=0, tilt=KEEP))
    args = (SetPosition("cover.a", 0), 1, 4, ("close_cover", {}), 87, 100)

    dry = _format_line(True, _command_fields("7f3a", request, *args, True))
    live = _format_line(False, _command_fields("7f3a", request, *args, False))

    assert dry == (
        "cover_logic[dry_run] seq=7f3a blind=cover.a prio=SCHEDULED src=coordinator "
        "mode=vecer step=1/4 cmd=SetPosition(0) would_call=cover.close_cover data={} "
        "pos=87 tilt=100"
    )
    assert live == dry.replace("[dry_run]", "[live]").replace("would_call=", "called=")


def test_a_live_line_never_says_would_call():
    """The one field a dry-run day's comparison keys off must not lie about the mode."""
    fields = _command_fields(
        "7f3a", _request(), SetTilt("cover.a", 100), 4, 4, ("open_cover_tilt", {}), 0, 0, False
    )
    assert "called" in fields
    assert "would_call" not in fields


def test_a_wait_is_logged_with_its_step_and_no_call():
    fields = _command_fields(
        "7f3a",
        _request(),
        WaitForPosition("cover.a", 0, DEAD_BAND, 90.0),
        2,
        4,
        None,
        87,
        100,
        True,
    )
    assert fields["step"] == "2/4"
    assert fields["cmd"] == "WaitForPosition(0+-5,90s)"
    assert fields["would_call"] == "none"


def test_an_unreadable_axis_is_logged_as_a_question_mark_not_a_zero():
    fields = _command_fields(
        "7f3a", _request(), SetPosition("cover.a", 0), 1, 1, ("close_cover", {}), None, None, True
    )
    assert (fields["pos"], fields["tilt"]) == ("?", "?")


def test_a_suppressed_line_names_the_axis_the_reason_and_both_numbers():
    """`pos` and `target` together are what tell a dead band from an interlock."""
    action = Action(position=34, tilt=KEEP)
    computed = plan(BLIND, 32, None, action)
    (suppressed,) = _suppressions(BLIND, action, computed, 32, None)

    line = _format_line(True, _suppressed_fields("7f3a", _request(action), suppressed))

    assert line == (
        "cover_logic[dry_run] seq=7f3a blind=cover.a prio=SCHEDULED src=coordinator "
        "mode=vecer step=-/- cmd=none axis=position reason=dead_band target=34 pos=32"
    )


def test_no_log_field_value_ever_contains_a_space():
    """The line is `key=value` pairs split on whitespace; a space inside one breaks it."""
    request = _request(action=Action(position=34, tilt=50))
    commands = [
        SetPosition("cover.a", 34),
        WaitForPosition("cover.a", 34, DEAD_BAND, 90.0),
        Settle(2.0),
        SetTilt("cover.a", 50),
    ]
    for step, command in enumerate(commands, 1):
        fields = _command_fields(
            "7f3a", request, command, step, 4, _call_for(command), 87, 100, True
        )
        for key, value in fields.items():
            assert " " not in str(value), f"{key} contains a space: {value!r}"


def test_service_data_renders_compactly_for_both_shapes():
    assert _format_data({}) == "{}"
    assert _format_data({"position": 34}) == "{position:34}"


def test_command_repr_is_not_the_dataclass_repr():
    """The dataclass repr contains ", " and repeats the entity; both break the line."""
    command = SetPosition("cover.a", 34)
    assert _command_repr(command) == "SetPosition(34)"
    assert _command_repr(command) != repr(command)
    assert _command_repr(Settle(2.0)) == "Settle(2s)"
    assert _command_repr(SetTilt("cover.a", 100)) == "SetTilt(100)"


# ---------------------------------------------------------------------------
# 7. Shutdown grace.
# ---------------------------------------------------------------------------


def test_remaining_seconds_counts_only_the_waits_and_only_the_tail():
    commands = (
        SetPosition("cover.a", 0),
        WaitForPosition("cover.a", 0, DEAD_BAND, 90.0),
        Settle(2.0),
        SetTilt("cover.a", 0),
    )
    whole = Plan(commands=commands)
    assert _remaining_seconds(whole, 0) == 92.0
    assert _remaining_seconds(whole, 2) == 2.0
    assert _remaining_seconds(whole, 4) == 0.0
