"""Waiting one out: the deadline `guards.py` deliberately cannot serve itself.

`guards.py` has no clock on purpose, so a `defer` comes out of it as a
`Deferral` -- which guard asked, how long the wait may last, what happens when
it runs out, how often it must be re-examined, and what "proceed" would do.
This module is the other half: given the current set of deferrals and a
timestamp, it says which blinds are still waiting, which have run out of time
and should now be acted on, and which have run out of time and should be
dropped.

**A deferral here is not a coroutine sitting in `await`.** That is the whole
design, and it is the direct answer to the failure this house has paid for more
often than any other: a `wait_for_trigger` (or a `wait_template`, or a `delay`)
is an *in-flight execution*, and a Home Assistant restart kills every one of
them silently -- the intent is simply gone, and the thing that should have
happened never does (`CLAUDE.md`, 2026-08-28, and memory
`feedback-restart-odzbroji-for-spustac`). Here, a deferral is a *derived fact*:
`guards.screen`/`guards.review` are pure functions of `(config, world)`, so
every evaluation re-derives the full set of deferrals from scratch. There is no
task to lose. A restart therefore cannot cancel a wait; the first evaluation
after startup re-creates it, because the guard that asked for it is still in
the configuration and its condition is still true.

`recheck_every` is what makes that guarantee not depend on luck. Re-derivation
happens on every evaluation, and an evaluation happens when a watched entity
changes -- but a deferral whose release depends on nothing watched (a timeout)
would otherwise wait for an unrelated event to happen along.
`next_recheck()` is this module's answer: the number of seconds after which the
caller must evaluate again even if the house has been perfectly still, which is
exactly the periodic watchdog `const.GUARD_DEFAULT_RECHECK` documents.

**What a restart does cost.** The elapsed time toward `max_wait` starts again,
because nothing here is persisted. That is deliberate and it is the safe
direction: every consequence of a reset clock is *later*, never *sooner* -- a
`proceed` fires further in the future, an `abandon` gives up further in the
future, and no blind ever moves earlier than its guard intended. Buying the
exact elapsed seconds back would mean a storage file whose own failure modes
(a stale record naming a guard that no longer exists, a write that did not land
before the restart) are worse than waiting longer.

Pure: no Home Assistant import, and no clock either -- `now` is an argument, so
a test states the time rather than racing it. In `tests/test_purity.py`'s
`PURE_MODULES`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from .const import GUARD_DEFAULT_RECHECK, GUARD_DEFER, GUARD_STAGE_OUTPUT, GUARD_TIMEOUT_PROCEED
from .engine import Decision
from .guards import Deferral, Guarded
from .model import Action

# The third verdict, alongside `GUARD_TIMEOUT_PROCEED`/`GUARD_TIMEOUT_ABANDON`:
# the deadline has not been reached, so keep waiting. Named here rather than in
# `const.py` because it is not part of the `guards:` vocabulary a config author
# writes -- no guard can be configured to "hold", it is what happens until one
# of the other two does.
HOLD = "hold"

# The smallest re-examination interval this module will ask for. A `max_wait`
# of a few seconds would otherwise produce a sub-second wake-up, and an
# evaluation is a full world snapshot plus the engine -- cheap, but not free,
# and never worth spinning on.
MIN_RECHECK = 1.0


def verdict(deferral: Deferral, waited: float) -> str:
    """`HOLD`, `proceed` or `abandon` for one deferral that has waited `waited` seconds.

    The two timeout answers are returned as the very strings
    `guard.on_timeout` is written in (`const.GUARD_TIMEOUTS`), not translated
    into a second vocabulary -- there is exactly one place the two spellings
    could drift, and this is it, so it does not exist.

    `max_wait is None` means "wait indefinitely" and is a *value*, not an
    omission (`model.UNSET` is the omission, and `guards._check_honourable`
    refuses a `defer` that omits it long before anything reaches here). So it
    holds forever, on purpose: the guard's author asked for a wait with no
    deadline, and inventing one here would be this module deciding a safety
    question that was already decided.
    """
    if deferral.max_wait is None:
        return HOLD
    if waited < deferral.max_wait:
        return HOLD
    return deferral.on_timeout


@dataclass(frozen=True, slots=True)
class Pending:
    """One blind's live wait: which guard, since when, and whether it is over.

    `since` is a plain epoch-seconds float supplied by the caller, never read
    from a clock here. `resolved` is `None` while the wait is running, and
    afterwards the timeout answer that ended it (`proceed`/`abandon`).

    A resolved record is **kept**, not dropped, and that is load-bearing. The
    guard that deferred this blind is usually still firing at the moment its
    `max_wait` runs out -- the sauna is still on, the door is still open -- so a
    dropped record would be re-created on the very next evaluation with a fresh
    clock, and the house would perform the same `proceed` movement again every
    `max_wait` for as long as the condition lasted. That is the "it moved and
    nobody could say why" incident with a timer attached. The record is
    released only when the guard stops deferring this blind, which is also
    exactly when a new wait would legitimately begin.
    """

    entity: str
    guard: int
    name: str
    stage: str
    since: float
    max_wait: int | None
    on_timeout: str
    recheck_every: int | None
    resolved: str | None = None

    @property
    def interval(self) -> int:
        """How often this wait must be re-examined, defaulted if the guard is silent.

        `config_schema` fills `recheck_every` in for every parsed `defer`, so
        `None` only reaches here from a `Guard` built in code. Defaulting is
        still the right answer: the alternative is a wait that is re-examined
        never, which is the exact thing this field exists to prevent.
        """
        return self.recheck_every if self.recheck_every is not None else GUARD_DEFAULT_RECHECK


@dataclass(frozen=True, slots=True)
class Elapsed:
    """What the clock did to the pending set during one `sync`.

    `proceed` maps a blind to the action its guard's timeout says to perform
    now; `abandoned` names the blinds whose guard gave up instead. Both are
    empty on the overwhelming majority of evaluations -- a timeout is an event,
    and `sync` runs on every recompute.
    """

    proceed: dict[str, Action]
    abandoned: tuple[str, ...]


class DeferralRegistry:
    """The live set of waits, reconciled against the guards on every evaluation.

    Holds no clock, no storage and no Home Assistant handle. The caller passes
    `now` in, acts on the `Elapsed` that comes back, and asks `next_recheck`
    when it must come back even if nothing else happens.
    """

    def __init__(self) -> None:
        """Start with nothing waiting."""
        self._pending: dict[str, Pending] = {}

    @property
    def pending(self) -> Mapping[str, Pending]:
        """Every blind currently held by a `defer`, resolved ones included."""
        return dict(self._pending)

    def sync(self, guarded: Guarded, decision: Decision, now: float) -> Elapsed:
        """Reconcile the live waits against `guarded`, and report what ran out.

        Called on every evaluation, with the freshly re-derived `Guarded`.
        Three things happen, in this order:

        - A blind that is no longer deferred is **released**: its record is
          dropped. No action is taken here for it -- if a guard released it,
          `Guarded.actions` already carries whatever the engine decided, and
          the caller dispatches that in the ordinary way. Dropping it is also
          what re-arms a future wait, since a later deferral of the same blind
          starts a fresh clock.
        - A blind newly deferred, or deferred by a *different* guard than
          before, starts its clock at `now`. "Different guard" matters: guards
          are first-match-wins, so a higher-priority one taking over is a new
          wait with its own deadline, not a continuation of someone else's.
        - A blind whose deadline has passed is resolved once. `proceed`
          produces an action, `abandon` produces a name; either way the record
          stays (see `Pending.resolved`) so the answer is not given twice.

        What `proceed` performs depends on the stage, and the difference is the
        one `guards.Deferral` documents: at the `output` stage the guard held a
        decided action back, so proceeding is performing `held`. At the `input`
        stage there is no `held` -- the blind was taken away before the engine
        was asked -- so proceeding means asking the engine *again*, with the
        world as it is now. That answer already exists: `engine.evaluate` is
        total and decided this blind regardless of screening (screening only
        decides whose answer is honoured), so `decision.targets[entity]` is
        precisely "what would this blind be told to do right now", freshly
        computed this very evaluation.
        """
        deferrals = guarded.deferrals
        proceed: dict[str, Action] = {}
        abandoned: list[str] = []
        live: dict[str, Pending] = {}

        for entity, deferral in deferrals.items():
            previous = self._pending.get(entity)
            if previous is not None and previous.guard == deferral.guard:
                record = replace(
                    previous,
                    name=deferral.name,
                    stage=deferral.stage,
                    max_wait=deferral.max_wait,
                    on_timeout=deferral.on_timeout,
                    recheck_every=deferral.recheck_every,
                )
            else:
                record = _fresh(entity, deferral, now)

            if record.resolved is None:
                answer = verdict(deferral, now - record.since)
                if answer != HOLD:
                    record = replace(record, resolved=answer)
                    if answer == GUARD_TIMEOUT_PROCEED:
                        action = _action_for(deferral, decision, entity)
                        if action is None:
                            abandoned.append(entity)
                        else:
                            proceed[entity] = action
                    else:
                        abandoned.append(entity)

            live[entity] = record

        self._pending = live
        return Elapsed(proceed=proceed, abandoned=tuple(abandoned))

    def next_recheck(self, now: float) -> float | None:
        """Seconds until this registry must be asked again, or `None` if never.

        `None` means there is nothing waiting: no timer is needed and the
        caller should cancel any it has. Otherwise it is the soonest of every
        unresolved wait's own `recheck_every` and its remaining time to
        `max_wait` -- the deadline is included so a guard with a two-minute
        `max_wait` and the default fifteen-minute recheck times out on time
        rather than thirteen minutes late.

        A resolved wait asks for nothing. It has already given its answer and
        is only being remembered so it does not give it twice; the event that
        clears it is its guard releasing the blind, which is a state change and
        arrives on its own.
        """
        soonest: float | None = None
        for record in self._pending.values():
            if record.resolved is not None:
                continue
            candidate = float(record.interval)
            if record.max_wait is not None:
                candidate = min(candidate, record.since + record.max_wait - now)
            candidate = max(MIN_RECHECK, candidate)
            soonest = candidate if soonest is None else min(soonest, candidate)
        return soonest

    def as_attributes(self, now: float) -> dict[str, dict[str, object]]:
        """The pending set as plain values, for the diagnostic sensor.

        Durations rather than timestamps: `waited` and `max_wait` in seconds
        answer "how much longer" without the reader converting anything, and
        without this pure module having to decide between UTC and local time
        for a field nobody will diff against a log.
        """
        return {
            entity: {
                "guard": record.guard,
                "name": record.name,
                "policy": GUARD_DEFER,
                "stage": record.stage,
                "state": record.resolved if record.resolved is not None else "waiting",
                "waited": int(max(0.0, now - record.since)),
                "max_wait": record.max_wait,
                "on_timeout": record.on_timeout,
                "recheck_every": record.interval,
            }
            for entity, record in sorted(self._pending.items())
        }


def _fresh(entity: str, deferral: Deferral, now: float) -> Pending:
    """A brand-new wait, its clock starting at `now`."""
    return Pending(
        entity=entity,
        guard=deferral.guard,
        name=deferral.name,
        stage=deferral.stage,
        since=now,
        max_wait=deferral.max_wait,
        on_timeout=deferral.on_timeout,
        recheck_every=deferral.recheck_every,
    )


def _action_for(deferral: Deferral, decision: Decision, entity: str) -> Action | None:
    """What `proceed` performs for one deferral -- see `DeferralRegistry.sync`.

    `None` means there is nothing to perform: an `input`-stage deferral on a
    blind the engine has no answer for at all. That cannot happen for a
    configured blind (`evaluate` is total), so it is treated as an abandon
    rather than as an error -- the wait is over either way, and inventing a
    movement for a blind nobody decided anything about is the one outcome that
    would be worse than doing nothing.
    """
    if deferral.stage == GUARD_STAGE_OUTPUT:
        return deferral.held
    return decision.targets.get(entity)
