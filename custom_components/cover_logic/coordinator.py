"""Watch state, run the whole decision path, and hand the result to the executor.

The seam between "the house changed" and "the house was told what to do".
Subscribes to exactly the entities the configuration reads, waits for the house
to stop writing (the settle window -- see `_schedule_settle`, and
`const.EVAL_SETTLE_SECONDS` for the morning that made it necessary), and then
runs the full path in the one order that is allowed to exist:

    screen()  ->  evaluate()  ->  review()  ->  CoverRunner.async_apply()

`guards.screen` takes no `Decision` and `guards.review` refuses to run without
the `Screening` that `screen` produced, so that order is a property of the
signatures rather than of this module's care -- see `guards.py`'s docstring.
The last-known-good `Decision` stays on the shelf through a failing evaluation
so the diagnostic sensor always has something to show; a failing evaluation
also dispatches nothing at all, because a `Guarded` that could not be computed
is not a `Guarded` whose guards can be trusted.

`readiness.assess` runs on the same snapshot and gates dispatch alone: a blind
whose own inputs are missing, `unknown` or `unavailable` is still decided,
still published and still on the sensor, but nothing is issued for it. See
`readiness.py` for the measured minute that bought this. Startup is no longer
exempt from the settle window either -- `async_setup` arms it like any state
change does, and its own docstring states what that costs.

**This is the module that gives the executor its hands.** Since phase 3 task 5
`_build_runner` binds the runner's service caller to `hass.services.async_call`
-- the only such call in this package -- so what stands between a decision and
a motor is the `dry_run` option alone. It defaults to `True`, which is why a
fresh install still moves nothing until someone turns it off.

This module imports `homeassistant` unconditionally at module level, the same
choice `ha_world.py` makes and for the same reason (see that module's own
docstring and `tests/test_purity.py`'s `PURE_MODULES` list, which this file is
deliberately absent from): it is never imported at `cover_logic` package
import time. `__init__.py` only reaches this module through a local import
inside `async_setup_entry`, so importing `cover_logic` itself -- which is
what happens merely by importing any *pure* submodule, including from the
system-Python 3.12 test run that has no `homeassistant` installed at all --
never touches this file.
"""

from collections.abc import Callable, Mapping
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
import homeassistant.util.dt as dt_util

from .command_log import CommandLog
from .config_schema import referenced_entities
from .const import (
    DEFAULT_DRY_RUN,
    EVAL_SETTLE_MAX_SECONDS,
    EVAL_SETTLE_SECONDS,
    GUARD_ANY,
    OPT_DRY_RUN,
)
from .deferrals import DeferralRegistry, Elapsed
from .engine import Decision, evaluate
from .guards import Guarded, guard_blinds, review, screen
from .ha_world import build_world
from .model import KEEP, Action, Config
from .readiness import Readiness, assess
from .runner import COVER_DOMAIN, CoverRunner, Priority, current_position

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# `runner.async_apply`'s `source` field for the two ways this module asks for a
# movement. They are the field that answers "why did it move" -- the question
# `last_triggered` provably cannot answer once a blind has moved twice inside
# the window being investigated.
SOURCE_RECOMPUTE = "coordinator"
SOURCE_GUARD_TIMEOUT = "guard-timeout"

# The settle window and its cap live in `const.py`, with the measured timeline
# that fixes their values -- they are read here as module globals rather than
# copied into an instance attribute so a test can shrink them (the *shape* of
# the timer is what most of `tests/ha/test_settle.py` is about, not its
# length).


