"""Execute a decided `Action`: the one module in this package with hands.

Everything else in `cover_logic` decides. This module is what turns a decision
into movement -- and it is deliberately the only place that knows a Home
Assistant service name exists. It takes an `Action`, not a `Plan`: a plan is a
description of the route from *here* to *there*, computed against a snapshot of
where the blind is, and a plan that sat forty seconds in a queue is a plan
computed against a position the blind no longer has. `plan()` is therefore
called at the moment a sequence actually starts, never when it is requested.

Three properties are load-bearing, and each of them is an incident this house
already paid for:

- **One entity per service call, never a list.** A call that names five covers
  cannot be cancelled for one of them, and per-blind cancellation is what the
  queue below exists to provide.
- **One queue per blind, and the queues run in parallel.** Serialising the
  whole house moves it in waves (2026-08-05: it woke two people). Serialisation
  is per blind and never wider.
- **A cancellation always has a successor.** "Stop this" as a standalone
  operation does not exist here, because that is exactly what left a blind
  down with untouched slats on 2026-08-21. See `_carry_over_tilt`.

**Why the service call is injected.** `CoverRunner` is handed an awaitable
`call_cover(service, data)` rather than reaching for `hass.services` itself.
That is the seam that lets the queue arbitration, the cancellation rules and
the whole translation table be tested without a Home Assistant runtime at all.
Since phase 3 task 5 the coordinator binds it to the real thing
(`hass.services.async_call`, blocking -- see `docs/rationale.md`), so this
module now genuinely has hands. What holds them is `dry_run`, read live off the
entry's options on every sequence and defaulting to `True`: below, `_execute`
returns before the caller whenever it is on, so a fresh install describes and
moves nothing until someone turns that option off.

`observer` is the other end of the same idea: an optional, synchronous
`(kind, fields)` callback fired from the one funnel every line here goes
through (`_log_fields`), so a dry run is visible somewhere other than the log.
It may not raise into a sequence and it may not block one -- something that
merely watches must never be able to stop a blind mid-movement.

**Why the Home Assistant imports are deferred.** Like `__init__.py` (see its
own docstring), and unlike `coordinator.py`: the pure test run under system
Python 3.12 has no `homeassistant` installed, and the module-level helpers
below (`_service_for_position`, `_arrived`, `_arbitrate`, `_carry_over_tilt`,
`_suppressions`) are meant to be tested there. So every `homeassistant` name is
either behind `TYPE_CHECKING` or imported inside the one function that needs
it. `runner.py` is *not* in `tests/test_purity.py`'s `PURE_MODULES` and must
not be added: it genuinely touches `hass`, it just does not do so on import.

What this module must not do is as important as what it does: it never
decides (no mode, no zone, no rule), never evaluates a guard, never clamps or
applies a dead band or reorders commands (all of that is `plan()`), never
merges two blinds into one call, never debounces (the coordinator does), and
never writes configuration -- in particular it never turns `dry_run` off by
itself. An empty `Plan` is a complete answer, not a missing one.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum
import logging
from typing import TYPE_CHECKING, Any
import uuid

from .const import (
    COMMAND_CALLED,
    COMMAND_SUPPRESSED,
    COMMAND_WOULD_CALL,
    DEFAULT_DRY_RUN,
    OPT_DRY_RUN,
)
from .model import KEEP, Action, Blind
from .planner import (
    AXIS_POSITION,
    AXIS_TILT,
    Clamp,
    Command,
    Plan,
    PlannerError,
    SetPosition,
    SetTilt,
    Settle,
    WaitForPosition,
    plan,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State

_LOGGER = logging.getLogger(__name__)

# The only service domain this module ever calls. Not a parameter: a runner
# that could be pointed at another domain would be a general-purpose service
# caller, and the promise of this package is that it moves covers and nothing
# else.
COVER_DOMAIN = "cover"

# The two cover attributes this module reads, and the only two things it reads
# from `hass.states` at all. Spelled out here rather than imported from
# `homeassistant.components.cover` so the module stays importable without Home
# Assistant (see the module docstring).
ATTR_CURRENT_POSITION = "current_position"
ATTR_CURRENT_TILT_POSITION = "current_tilt_position"

# The two ends of both axes. Named so the translation table below reads as
# "at the end stop" rather than as two bare numbers -- and so the choice of
# `close_cover`/`open_cover` over the setpoint service at exactly these two
# values is impossible to mistake for an off-by-one.
FULLY_CLOSED = 0
FULLY_OPEN = 100

# States in which a cover reports nothing usable. An unreadable position makes
# the arrival predicate `False` -- never "arrived" -- which is the direct
# translation of what the house's own templates express as `map('int', <an
# unsatisfiable default>)`; see `_arrived`.
UNREADABLE_STATES = frozenset({"unavailable", "unknown"})

# Upper bound on how long `async_shutdown` waits for sequences already in
# flight. The real grace is the plan's own remaining time (arrival waits plus
# settles), capped here so a blind configured with an absurd `travel_time`
# cannot hold up a Home Assistant restart.
SHUTDOWN_GRACE_CAP = 30.0

# What `_arbitrate` can answer. Strings rather than an enum because they are a
# closed three-way verdict read in exactly one place; naming them keeps the
# call sites and the tests spelling them identically.
CANCEL = "cancel"
PEND = "pend"
DROP = "drop"

# Why a command that had a target was not sent. `dead_band` is the interesting
# one: it is the difference between "the runner stood still" and "the runner
# never got asked", which is the single most valuable distinction a dry-run day
# produces (2026-08-27 was a movement nobody could attribute).
REASON_DEAD_BAND = "dead_band"
REASON_NO_TILT = "no_tilt"


class Priority(IntEnum):
    """Who asked for a movement -- which is what decides who wins.

    Not a number the caller invents. There are exactly three askers, and the
    order between them is a safety decision: `GUARD` outranks `MANUAL` on
    purpose. An interlock exists to prevent a movement that would be wrong --
    wind, an open door, a running sauna -- so when a person says "close the
    blinds" and the sauna door is open, the interlock has to win. The other
    way round would make an interlock a suggestion.
    """

    SCHEDULED = 10
    MANUAL = 20
    GUARD = 30


# How a `Command` is actually issued. One service, one payload, one entity --
# the entity is in `data["entity_id"]`, always a single id (see the module
# docstring on why never a list).
CoverCall = Callable[[str, dict[str, Any]], Awaitable[None]]

# An optional second pair of eyes on every line this module logs: the same
# `kind` and the same already-built field mapping, handed to whoever wants to
# keep it somewhere a person can see without tailing a log. Synchronous and
# return-less on purpose -- an observer that could block or fail would be able
# to delay or break a movement, and nothing that merely watches may do that.
CommandObserver = Callable[[str, Mapping[str, object]], None]


# ---------------------------------------------------------------------------
# Translation: `Command` -> a `cover.*` service and its data.
#
# Both extremes deliberately use the "drive to the end stop" service rather
# than the setpoint one. For tilt this is measured: `set_cover_tilt_position:
# 100` lands on 99 on these motors (documented 2026-07-29, with the comment
# still sitting in the house's `zaluzie_uplatnit`), and 99 fails every equality
# check for "are the slats open?". For position the reason is the same in
# direction if weaker in degree: `close_cover` seats on the end stop, whereas
# `set_cover_position: 0` may stop at 2-3, which the arrival predicate accepts
# as "closed" although the blind never physically seated.
# ---------------------------------------------------------------------------


def _service_for_position(position: int) -> tuple[str, dict[str, Any]]:
    """The `cover` service and data that drive a blind to `position`."""
    if position <= FULLY_CLOSED:
        return ("close_cover", {})
    if position >= FULLY_OPEN:
        return ("open_cover", {})
    return ("set_cover_position", {"position": position})


def _service_for_tilt(tilt: int) -> tuple[str, dict[str, Any]]:
    """The `cover` service and data that set a blind's slats to `tilt`."""
    if tilt <= FULLY_CLOSED:
        return ("close_cover_tilt", {})
    if tilt >= FULLY_OPEN:
        return ("open_cover_tilt", {})
    return ("set_cover_tilt_position", {"tilt_position": tilt})


