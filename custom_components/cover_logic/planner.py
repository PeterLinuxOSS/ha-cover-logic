"""Turn one blind's decided `Action` into the commands that actually realise it.

Pure and total, like the engine: no Home Assistant imports, no service calls,
no clock. `plan()` returns a *description* of what to ask of one cover --
`runner.py` is what will later translate that description into real
`cover.*` service calls.

This is where everything this house learned about these motors is written
down: that a tilt command sent during travel is thrown away, that the angle
can only be changed by moving, that a repeated absolute command is itself a
movement, and that a value outside 0..100 must be pulled back in but never
silently.

See docs/rationale.md -- "`planner.py`" for the reasoning behind every
constant and every skip below; none of them is arbitrary and none of them
should be "simplified" without reading that section first.
"""

from dataclasses import dataclass

from .model import KEEP, Action, Blind, Ref, Value

# How far the reported value may sit from the target before a command is
# worth sending. Not an optimisation: closing the slats changes the reported
# *position* (the motor changes the angle by moving), so a blind that reports
# 34 drifts to 29-30 on its own, and re-sending the same absolute 34 makes it
# visibly jump -- the kitchen did exactly that twice in one evening on
# 2026-08-27. Five points is the threshold the house's own scripts settled
# on: the living-room terrace blind seats at 3%, so a tighter band made every
# recompute consider it "not closed yet" and drive it again.
DEAD_BAND = 5

# Pause between the blind reporting arrival and the tilt command. These
# motors report position 0 slightly before they physically stop, so a tilt
# fired the same instant can still land mid-travel and be discarded.
SETTLE_SECONDS = 2.0

# Arrival timeout as a multiple of the blind's own `travel_time`. A full run
# on these blinds takes ~55 s against a configured `travel_time` of 60, and
# the house's scripts wait 90 s -- 1.5x -- before giving up. Derived rather
# than hard-coded so a slower blind gets a proportionally longer wait from
# its own configuration.
ARRIVAL_TIMEOUT_FACTOR = 1.5

# At or above this the blind is up and the slats cannot be set at all -- the
# motor changes the angle by moving, and from the top there is no movement
# left to change it with.
#
# Its own number, NOT `100 - DEAD_BAND`, so that retuning the dead band --
# a legitimate fix for a blind that chatters -- cannot silently drag this
# with it. That much stands on its own.
#
# WHAT THIS IS NOT: an earlier version of this comment claimed the house
# keeps the two apart the same way, citing `scripts.yaml`'s
# `dol = 95 if c >= 100 else c - 5`. That reading was WRONG and the correction
# is worth keeping. That line lives inside `pozicia_tilt_f`, a *tilt* filter,
# and its 95 is literally `c - 5` evaluated at c=100 -- the dead band, not a
# separate constant. Three lines above it the house says the opposite outright:
# "Prahy su ZAMERNE zhodne s tymi, ktore pouzivaju wait_template a until
# nizsie". So there is no house precedent for separating them; this is our
# decision, and it should be defended as one.
#
# OPEN QUESTION, do not settle it by reading this file: suppressing the tilt
# at all is a planner invention. The house's own tilt filters (`tilt100_f`,
# `tilt50_f`) look ONLY at `current_tilt_position` and never at the position,
# and `zaluzie_otvorit` drives to 100, waits for arrival, and *then* sends
# `open_cover_tilt` three times over -- to every target, including the blinds
# that just went to the top. So at cutover `Action(KEEP, 50)` against a blind
# at 97 makes this module emit nothing while the house sends
# `set_cover_tilt_position: 50`. Whether the motor can act on a tilt near the
# top is a fact about the hardware that only the live `dry_run` day can
# settle; until it does, this threshold is a divergence from the house and
# the project's "parity first, improvements later" rule points at removing it.
TOP_THRESHOLD = 95

# The two axis names, spelled exactly as `model.Action`'s own fields, so a
# `Clamp` report names the axis the configuration names.
AXIS_POSITION = "position"
AXIS_TILT = "tilt"


class PlannerError(Exception):
    """Raised when an action cannot be turned into commands at all."""


@dataclass(frozen=True, slots=True)
class SetPosition:
    """Drive `entity` to `position` (0 = fully closed, 100 = fully open)."""

    entity: str
    position: int


@dataclass(frozen=True, slots=True)
class WaitForPosition:
    """Wait until `entity` reports `position` +/- `tolerance`, at most `timeout` seconds.

    A step, not a sleep: a fixed delay cannot express "the blind has actually
    arrived", and that is the one thing the tilt command depends on.
    """

    entity: str
    position: int
    tolerance: int
    timeout: float