class CoverLogicCoordinator:
    """Holds the current `Decision` for one config entry's `Config`.

    `decision` is the last successfully computed `Decision`, or `None` before
    the first evaluation has ever completed. A failing evaluation never blanks
    it -- see `_async_evaluate` -- it only ever updates `last_error`, so a
    diagnostic entity reading `decision` mid-outage shows the last good answer
    instead of nothing.

    `add_listener` is how `sensor.py`'s entity learns a new evaluation has
    completed -- this class has no `hass.bus` event of its own, so without it
    the entity would have no way to know when to call `async_write_ha_state`.
    """

    def __init__(self, hass: HomeAssistant, config: Config, entry: "ConfigEntry") -> None:
        """Store `hass`/`config`/`entry` and build the executor; subscribe nothing yet.

        `entry` is required, not optional, and the runner is built here rather
        than being injectable. A coordinator that could be constructed without
        an executor would have two code paths -- one that dispatches and one
        that only computes -- and the one running in the house would be
        whichever `__init__.py` happened to pick. There is one path.
        """
        self.hass = hass
        self.config = config
        self.entry = entry
        self.decision: Decision | None = None
        self.guarded: Guarded | None = None
        # The last computed verdict on whether the inputs were readable at all.
        # `None` only before the first evaluation; a `Readiness` with an empty
        # `blocked` is "everything readable", which is not the same thing.
        self.readiness: Readiness | None = None
        self.last_error: str | None = None
        self.last_success: dt.datetime | None = None
        self.commands = CommandLog()
        self.deferrals = DeferralRegistry()
        self.runner = self._build_runner()
        self._unsub_state_change: Callable[[], None] | None = None
        self._unsub_recheck: Callable[[], None] | None = None
        self._unsub_settle: Callable[[], None] | None = None
        # When the burst currently being settled must be evaluated regardless
        # of further changes; `None` when no burst is in flight. See
        # `_schedule_settle`.
        self._settle_cap: dt.datetime | None = None
        # Whether this coordinator is between `async_setup` and `async_unload`.
        # A timer that fires after unload, or a state change arriving before
        # setup, must not evaluate against a config entry that is on its way
        # out.
        self._active = False
        self._listeners: list[Callable[[], None]] = []
        # The last reason each blind's action was withheld by a guard, so a
        # standing suppression is recorded once instead of on every recompute.
        # Without this, a single open sauna door writes a `withheld` entry
        # every time the weather updates and `last_command` degenerates into a
        # clock.
        self._withheld: dict[str, str] = {}
        # The same once-per-change-of-reason bookkeeping for readiness
        # withholdings. A separate map from `_withheld` on purpose: the two
        # answer different questions ("a guard said no" vs. "the inputs could
        # not be read") and a blind can legitimately be in both, so one shared
        # slot would let each overwrite the other's dedupe key and re-record
        # every recompute.
        self._unready: dict[str, str] = {}

    def _build_runner(self) -> CoverRunner:
        """The executor, with its service caller bound to real `cover.*` services.

        This is the wire phase 3 task 5 connects: the runner's `CoverCall` is
        `hass.services.async_call`, one entity per call. `dry_run` (default
        `True`) is what still stops a command short of it -- the runner returns
        before reaching this function -- so a fresh install moves nothing until
        someone turns that option off.

        `CommandLog` stays as the runner's `observer` and as the recorder of
        what actually went out; it is no longer the caller.
        """

        async def _call_cover(service: str, data: dict[str, Any]) -> None:
            """Issue one `cover.*` call and record it once Home Assistant accepts it."""
            # `blocking=True` -- see `docs/rationale.md`, "Why the service call blocks".
            await self.hass.services.async_call(COVER_DOMAIN, service, dict(data), blocking=True)
            self.commands.dispatched(service, data)

        return CoverRunner(self.hass, self.entry, _call_cover, observer=self.commands.observe)

    @property
    def dry_run(self) -> bool:
        """Whether the executor is currently only describing what it would do.

        Read live off `entry.options`, never cached -- the same reason
        `runner._dry_run` reads it live: the one path this switch exists for is
        "turn the hands off right now", and a cached copy would need a reload
        to notice.
        """
        return bool(self.entry.options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN))

    @property
    def last_command(self) -> dict[str, Any] | None:
        """The newest thing the executor did, or deliberately did not do."""
        return self.commands.last

    @property
    def pending(self) -> dict[str, dict[str, Any]]:
        """What is waiting: deferred by a guard, or queued behind a movement.

        Two sub-mappings rather than one merged per-blind view, because the two
        are genuinely independent -- a blind can be deferred *now* while a
        sequence started before the guard fired is still finishing -- and a
        merged shape would have to decide which of those a reader meant.
        """
        now = dt_util.utcnow().timestamp()
        return {
            "deferred": self.deferrals.as_attributes(now),
            "queued": self.runner.in_flight,
        }

    async def async_setup(self) -> None:
        """Subscribe to the config's referenced entities and arm the first evaluation.

        Subscribing before arming the evaluation (rather than after) would
        leave a window where a real state change is missed because nothing is
        listening yet.

        **Startup goes through the settle window like every other evaluation.**
        It used to be the one exemption -- "there is no transition at startup,
        only a world that already is what it is" -- and that sentence was
        exactly wrong: startup is the largest burst of state writes the house
        ever has, and the world half a second into it is one that never
        existed. On 2026-08-31 at 11:45:35, 0.5 s after setup, every input this
        config reads was still missing and the engine decided "open" for all
        ten blinds (`readiness.py`'s docstring has the log). The window is not
        the whole fix -- readiness is, and it is what makes the decision
        unactionable rather than merely late -- but an exemption whose stated
        reason was false does not get to stay.

        **What that costs, plainly:** the first evaluation now lands one settle
        window later, so for `EVAL_SETTLE_SECONDS` after every reload the
        diagnostic sensor is unavailable and a pending deferral's recheck timer
        is unarmed. One window of a blank diagnostic against a house-wide
        movement on a world nobody saw is not a close trade, and the deferral
        half costs nothing real: every consequence of a deferral being
        re-derived one window late is *later*, never sooner, which is the same
        argument `deferrals.py` already makes about a restart resetting its
        elapsed seconds.
        """
        self._active = True

        entity_ids = sorted(_entity_ids(self.config))
        if entity_ids:
            self._unsub_state_change = async_track_state_change_event(
                self.hass, entity_ids, self._handle_state_change
            )

        self._schedule_settle()

    async def async_unload(self) -> None:
        """Remove the state-change subscription and cancel any pending settle.

        A leaked subscription or timer outlives the config entry that owns
        it -- concretely, a reload while editing the config would otherwise
        leave the old subscription (and a possibly-pending settle timer)
        alive alongside the new one, doubling up on evaluations against
        whichever `Config` object it captured.

        The executor is shut down last and is *not* cancelled: `async_shutdown`
        lets a sequence already in flight finish and names whatever it could
        not send, because a sequence killed halfway can leave a blind down with
        its slats untouched and there is no successor left to hand them to.
        Subscriptions come first so nothing new arrives while it drains.
        """
        self._active = False
        if self._unsub_state_change is not None:
            self._unsub_state_change()
            self._unsub_state_change = None
        if self._unsub_recheck is not None:
            self._unsub_recheck()
            self._unsub_recheck = None
        if self._unsub_settle is not None:
            self._unsub_settle()
            self._unsub_settle = None
        self._settle_cap = None
        await self.runner.async_shutdown()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register `listener` to be called after every completed evaluation.

        Called once per `_async_evaluate` run, success or failure alike --
        the diagnostic sensor needs to refresh on a recorded error just as
        much as on a new `decision`, since `last_error` is part of what it
        shows. The listener always sees `decision`/`last_error` already
        updated: `_async_evaluate` notifies only after writing them, never
        before. Returns an unsubscribe callable; safe to call more than once.
        """
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Start (or restart) the settle window; never evaluate inline here.

        `event` itself is not read -- `_async_evaluate` always takes a fresh
        `World` snapshot of every referenced entity, not just the one that
        changed, so there is nothing this handler needs from it beyond "some
        watched entity changed, evaluate once the house has stopped writing".
        """
        self._schedule_settle()

    @callback
    def _schedule_settle(self) -> None:
        """Arm the settle window, moving its deadline if one is already pending.

        **Restart-on-change, not a fixed window from the first change**, and
        that distinction is the whole fix. `svitanie` writes several entities
        in sequence about one transition -- measured 1-2 s apart, see
        `const.EVAL_SETTLE_SECONDS` -- and the world in between those writes is
        one that never really existed. A fixed window opened by the first write
        evaluates inside the sequence and then again after it; a window that
        moves with every new change evaluates once, after the *last* write.

        Bounded, because restart-on-change is starvable: `_settle_cap` is set
        from the *first* change of a burst and never moved, so an entity that
        changes faster than the window can delay the evaluation by at most
        `EVAL_SETTLE_MAX_SECONDS` rather than for as long as it keeps
        flapping. Clamping the new deadline to the cap (rather than firing
        early, or refusing to re-arm) keeps one code path: at the cap the
        deadline simply stops moving.

        A point in time rather than the shorter delay-taking helper: the two are
        equivalent here, and this shape was chosen when the phase-2 no-movement
        guard (since deleted) refused that helper's *name*. `_reschedule` uses
        the same call for the same reason.
        """
        if not self._active:
            return
        now = dt_util.utcnow()
        if self._unsub_settle is None:
            self._settle_cap = now + dt.timedelta(seconds=EVAL_SETTLE_MAX_SECONDS)
        else:
            self._unsub_settle()
            self._unsub_settle = None
        when = now + dt.timedelta(seconds=EVAL_SETTLE_SECONDS)
        if self._settle_cap is not None and when > self._settle_cap:
            when = self._settle_cap
        self._unsub_settle = async_track_point_in_utc_time(self.hass, self._handle_settled, when)

    async def _handle_settled(self, _now: dt.datetime) -> None:
        """The house stopped writing (or hit the cap): evaluate now.

        A coroutine rather than a `@callback`, which
        `async_track_point_in_utc_time` handles by running it as a task -- the
        alternative is a synchronous callback that creates that task itself,
        which is the same thing with one more place to forget the `_active`
        check.
        """
        self._unsub_settle = None
        self._settle_cap = None
        if not self._active:
            return
        await self._async_evaluate()

    async def _async_evaluate(self) -> None:
        """Snapshot the world once, run the whole path, and execute the result.

        Any evaluation failure is caught, logged with its traceback, and
        recorded in `last_error` rather than propagated: a transient bad state
        (e.g. mid-reload, a helper briefly missing) must not blank a diagnostic
        sensor that was showing a perfectly good answer a moment ago. The
        failure stays visible -- in the log, and in `last_error` for a sensor to
        surface -- and is never swallowed silently.

        The catch is deliberately broad, not limited to `EngineError`. The
        failures this will actually meet in a house are not configuration
        invariants: a typo'd condition type raises `ValueError`, and a broken
        user template raises out of Jinja by design, because evaluating it as
        False could mean leaving the house open during a heatwave. Catching only
        `EngineError` would let those escape into the settle timer's own
        callback, where `last_error` is never set and the sensor keeps showing a stale
        answer with no sign that anything is wrong.

        The guards are inside the same `try` as the engine, and deliberately so:
        `guards.GuardError` means an interlock cannot be honoured as written,
        and the answer to "a safety rule is unusable" is to move nothing and say
        so -- never to fall back to the unguarded decision, which is exactly the
        movement the guard existed to stop.
        """
        world = build_world(self.hass, self.config)
        try:
            # From this same snapshot, never a second read of `hass.states`:
            # otherwise "was the world readable" and "what did the world say"
            # are answers about two different instants. Inside the `try` for
            # the same reason the guards are -- a readiness verdict that could
            # not be computed must dispatch nothing, not dispatch everything.
            readiness = assess(self.config, world)
            screening = screen(self.config, world)
            decision = evaluate(self.config, world)
            guarded = review(self.config, world, decision, self._positions(), screening)
        except Exception as err:
            _LOGGER.exception("cover_logic: evaluation failed, keeping previous decision")
            self.last_error = f"{type(err).__name__}: {err}"
            self._notify_listeners()
            return

        self.decision = decision
        self.guarded = guarded
        self.readiness = readiness
        self.last_error = None
        self.last_success = dt_util.utcnow()

        now = self.last_success.timestamp()
        elapsed = self.deferrals.sync(guarded, decision, now)
        self._reschedule(self.deferrals.next_recheck(now))
        await self._execute(guarded, elapsed, decision.mode, readiness)

        # Last, not first: the sensor reads `last_command` and `pending`, and a
        # listener fired before the executor ran would show the previous
        # recompute's answer next to this one's mode.
        self._notify_listeners()

    def _positions(self) -> dict[str, int | None]:
        """Every configured blind's reported position, for `guards.review`.

        Read fresh on every evaluation and read through `runner.current_position`,
        not through a second copy of "what does this cover report" -- a
        directional guard has to be judged against the same number the runner
        would act on, or the two layers can disagree about which way a blind is
        about to move.

        A blind with no state, or an unreadable one, is `None`, which
        `guards.review` treats as "the position could not be read" and which
        makes a directional guard fire rather than silently stand down.
        """
        return {
            entity: current_position(self.hass.states.get(entity)) for entity in self.config.blinds
        }

    async def _execute(
        self, guarded: Guarded, elapsed: Elapsed, mode: str, readiness: Readiness
    ) -> None:
        """Hand this evaluation's answers to the runner, in priority order.

        `readiness` is carried down to `_apply` rather than consulted here, so
        the gate sits on the one funnel both dispatch groups go through. A
        second check next to a second call site is a second thing to forget,
        and the thing forgotten would be the one that moves a house.

        Three groups, in this order and never merged:

        1. **Withheld.** Every blind a guard suppressed outright is recorded --
           once per change of reason, not once per recompute -- so that "the
           new system did nothing here" is distinguishable from "the new system
           was never asked", which is the single most valuable thing the dry-run
           day produces. Nothing is dispatched for these.
        2. **Timed-out deferrals**, at `Priority.GUARD`. A guard's own deadline
           expiring outranks a routine recompute, and it outranks a person, for
           the reason `runner.Priority` states: an interlock that a later
           recompute could quietly overtake is a suggestion.
        3. **The ordinary decision**, at `Priority.SCHEDULED`.

        Groups 2 and 3 cannot collide: a blind whose deferral just resolved is
        still in `Guarded.deferrals` for this evaluation, so its `Outcome.action`
        is `None` and it is absent from `Guarded.actions`.
        """
        for entity, outcome in sorted(guarded.outcomes.items()):
            if outcome.action is not None:
                self._withheld.pop(entity, None)
                continue
            if self._withheld.get(entity) == outcome.reason:
                continue
            self._withheld[entity] = outcome.reason
            self.commands.withheld(
                entity, outcome.reason, policy=outcome.policy, guard=outcome.guard
            )

        for entity in elapsed.abandoned:
            _LOGGER.warning(
                "cover_logic: %s waited out its guard and gave up (on_timeout: abandon); "
                "nothing was issued for it",
                entity,
            )

        # Drop the dedupe slots of blinds that are readable again, so the next
        # outage is recorded instead of being mistaken for the same one.
        self._unready = {
            entity: reason
            for entity, reason in self._unready.items()
            if readiness.blocked_by(entity)
        }

        await self._apply(elapsed.proceed, Priority.GUARD, SOURCE_GUARD_TIMEOUT, mode, readiness)
        await self._apply(guarded.actions, Priority.SCHEDULED, SOURCE_RECOMPUTE, mode, readiness)

    async def _apply(
        self,
        actions: Mapping[str, Action],
        priority: Priority,
        source: str,
        mode: str,
        readiness: Readiness,
    ) -> None:
        """Ask the runner for each of `actions`, skipping the ones that ask for nothing.

        **The readiness gate is here, and it is here rather than earlier for one
        reason: this is the only place a command can reach the runner.** A blind
        whose own inputs could not be read is recorded and dropped -- including
        a `Priority.GUARD` deferral whose deadline just expired, because "the
        interlock ran out of patience" is not a reason to trust the decision it
        was holding back. The decision itself is untouched and still published;
        see `readiness.py` for why this is a veto rather than a wait, and why
        the veto is per blind.

        An `Action(KEEP, KEEP)` is not dispatched. It is a complete, meaningful
        decision -- "leave both axes alone", which is how the engine spells both
        "a rule said keep" and "no rule matched" -- but there is nothing in it
        to execute, and handing it to a blind's queue is not free: the waiting
        slot holds exactly one request, so a no-op arriving mid-movement would
        evict a real request that was queued behind it.

        Re-sending an action the blind is already at is *not* filtered here, on
        purpose. That is the planner's dead band, which compares against the
        position the blind reports at the moment the sequence starts; filtering
        on a stale idea of "we already sent this" is how a blind ends up never
        being corrected after somebody moved it by hand.
        """
        for entity, action in sorted(actions.items()):
            blind = self.config.blinds.get(entity)
            if blind is None:
                # `Decision.targets` is built from `config.blinds`, so this is
                # unreachable from the engine; a guard's `force` could name
                # anything, though, and a missing blind has no `travel_time` to
                # plan against.
                _LOGGER.warning("cover_logic: %s is not a configured blind, not applying", entity)
                continue
            if action.position is KEEP and action.tilt is KEEP:
                continue
            # After the no-op check on purpose: a blind that was going to be
            # asked for nothing was not withheld from anything.
            if readiness.blocked_by(entity):
                self._record_unready(entity, readiness)
                continue
            try:
                await self.runner.async_apply(
                    blind, action, priority=priority, source=source, mode=mode
                )
            except Exception:
                _LOGGER.exception("cover_logic: could not queue %s for %s", entity, source)

    def _record_unready(self, entity: str, readiness: Readiness) -> None:
        """Say once, loudly, that `entity` was not commanded because its inputs were not readable.

        `WARNING`, not `DEBUG`: the four silent minutes of 2026-08-06 are the
        thing this whole gate exists not to repeat. Deduped per change of
        reason, the same way a standing guard suppression is -- an outage that
        lasts an hour must not turn `last_command` into a clock.
        """
        reason = readiness.reason(entity)
        if self._unready.get(entity) == reason:
            return
        self._unready[entity] = reason
        _LOGGER.warning("cover_logic: %s not commanded -- %s", entity, reason)
        self.commands.withheld(entity, reason)

    def _reschedule(self, seconds: float | None) -> None:
        """Arm (or disarm) the periodic re-examination a pending `defer` needs.

        `None` means nothing is waiting: the timer is cancelled outright rather
        than left ticking, so a still house costs nothing.

        This is the restart-resilient half of a deferral, and it is worth being
        explicit about why it is enough. A deferral is never an in-flight
        `await` here -- `guards.screen`/`guards.review` re-derive the whole
        pending set from `(config, world)` on every evaluation, so a restart
        has no task to kill and the first evaluation after startup re-creates
        every wait that is still warranted. What a restart *could* still break
        is a wait whose release depends on nothing anyone is watching, i.e. a
        timeout: that is what this timer is, and `async_setup` runs an
        evaluation itself, so it is armed again before Home Assistant has
        finished starting. See `deferrals.py`'s module docstring.

        A one-shot point in time rather than a repeating interval, because the
        interval this needs is not constant: `next_recheck` shortens itself to
        whatever is closest -- a guard's `recheck_every` or the deadline it is
        counting down to -- and that answer changes on every evaluation.

        (`async_track_point_in_utc_time` rather than the shorter delay-taking
        helper, which is the equivalent call: see `_schedule_settle` for why
        this package settled on this one.)
        """
        if self._unsub_recheck is not None:
            self._unsub_recheck()
            self._unsub_recheck = None
        if seconds is None:
            return
        when = dt_util.utcnow() + dt.timedelta(seconds=seconds)
        self._unsub_recheck = async_track_point_in_utc_time(self.hass, self._handle_recheck, when)

    async def _handle_recheck(self, _now: dt.datetime) -> None:
        """The recheck timer fired: evaluate now, not one settle window later.

        The settle window is about *state-change-driven* evaluation. This timer
        is the opposite case: it fires precisely because nothing is changing,
        and it is counting down a guard's own deadline. Adding the window to it
        would add that window to every deadline in the house, silently and
        always in the direction of "the interlock released later than it said".

        The one exception is a burst already in flight: then this returns and
        lets the pending settle do the evaluation, because a `defer` released
        against a half-applied world is the very defect the window exists for
        -- and the wait is bounded, since that settle fires within
        `EVAL_SETTLE_SECONDS` (`EVAL_SETTLE_MAX_SECONDS` at the outside) and
        `_async_evaluate` re-arms this timer from whatever it finds. No wakeup
        is lost by returning here.
        """
        self._unsub_recheck = None
        if not self._active or self._unsub_settle is not None:
            return
        await self._async_evaluate()