def _call_for(command: Command) -> tuple[str, dict[str, Any]] | None:
    """The service call `command` becomes, or `None` if it is a wait, not an action."""
    if isinstance(command, SetPosition):
        return _service_for_position(command.position)
    if isinstance(command, SetTilt):
        return _service_for_tilt(command.tilt)
    return None


# ---------------------------------------------------------------------------
# Reading the blind.
# ---------------------------------------------------------------------------


def _reported(state: "State | None", attribute: str) -> int | None:
    """What `state` reports for `attribute`, or `None` when it reports nothing usable.

    `None` covers all four ways a cover can fail to answer: no state object at
    all, an `unavailable`/`unknown` state, a missing attribute, and an
    attribute that is not a number. `bool` is excluded explicitly for the same
    reason `planner._axis_target` excludes it -- `bool` is a subclass of `int`,
    so `True` would otherwise sail through as position 1.
    """
    if state is None or state.state in UNREADABLE_STATES:
        return None
    value = state.attributes.get(attribute)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def current_position(state: "State | None") -> int | None:
    """What a cover reports as its position, or `None` when it reports nothing usable.

    Public because `guards.review` needs a `positions` map and must be given
    exactly the number this module would act on. Two readers of the same
    attribute, each deciding for itself what `unavailable` or a non-numeric
    value means, is the class of duplication `MODELS.md` §9 names: a directional
    guard could then judge a movement the runner sees differently, and the
    disagreement would only ever surface on a blind that was already broken.
    """
    return _reported(state, ATTR_CURRENT_POSITION)