@dataclass(frozen=True, slots=True)
class Settle:
    """Pause for `seconds` before the next command in the sequence."""

    seconds: float


@dataclass(frozen=True, slots=True)
class SetTilt:
    """Set `entity`'s slat angle to `tilt` (0 = closed, 100 = fully open)."""

    entity: str
    tilt: int


Command = SetPosition | WaitForPosition | Settle | SetTilt


@dataclass(frozen=True, slots=True)
class Clamp:
    """One axis value that arrived outside 0..100 and was pulled back into range."""

    entity: str
    axis: str
    requested: int
    applied: int


@dataclass(frozen=True, slots=True)
class Plan:
    """The ordered commands for one blind, plus every clamp that had to be applied.

    Two fields rather than a bare list because a clamp can outlive its
    command: a blind already at 100 that is told 105 is clamped to 100, and
    then emits nothing at all. Reporting the clamp on the command would lose
    exactly the case worth knowing about. See docs/rationale.md --
    "Why a clamp is reported on the `Plan`, not on the command".
    """

    commands: tuple[Command, ...] = ()
    clamps: tuple[Clamp, ...] = ()


def plan(
    blind: Blind,
    current_position: int | None,
    current_tilt: int | None,
    action: Action,
) -> Plan:
    """Describe the commands that would move `blind` from where it is to `action`.

    `current_position`/`current_tilt` are what the cover reports now, or
    `None` when it reports nothing usable (missing attribute, `unavailable`).
    `None` deliberately means "send the command anyway": silently dropping a
    command because the state could not be read is the worse failure of the
    two, and the arrival wait bounds the cost of guessing wrong.

    `action` must already be resolved -- a `Ref` axis raises, because
    resolving one needs a `World` this layer does not have and never should.

    Returns an empty `Plan` when the blind is already where it should be.
    """
    clamps: list[Clamp] = []
    position = _axis_target(blind, AXIS_POSITION, action.position, clamps)
    # A blind with no slats never gets a tilt command, and its tilt axis is
    # not even range-checked: reporting a clamp for a command that can never
    # be sent would be noise on every recompute.
    tilt = _axis_target(blind, AXIS_TILT, action.tilt, clamps) if blind.has_tilt else None

    move_position = position is not None and _off_target(current_position, position)
    # Where the blind ends up once this plan has run -- which is what decides
    # whether the slats are reachable at all, not where it stands right now.
    final_position = position if move_position else current_position
    move_tilt = tilt is not None and not _at_top(final_position) and _off_target(current_tilt, tilt)

    commands: list[Command] = []
    if move_position:
        commands.append(SetPosition(blind.entity, position))
    if move_position and move_tilt and blind.tilt_after_arrival:
        # The whole reason this module returns a sequence instead of a pair of
        # service calls. `tolerance` is DEAD_BAND on purpose: if the threshold
        # that decides "do not send the command" and the one that decides "it
        # has arrived" ever differ, a blind can be simultaneously close enough
        # to skip and never close enough to finish.
        commands.append(
            WaitForPosition(
                blind.entity,
                position,
                DEAD_BAND,
                blind.travel_time * ARRIVAL_TIMEOUT_FACTOR,
            )
        )
        commands.append(Settle(SETTLE_SECONDS))
    if move_tilt:
        commands.append(SetTilt(blind.entity, tilt))

    return Plan(commands=tuple(commands), clamps=tuple(clamps))


def _axis_target(blind: Blind, axis: str, value: Value, clamps: list[Clamp]) -> int | None:
    """The clamped target for one axis, or `None` when the axis is to be left alone.

    `KEEP` is not "the target equals the current value" -- it is "this axis is
    none of our business", which also means an out-of-range *other* axis must
    not drag it into a command.
    """
    if value is KEEP:
        return None
    if isinstance(value, Ref):
        msg = f"blind {blind.entity!r}: unresolved {value!r} on the {axis} axis"
        raise PlannerError(msg)

    applied = max(0, min(100, value))
    if applied != value:
        clamps.append(Clamp(blind.entity, axis, value, applied))
    return applied


def _off_target(current: int | None, target: int) -> bool:
    """Whether `current` is far enough from `target` to be worth a command."""
    return current is None or abs(current - target) > DEAD_BAND


def _at_top(position: int | None) -> bool:
    """Whether a blind at `position` is up, where the slats cannot be set at all.

    Unknown is not "up": the safe direction is to send the tilt command and
    let the motor ignore it, not to skip it on a guess.
    """
    return position is not None and position >= TOP_THRESHOLD