def _entity_ids(config: Config) -> set[str]:
    """Plain entity ids to subscribe to: what the config reads, plus directional guards' blinds.

    An attribute-read entry from `referenced_entities` is `(entity_id,
    attribute)` -- there is no per-attribute subscription in Home Assistant's
    state-change event, so its `entity_id` is what gets watched; a change to
    any attribute (or the state itself) of that entity is what triggers
    re-evaluation.

    `referenced_entities` alone is not the whole answer any more, and this is
    the gap `_directional_guard_blinds` closes -- see its own docstring.
    """
    watched = {
        entry[0] if isinstance(entry, tuple) else entry for entry in referenced_entities(config)
    }
    return watched | _directional_guard_blinds(config)


def _directional_guard_blinds(config: Config) -> set[str]:
    """The blinds whose *own* position can change a guard's verdict.

    A guard with `applies_to: closing`/`opening` is judged against where the
    blind is right now (`guards._direction_matches`), so its answer can change
    with nothing but the blind itself moving -- somebody pulling it up by hand,
    or another system putting it down. Nothing in `referenced_entities` sees
    that: it enumerates what *conditions* read, and a directional guard reads a
    position no condition mentions. On this house's own configuration
    `referenced_entities(fixtures/dom_peter.yaml)` contains no `cover.*` entry
    at all, so without this the verdict would go stale silently and only be
    noticed on the day the interlock was needed.

    Narrow on purpose. Subscribing the coordinator to *every* cover would make
    the layer that decides listen to the layer that moves: the runner's own
    movement would feed back as a recompute several times a second during a
    55-second travel. Only blinds a directional guard could actually judge are
    watched, which is none at all on a configuration with no directional
    guards, and the settle window plus the planner's dead band bound what is
    left.

    An `input`-stage guard can never name a direction (`validation`'s
    `guard_input_direction`), so this only ever collects `output`-stage guards'
    targets without having to filter by stage.
    """
    watched: set[str] = set()
    for guard in config.guards:
        if guard.applies_to != GUARD_ANY:
            watched |= guard_blinds(config, guard)
    return watched