def _arrived(state: "State | None", target: int, tolerance: int) -> bool:
    """Whether `state` says the blind has reached `target` within `tolerance`.

    An unreadable position is **not** an arrival. A blind that drops into
    `unavailable` mid-travel therefore burns its whole timeout and then
    continues (loudly) rather than being declared arrived early -- which is the
    right trade, because a premature "it is there" fires the tilt command
    mid-travel and the motor throws it away. Ninety seconds is the price of not
    losing the slats.

    `tolerance` is `planner.DEAD_BAND` and is passed in on the command rather
    than recomputed: if the threshold that decides "do not send this" and the
    one that decides "it has arrived" ever drifted apart, a blind could be
    simultaneously close enough to skip and never close enough to finish.
    """
    current = _reported(state, ATTR_CURRENT_POSITION)
    return current is not None and abs(current - target) <= tolerance


# ---------------------------------------------------------------------------
# Queue arbitration and clean cancellation.
# ---------------------------------------------------------------------------


def _arbitrate(
    running_priority: Priority,
    running_action: Action,
    incoming_priority: Priority,
    incoming_action: Action,
) -> str:
    """What to do with an incoming request when a sequence is already running.

    Three answers and no fourth: `CANCEL` the running sequence in favour of the
    newcomer, `PEND` the newcomer in the one waiting slot (overwriting whatever
    was there), or `DROP` it because it asks for exactly what is already
    happening at exactly the same standing.

    The waiting slot holds one request, not a list. A queue of depth *k* means
    the blind visibly performs *k-1* stale intentions, one movement each -- and
    this house has already paid twice for a blind that moved for no reason a
    person could see (2026-08-27) and for the whole house moving in waves
    (2026-08-05). The newest request of equal or lower standing is the only one
    still worth performing.
    """
    if incoming_priority == running_priority and incoming_action == running_action:
        return DROP
    if incoming_priority > running_priority:
        return CANCEL
    return PEND


def _carry_over_tilt(abandoned: SetTilt | None, successor: Plan, action: Action) -> Plan:
    """Hand an abandoned slat command to the successor -- or deliberately drop it.

    The condition is exact, and it is on the **`Action`**, not on the `Plan`:
    carry the abandoned `SetTilt` over precisely when `action.tilt is KEEP`.

    - `KEEP` means "the slat axis is none of my business" (see memory
      `zaluzie-nechat-je-delegacia`: `keep` is delegation, not "do nothing").
      Nobody owns that axis, so the abandoned command is still its only owner
      and must survive -- otherwise the blind ends its movement with untouched
      slats, which is the 2026-08-21 incident verbatim.
    - When the successor *does* name a tilt, it owns the axis and the abandoned
      value is correctly discarded -- even if `plan()` emitted no `SetTilt` for
      it because the dead band filtered it out. Asking the `Plan` instead of
      the `Action` would re-send exactly the command the dead band just
      suppressed, and the blind would move for no reason.

    **Known wrinkle, deliberately left as designed.** The abandoned command is
    appended, not folded back into the successor's `Action` and re-planned. So
    when the successor also moves the *position*, the carried tilt goes out
    with no arrival wait in front of it (`plan()` only inserts one when both
    axes move), and these motors discard a tilt that lands mid-travel. That is
    still strictly better than the alternative -- a discarded tilt is no worse
    than a tilt that was never sent, which is the incident -- but it is not
    free, and the fix, if it is wanted, is to re-plan the successor with
    `tilt=abandoned.tilt` rather than to append here. Raised rather than
    changed unilaterally, because appending is what the design says.
    """
    if abandoned is None or action.tilt is not KEEP:
        return successor
    return Plan(commands=(*successor.commands, abandoned), clamps=successor.clamps)


def _remaining_seconds(plan_: Plan, index: int) -> float:
    """How long the untouched tail of `plan_` may still legitimately take.

    Only the waits count: a service call returns as fast as Home Assistant
    dispatches it, while an arrival wait and a settle are the sequence's real
    duration. Used to size the shutdown grace off the plan itself instead of a
    single invented number.
    """
    total = 0.0
    for command in plan_.commands[index:]:
        if isinstance(command, WaitForPosition):
            total += command.timeout
        elif isinstance(command, Settle):
            total += command.seconds
    return total


# ---------------------------------------------------------------------------
# Suppressed commands: what the runner deliberately did not send.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Suppressed:
    """One axis that was given a target and produced no command anyway."""

    axis: str
    target: int
    current: int | None
    reason: str


def _suppressions(
    blind: Blind,
    action: Action,
    computed: Plan,
    position: int | None,
    tilt: int | None,
) -> tuple[_Suppressed, ...]:
    """Every axis `action` named that `computed` does not act on, and why.

    Without this, "the runner did nothing" and "the runner was never asked" log
    identically, and the most valuable finding of a dry-run day -- the places
    where this runner stands still while the old script moves -- is invisible.
    An axis left `KEEP` is not reported: nothing was asked for, so a line
    saying nothing happened would be noise on every single recompute.
    """
    sent_position = any(isinstance(c, SetPosition) for c in computed.commands)
    sent_tilt = any(isinstance(c, SetTilt) for c in computed.commands)
    out: list[_Suppressed] = []
    for axis, value, current, sent in (
        (AXIS_POSITION, action.position, position, sent_position),
        (AXIS_TILT, action.tilt, tilt, sent_tilt),
    ):
        if sent or not isinstance(value, int) or isinstance(value, bool):
            # Not an int: `KEEP` (nobody asked) or a `Ref`, which `plan()`
            # would already have raised on before this function is reached.
            continue
        reason = REASON_NO_TILT if axis == AXIS_TILT and not blind.has_tilt else REASON_DEAD_BAND
        out.append(_Suppressed(axis, _applied(computed.clamps, axis, value), current, reason))
    return tuple(out)


def _applied(clamps: tuple[Clamp, ...], axis: str, requested: int) -> int:
    """The value actually planned for `axis` -- the clamped one where a clamp applied."""
    for clamp in clamps:
        if clamp.axis == axis and clamp.requested == requested:
            return clamp.applied
    return requested


# ---------------------------------------------------------------------------
# The log line. One shape for both modes: a dry-run day is a set comparison
# against `/api/logbook`, and two logs in two shapes cannot be compared.
# ---------------------------------------------------------------------------


def _format_line(dry_run: bool, fields: Mapping[str, object]) -> str:
    """`cover_logic[dry_run|live] k=v k=v ...` -- one greppable line, no spaces in values."""
    prefix = "cover_logic[dry_run]" if dry_run else "cover_logic[live]"
    body = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{prefix} {body}"


def _command_repr(command: Command) -> str:
    """A compact, space-free rendering of one command.

    Not `repr(command)`: the dataclass repr contains ", " separators, which
    would break a line whose whole point is that `key=value` pairs are
    space-delimited, and it repeats the entity that the line's own `blind=`
    field already carries.
    """
    if isinstance(command, SetPosition):
        return f"SetPosition({command.position})"
    if isinstance(command, SetTilt):
        return f"SetTilt({command.tilt})"
    if isinstance(command, WaitForPosition):
        return f"WaitForPosition({command.position}+-{command.tolerance},{command.timeout:g}s)"
    return f"Settle({command.seconds:g}s)"


def _format_data(data: Mapping[str, Any]) -> str:
    """Service data as `{}` or `{position:34}` -- the payload half of the comparison key."""
    return "{" + ",".join(f"{key}:{value}" for key, value in data.items()) + "}"


def _axis_value(value: int | None) -> str:
    """A reported axis for the log: the number, or `?` when the cover reports nothing."""
    return "?" if value is None else str(value)


# ---------------------------------------------------------------------------
# Requests, sequences, queues.
# ---------------------------------------------------------------------------


@dataclass
class _Request:
    """One asked-for movement. Not a `Plan` -- see the module docstring."""

    blind: Blind
    action: Action
    priority: Priority
    source: str
    mode: str
    carried_tilt: SetTilt | None = None


def _base_fields(seq_id: str, request: _Request) -> dict[str, object]:
    """The five fields every line of one sequence shares.

    `prio` and `src` together are the field that answers "why did it move" --
    the question `last_triggered` provably cannot answer once a blind has moved
    twice inside the window being investigated. `mode` is what tells a
    difference against the old scripts apart as belonging to the decision or to
    the execution; without it every difference is suspect of both.
    """
    return {
        "seq": seq_id,
        "blind": request.blind.entity,
        "prio": request.priority.name,
        "src": request.source or "-",
        "mode": request.mode or "-",
    }


def _command_fields(
    seq_id: str,
    request: _Request,
    command: Command,
    step: int,
    total: int,
    call: tuple[str, dict[str, Any]] | None,
    position: int | None,
    tilt: int | None,
    dry_run: bool,
) -> dict[str, object]:
    """One command's line, in whichever of the two modes is running.

    The only difference between the modes is the key: `would_call` in a dry
    run, `called` live. One formatter, one switch -- two logs written in two
    shapes cannot be compared against each other, and comparing them is the
    entire purpose of a dry-run day.
    """
    fields = _base_fields(seq_id, request)
    fields["step"] = f"{step}/{total}"
    fields["cmd"] = _command_repr(command)
    service, data = call if call is not None else (None, {})
    fields["would_call" if dry_run else "called"] = (
        "none" if service is None else f"{COVER_DOMAIN}.{service}"
    )
    fields["data"] = _format_data(data)
    fields["pos"] = _axis_value(position)
    fields["tilt"] = _axis_value(tilt)
    return fields


def _suppressed_fields(
    seq_id: str, request: _Request, suppressed: _Suppressed
) -> dict[str, object]:
    """The same line shape for an axis that was asked for and deliberately not driven.

    `step=-/-` because a suppressed command has no position in the sequence --
    it never entered one. Without this line, "nothing happened" and "nothing
    was logged" are indistinguishable, and the most valuable finding of a
    dry-run day is exactly where this runner stands still and the old script
    moves.
    """
    fields = _base_fields(seq_id, request)
    fields["step"] = "-/-"
    fields["cmd"] = "none"
    fields["axis"] = suppressed.axis
    fields["reason"] = suppressed.reason
    fields["target"] = suppressed.target
    fields["pos"] = _axis_value(suppressed.current)
    return fields


class _Sequence:
    """One running request: its plan, how far it got, and its cancel flag.

    Cancellation is an `asyncio.Event`, not `Task.cancel()`. The running task
    checks it between commands and races it against every wait, so a
    cancellation lands on a command boundary and never in the middle of an
    awaited service call -- and the commands it never sent stay readable in
    `unsent`, which is what makes both the tilt hand-over and the shutdown
    report possible.
    """

    def __init__(self, request: _Request, seq_id: str, dry_run: bool) -> None:
        """Start at command zero with an empty plan; `_run` fills both in."""
        self.request = request
        self.id = seq_id
        self.dry_run = dry_run
        self.plan = Plan()
        self.index = 0
        self.cancelled = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    @property
    def unsent(self) -> tuple[Command, ...]:
        """The tail of the plan that was never dispatched."""
        return self.plan.commands[self.index :]

    @property
    def unsent_tilt(self) -> SetTilt | None:
        """The slat command this sequence owed and did not send, if any."""
        return next((c for c in self.unsent if isinstance(c, SetTilt)), None)


def _grace_for(sequence: _Sequence) -> float:
    """How long `async_shutdown` should be willing to wait for one sequence.

    A sequence that has not planned yet cannot say how long it needs -- it was
    created in this very tick and `_run` has not been scheduled once. The cap is
    the honest answer there, and it costs nothing: the grace is a *timeout* on
    `asyncio.wait`, so a sequence that turns out to have nothing to do still
    returns immediately. Deriving 0.0 from its empty plan instead would abandon
    every sequence that had not started yet, which is the opposite of the rule
    this whole method exists to keep.
    """
    if not sequence.plan.commands:
        return SHUTDOWN_GRACE_CAP
    return _remaining_seconds(sequence.plan, sequence.index)


class _BlindQueue:
    """One blind's serialisation point: at most one running, at most one waiting."""

    def __init__(self) -> None:
        """Start empty."""
        self.running: _Sequence | None = None
        self.pending: _Request | None = None


class CoverRunner:
    """Execute decided actions against real covers, one queue per blind.

    Construction takes the service caller rather than reaching for
    `hass.services` -- see the module docstring for why that seam exists and
    what it is not.
    """

    def __init__(
        self,
        hass: "HomeAssistant",
        entry: "ConfigEntry",
        call_cover: CoverCall,
        *,
        observer: CommandObserver | None = None,
    ) -> None:
        """Store the runtime handles; nothing is scheduled until `async_apply`."""
        self.hass = hass
        self._entry = entry
        self._call_cover = call_cover
        self._observer = observer
        self._queues: dict[str, _BlindQueue] = {}
        self._clamped: set[tuple[str, str, int]] = set()
        self._closing = False

    # -- Public API ------------------------------------------------------

    @property
    def in_flight(self) -> dict[str, dict[str, str]]:
        """Read-only: what each blind is doing and what is waiting behind it.

        A view, never a handle -- plain strings, no `_Sequence`, no `_Request`,
        nothing a caller could cancel or mutate. It exists so "what is queued"
        is answerable from the diagnostic sensor during the dry-run day, which
        is the one question the log genuinely cannot answer: a log says what
        already happened, and a queue is about what has not happened yet.
        """
        out: dict[str, dict[str, str]] = {}
        for entity, queue in sorted(self._queues.items()):
            view: dict[str, str] = {}
            if queue.running is not None:
                request = queue.running.request
                view["running"] = f"{request.priority.name}/{request.source or '-'}"
                view["seq"] = queue.running.id
                view["step"] = f"{queue.running.index}/{len(queue.running.plan.commands)}"
            if queue.pending is not None:
                view["waiting"] = f"{queue.pending.priority.name}/{queue.pending.source or '-'}"
            if view:
                out[entity] = view
        return out

    async def async_apply(
        self,
        blind: Blind,
        action: Action,
        *,
        priority: Priority,
        source: str,
        mode: str = "",
    ) -> None:
        """Ask for `blind` to be moved to `action`, arbitrating against what it is doing.

        Returns as soon as the request has been placed, not when the blind has
        finished moving: a caller that blocked here would serialise the house
        through itself, which is the thing per-blind queues exist to prevent.
        Use `async_wait_idle` to await completion.

        `source` and `mode` are carried only into the log, and they are the
        fields that answer "why did it move" -- the question `last_triggered`
        provably cannot answer once a blind has moved twice in one window.
        """
        request = _Request(blind, action, priority, source, mode)
        queue = self._queues.get(blind.entity)
        if queue is None:
            queue = _BlindQueue()
            self._queues[blind.entity] = queue

        running = queue.running
        if running is None:
            self._start(queue, request)
            return

        verdict = _arbitrate(running.request.priority, running.request.action, priority, action)
        if verdict == DROP:
            _LOGGER.debug(
                "cover_logic: %s already running the same action at %s, dropping %s request",
                blind.entity,
                priority.name,
                source,
            )
            return

        queue.pending = request
        if verdict == CANCEL:
            # Never a bare `Task.cancel()`: the successor is already in the
            # slot, so the blind is guaranteed a complete sequence afterwards.
            running.cancelled.set()

    async def async_wait_idle(self) -> None:
        """Wait until no blind has a running or waiting sequence left."""
        while True:
            tasks = [
                queue.running.task
                for queue in self._queues.values()
                if queue.running is not None and queue.running.task is not None
            ]
            if not tasks:
                return
            await asyncio.wait(tasks)

    async def async_shutdown(self, *, grace: float | None = None) -> None:
        """Let in-flight sequences finish; name whatever did not go out.

        Deliberately not a cancellation. A sequence killed halfway can leave a
        blind down with its slats untouched, and at shutdown there is no
        successor to hand the slat command to -- so the only honest thing left
        is to make the loss **visible**, by name, at WARNING: entity, command,
        step `i/n`. Repairing it is a person's job (`CLAUDE.md`, 2026-08-28:
        "before restarting, look at what is sitting in a `wait`").

        Restart resilience for *deferred* waits is not here and must not be
        built here -- that belongs to `guard.recheck_every`.
        """
        self._closing = True
        sequences = [queue.running for queue in self._queues.values() if queue.running is not None]
        tasks = [seq.task for seq in sequences if seq.task is not None]
        if grace is None:
            grace = min(
                SHUTDOWN_GRACE_CAP,
                max((_grace_for(seq) for seq in sequences), default=0.0),
            )
        if tasks:
            await asyncio.wait(tasks, timeout=grace)

        for entity, queue in list(self._queues.items()):
            sequence = queue.running
            if sequence is not None and sequence.task is not None and not sequence.task.done():
                self._log_abandoned(sequence)
                sequence.task.cancel()
                await asyncio.gather(sequence.task, return_exceptions=True)
            if queue.pending is not None:
                _LOGGER.warning(
                    "cover_logic: %s had a queued %s request from %s that never started "
                    "(shutting down)",
                    entity,
                    queue.pending.priority.name,
                    queue.pending.source,
                )
        self._queues.clear()

    # -- Scheduling ------------------------------------------------------

    def _start(self, queue: _BlindQueue, request: _Request) -> None:
        """Plan-and-run `request` now, in its own task on this blind's queue."""
        sequence = _Sequence(request, uuid.uuid4().hex[:4], self._dry_run())
        queue.running = sequence
        task = asyncio.get_running_loop().create_task(self._run(sequence))
        sequence.task = task
        task.add_done_callback(lambda _task: self._finished(queue, sequence))

    def _finished(self, queue: _BlindQueue, sequence: _Sequence) -> None:
        """Retire `sequence` and start whatever was waiting behind it."""
        if queue.running is not sequence:
            return
        queue.running = None
        request, queue.pending = queue.pending, None

        if self._closing:
            return
        if request is None:
            self._queues.pop(sequence.request.blind.entity, None)
            return
        if sequence.cancelled.is_set():
            request.carried_tilt = sequence.unsent_tilt
        self._start(queue, request)

    def _dry_run(self) -> bool:
        """Read `entry.options["dry_run"]` live -- never cached at setup.

        Caching it would break the one path it exists for: "turn the hands off
        right now, something is wrong". Options give that for free, without a
        reload, which is why the switch lives there and not in `entry.data`.
        """
        return bool(self._entry.options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN))

    # -- Running one sequence --------------------------------------------

    async def _run(self, sequence: _Sequence) -> None:
        """Plan at the moment of starting, then issue the plan command by command."""
        request = sequence.request
        blind = request.blind
        position, tilt = self._read_axes(blind.entity)

        try:
            computed = plan(blind, position, tilt, request.action)
        except PlannerError:
            _LOGGER.exception("cover_logic: %s could not be planned", blind.entity)
            return
        computed = _carry_over_tilt(request.carried_tilt, computed, request.action)
        sequence.plan = computed

        self._log_clamps(blind.entity, computed.clamps)
        for suppressed in _suppressions(blind, request.action, computed, position, tilt):
            self._log_suppressed(sequence, suppressed)

        total = len(computed.commands)
        for step, command in enumerate(computed.commands, 1):
            if sequence.cancelled.is_set():
                return
            if not await self._execute(sequence, command, step, total):
                return
            sequence.index = step

    async def _execute(self, sequence: _Sequence, command: Command, step: int, total: int) -> bool:
        """Issue or wait out one command. `False` means stop this sequence here."""
        entity = sequence.request.blind.entity
        position, tilt = self._read_axes(entity)
        call = _call_for(command)
        self._log_command(sequence, command, step, total, call, position, tilt)

        if isinstance(command, Settle):
            return not await self._sleep_or_cancelled(sequence, command.seconds)
        if isinstance(command, WaitForPosition):
            return await self._wait_for_position(sequence, command)
        if call is None or sequence.dry_run:
            return True

        # Deferred import: this module must stay importable without Home
        # Assistant -- see the module docstring.
        from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

        service, data = call
        try:
            await self._call_cover(service, {"entity_id": entity, **data})
        except HomeAssistantError:
            # The house's own `continue_on_error: true`, one queue wide: this
            # blind's sequence stops, every other blind's keeps going. There is
            # no successor to hand the rest to, so what is left is named
            # instead. `index` is advanced past this command first: it *was*
            # issued, and it is already reported right here with its traceback
            # -- listing it again as "never issued" would send a reader looking
            # for a second, non-existent failure.
            _LOGGER.exception(
                "cover_logic: %s failed on %s (step %s/%s)",
                entity,
                _command_repr(command),
                step,
                total,
            )
            sequence.index = step
            self._log_abandoned(sequence)
            return False
        return True

    async def _sleep_or_cancelled(self, sequence: _Sequence, seconds: float) -> bool:
        """Sleep `seconds`; return `True` if the sequence was cancelled instead.

        Not skipped in dry run. A dry run that skips its waits compresses a
        90-second sequence into five milliseconds and never exercises the queue
        arbitration -- the one part of this module for which no other evidence
        exists.
        """
        if sequence.cancelled.is_set():
            return True
        try:
            await asyncio.wait_for(sequence.cancelled.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def _wait_for_position(self, sequence: _Sequence, command: WaitForPosition) -> bool:
        """Wait until the blind reports arrival, it is cancelled, or the timeout expires.

        On timeout this **continues** to the next command. Every arrival wait in
        the house it replaces is `continue_on_timeout: true`, with the reason
        written in the house's own words: "so a frozen cover cannot block the
        tilt forever". The one `false` in the house is a three-hour
        precondition gate, which is a different thing on a different time scale
        and already has an owner in `guard.on_timeout`. It is not silent,
        though: a frozen blind is exactly the symptom whose fix is to check the
        source node's `last_seen`, not to lengthen the timeout (2026-07-31: a
        timeout was added, the sensor stayed dead for another nine days).
        """
        entity = command.entity
        if sequence.cancelled.is_set():
            return False
        # Evaluated on entry as well as on every event: the blind may already
        # be there, and waiting for a state change that will never come is the
        # `wait_for_trigger` race this house has hit before. Semantically a
        # `wait_template`, not a `wait_for_trigger`.
        if _arrived(self.hass.states.get(entity), command.position, command.tolerance):
            return True

        # Deferred imports -- see the module docstring.
        from homeassistant.core import callback  # noqa: PLC0415
        from homeassistant.helpers.event import async_track_state_change_event  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        arrived: asyncio.Future[None] = loop.create_future()

        @callback
        def _changed(event: Any) -> None:
            if arrived.done():
                return
            if _arrived(event.data.get("new_state"), command.position, command.tolerance):
                arrived.set_result(None)

        unsub = async_track_state_change_event(self.hass, [entity], _changed)
        cancelled = loop.create_task(sequence.cancelled.wait())
        started = loop.time()
        try:
            done, _pending = await asyncio.wait(
                {arrived, cancelled},
                timeout=command.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            unsub()
            cancelled.cancel()
            arrived.cancel()

        if cancelled in done:
            return False
        if arrived in done:
            return True
        self._log_timeout(sequence, command, loop.time() - started)
        return True

    def _read_axes(self, entity: str) -> tuple[int | None, int | None]:
        """The blind's reported position and tilt -- the only two reads this module makes."""
        state = self.hass.states.get(entity)
        return (
            _reported(state, ATTR_CURRENT_POSITION),
            _reported(state, ATTR_CURRENT_TILT_POSITION),
        )

    # -- Logging ---------------------------------------------------------

    def _log_command(
        self,
        sequence: _Sequence,
        command: Command,
        step: int,
        total: int,
        call: tuple[str, dict[str, Any]] | None,
        position: int | None,
        tilt: int | None,
    ) -> None:
        """One line per command: what would be called (dry run) or was (live).

        `INFO` in a dry run, `DEBUG` live. A dry-run day's whole output is
        these lines, so it must be readable without turning debug logging on;
        in normal operation the same volume would be noise.
        """
        fields = _command_fields(
            sequence.id,
            sequence.request,
            command,
            step,
            total,
            call,
            position,
            tilt,
            sequence.dry_run,
        )
        kind = COMMAND_WOULD_CALL if sequence.dry_run else COMMAND_CALLED
        self._log_fields(sequence, kind, fields)

    def _log_suppressed(self, sequence: _Sequence, suppressed: _Suppressed) -> None:
        """The same line shape for a command that was asked for and not sent."""
        self._log_fields(
            sequence,
            COMMAND_SUPPRESSED,
            _suppressed_fields(sequence.id, sequence.request, suppressed),
        )

    def _log_fields(self, sequence: _Sequence, kind: str, fields: Mapping[str, object]) -> None:
        """Emit one already-built field mapping at this sequence's own level.

        The single funnel for every line this class writes, which is why the
        observer is notified here and nowhere else: a second call site is a
        second thing to forget. `kind` is passed down from the two callers
        rather than sniffed back out of `fields` -- each of them knows for
        certain which of the three states it is in, and re-deriving that from
        the presence of a key would be the same fact implemented twice.

        The observer is called after the log, and its failure is not allowed to
        become the sequence's failure: something that merely watches must never
        be able to stop a blind mid-movement.
        """
        level = logging.INFO if sequence.dry_run else logging.DEBUG
        _LOGGER.log(level, "%s", _format_line(sequence.dry_run, fields))
        if self._observer is None:
            return
        try:
            self._observer(kind, fields)
        except Exception:
            _LOGGER.exception("cover_logic: command observer raised, ignoring")

    def _log_timeout(self, sequence: _Sequence, command: WaitForPosition, elapsed: float) -> None:
        """A blind that never reported arrival: one line, one entity, one number to check."""
        if sequence.dry_run:
            # A dry run's waits always expire -- nothing is moving -- so at
            # WARNING they would drown out the real ones.
            _LOGGER.debug(
                "cover_logic[dry_run]: %s did not reach %s within %.0fs (expected: nothing moved)",
                command.entity,
                command.position,
                elapsed,
            )
            return
        _LOGGER.warning(
            "cover_logic: %s did not reach %s within %.0fs (last reported %s); "
            "continuing to the next command -- check that node's last_seen rather than "
            "lengthening the timeout",
            command.entity,
            command.position,
            elapsed,
            _axis_value(_reported(self.hass.states.get(command.entity), ATTR_CURRENT_POSITION)),
        )

    def _log_clamps(self, entity: str, clamps: tuple[Clamp, ...]) -> None:
        """Report each out-of-range value once per `(entity, axis, requested)`, not per recompute.

        A standing configuration mistake must be visible, but it must not
        stream into the log every ten minutes when the weather updates.
        """
        for clamp in clamps:
            key = (entity, clamp.axis, clamp.requested)
            if key in self._clamped:
                continue
            self._clamped.add(key)
            _LOGGER.warning(
                "cover_logic: %s %s value %s is outside 0..100, applying %s",
                entity,
                clamp.axis,
                clamp.requested,
                clamp.applied,
            )

    def _log_abandoned(self, sequence: _Sequence) -> None:
        """Name every command of `sequence` that will never be sent."""
        total = len(sequence.plan.commands)
        for offset, command in enumerate(sequence.unsent, sequence.index + 1):
            _LOGGER.warning(
                "cover_logic: %s never issued %s (step %s/%s, seq %s, src %s) -- "
                "this blind may need finishing by hand",
                sequence.request.blind.entity,
                _command_repr(command),
                offset,
                total,
                sequence.id,
                sequence.request.source or "-",
            )
