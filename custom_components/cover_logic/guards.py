"""Safety interlocks: what may suppress, replace or postpone a decision.

Pure and total, like `engine.py` and `planner.py`: no Home Assistant imports,
no service calls, and -- the constraint that shapes this whole module -- **no
clock**. A `defer` is not a decision, it is a decision *plus a deadline*, and a
module that cannot wait must hand the deadline to something that can. That is
what `Deferral` is: which guard asked for the wait, how long it may last, what
happens when it runs out, how often it must be re-examined so a restart cannot
silently cancel it, and what "proceed" would actually do. `runner.py` does the
waiting and never has to re-derive any of that.

Two entry points, one per stage, and **the stage is never a parameter**:

- `screen(config, world)` runs the `stage: input` guards. It takes no
  `Decision` and no positions because at that moment neither exists -- the
  engine has not been asked yet. That missing argument is the enforcement: a
  caller cannot run the input guards at the output moment by passing the wrong
  flag, because there is no flag.
- `review(config, world, decision, positions, screening)` runs the
  `stage: output` guards. It cannot be called without a `Decision`, and it
  cannot be called without the `Screening` that came out of `screen()`, so the
  two stages can only run in the one order that makes sense and a blind an
  input guard already claimed can never be judged twice.

Both filter `config.guards` by stage themselves. `Guard.stage` is read here and
nowhere else.

See docs/rationale.md -- "`guards.py`" for the four decisions this module
makes that a reader would otherwise have to reverse-engineer: why `closing` is
the position axis alone, what an unreadable position means, why a guard that
fires governs the whole action rather than half of it, and why an
`input`-stage guard may not name a direction.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .conditions import evaluate_condition
from .const import (
    GUARD_ANY,
    GUARD_CLOSING,
    GUARD_DEFER,
    GUARD_DIRECTIONS,
    GUARD_FORCE,
    GUARD_POLICIES,
    GUARD_SKIP,
    GUARD_STAGE_INPUT,
    GUARD_STAGE_OUTPUT,
    GUARD_STAGES,
    GUARD_TIMEOUTS,
)
from .engine import Decision, resolve_action, resolve_ownership
from .model import KEEP, UNSET, Action, Config, Guard, Ref
from .world import Target, World

# The trace label for a blind no guard claimed. Deliberately the same `#none`
# shape `engine.py` uses for "no rule matched", and deliberately ambiguous in
# the same way: "there are no guards at all" and "every guard was asked and
# none matched" end identically, and debugging either means reading the guard
# list either way.
NO_GUARD = "guards#none"


class GuardError(Exception):
    """Raised when a guard cannot be honoured as written.

    Never "ignore the guard and carry on". An interlock that quietly does
    nothing is the failure mode this whole schema exists to end -- the house
    it is derived from already has one (`continue_on_timeout: true` with no
    `timeout` key, a line that looks like it bounds a wait and does not). Every
    condition that raises here is also an `ERROR`-severity `validation.Problem`,
    so a configuration reaching this point is one that was never validated;
    the same contract `planner.plan` has with an unresolved `Ref`.
    """


@dataclass(frozen=True, slots=True)
class Deferral:
    """Everything `runner.py` needs to do the waiting `guards.py` cannot.

    Fields:

    - `guard`/`name` -- which guard asked, by index in `Config.guards` and by
      its own label. The index is the identity; the label is for humans.
    - `stage` -- `input` or `output`. It decides what `held` means, below.
    - `max_wait` -- seconds, or `None` for "wait indefinitely". `None` is a
      value, not an omission (`model.UNSET` is the omission, and a `defer`
      that omits it raises rather than reaching here).
    - `on_timeout` -- `proceed` or `abandon`, always set. There is no default
      because the two are opposites and both are in real use.
    - `recheck_every` -- seconds between re-examinations. Present on every
      `defer` (the parser fills it in), because a wait nothing re-examines
      dies at the next restart and looks alive until the day it was needed.
    - `held` -- what `proceed` means. At the `output` stage it is the action
      the engine decided and this guard held back, so proceeding is
      performing it. At the `input` stage it is `None`: no decision was ever
      made for this blind, so proceeding means asking the engine again, with
      whatever the world looks like then -- which is the right answer that
      much later anyway.
    """

    guard: int
    name: str
    stage: str
    max_wait: int | None
    on_timeout: str
    recheck_every: int | None
    held: Action | None


@dataclass(frozen=True, slots=True)
class Outcome:
    """One blind's final answer, and why -- never one without the other.

    `action is None` means "do nothing to this blind now". That is not the
    same as `Action()` (keep, keep), which is a decision to leave both axes
    alone: `None` says no decision of this blind's own is in force at all,
    because a guard suppressed it or is holding it. A runner treats both as
    "issue nothing", but only one of them has a deadline attached.

    `guard`/`policy`/`stage` are `None` exactly when no guard fired, in which
    case `action` is whatever the engine decided and `reason` is `NO_GUARD`.
    """

    entity: str
    action: Action | None
    reason: str
    guard: int | None = None
    policy: str | None = None
    stage: str | None = None
    deferral: "Deferral | None" = None


@dataclass(frozen=True)
class Screening:
    """What the `input` guards did, and which blinds are still the engine's business."""

    outcomes: dict[str, Outcome]
    remaining: frozenset[str]


@dataclass(frozen=True)
class Guarded:
    """Every blind's final outcome after both stages -- the input stage's included.

    One mapping rather than two, because a caller that has to merge the
    stages itself is a caller that can forget to.
    """

    outcomes: dict[str, Outcome]

    @property
    def actions(self) -> dict[str, Action]:
        """The blinds that should move now, and to what. Derived, never stored."""
        return {
            entity: outcome.action
            for entity, outcome in self.outcomes.items()
            if outcome.action is not None
        }

    @property
    def deferrals(self) -> dict[str, Deferral]:
        """The blinds whose answer is waiting on a deadline. Derived, never stored."""
        return {
            entity: outcome.deferral
            for entity, outcome in self.outcomes.items()
            if outcome.deferral is not None
        }


def guard_blinds(config: Config, guard: Guard) -> set[str]:
    """Which blinds `guard.targets` actually names.

    No `targets` at all means every blind -- a guard is a house-wide safety
    rule unless it says otherwise. A zone id stands for that zone's members,
    so "do not close anything in the bedroom" survives a blind being added to
    that zone later, which naming the blinds individually would not.

    A target naming nothing real contributes nothing (it is reported
    separately as `guard_unknown_target`) rather than being treated as a blind
    whose name happens not to exist yet -- otherwise a typo would widen the
    guard's apparent scope, both here and in
    `validation._check_guard_reachability`, which reads this same function so
    that "which blinds does this guard cover" has one answer rather than a
    static one and a runtime one that can disagree.
    """
    if not guard.targets:
        return set(config.blinds)

    out: set[str] = set()
    for target in guard.targets:
        if target in config.zones:
            out |= set(config.zones[target].members)
        elif target in config.blinds:
            out.add(target)
    return out


def screen(config: Config, world: World) -> Screening:
    """Run the `stage: input` guards: which blinds the engine must not be asked about.

    Takes no decision and no positions, and cannot: at this moment there is no
    candidate command, which is also why an `input` guard naming a direction
    is refused outright rather than guessed at (see `_check_honourable`).

    A blind an input guard claims gets its outcome here and is absent from
    `Screening.remaining`; `review` will not offer it to the output guards.
    """
    _check_honourable(config.guards)
    owners = resolve_ownership(config)
    staged = _staged(config, GUARD_STAGE_INPUT)

    outcomes: dict[str, Outcome] = {}
    for entity, blind in config.blinds.items():
        if entity not in owners:
            # A blind no zone owns has no decision to override; `engine.
            # resolve_ownership` skips it and so must this. `review` gets it
            # for free -- it iterates the decision, which already omits it.
            continue
        target = Target(blind=blind, zone=config.zones[owners[entity]])
        for index, guard, covered in staged:
            if entity not in covered:
                continue
            # No direction check: `_check_honourable` has already established
            # that every input-stage guard applies to `any`.
            if not evaluate_condition(guard.when, world, target, config.conditions):
                continue
            outcomes[entity] = _outcome(index, guard, entity, world, held=None)
            break

    return Screening(
        outcomes=outcomes,
        # Unowned blinds are excluded, not just unclaimed: `remaining` means
        # "the output stage may still judge these", and a blind no zone owns
        # never reaches a decision for it to judge.
        remaining=frozenset(owners) - set(outcomes),
    )


def review(
    config: Config,
    world: World,
    decision: Decision,
    positions: Mapping[str, int | None],
    screening: Screening,
) -> Guarded:
    """Run the `stage: output` guards over the engine's decision.

    `positions` is what each cover reports *now*, keyed by entity id: the
    caller must supply one for every blind it wants a directional guard judged
    against. A missing key and an explicit `None` mean the same thing -- the
    position could not be read -- and both make a directional guard fire, on
    the ground that an interlock disabled by a dead sensor is exactly the
    failure this schema exists to prevent. Positions are needed for nothing
    else: `applies_to` is the only field that asks where the blind is.

    `screening` is required, not optional. It is the only way to get a
    `Screening`, so requiring it here is what makes "run the input stage
    first" a fact about the type signature rather than a line in a docstring
    someone skips.
    """
    _check_honourable(config.guards)
    owners = resolve_ownership(config)
    staged = _staged(config, GUARD_STAGE_OUTPUT)

    outcomes: dict[str, Outcome] = dict(screening.outcomes)
    for entity, decided in decision.targets.items():
        if entity in outcomes:
            # Claimed at the input stage. First match wins across the two
            # stages too: a blind already answered for is not judged twice.
            continue

        blind = config.blinds.get(entity)
        if blind is None:
            msg = f"decision names {entity!r}, which is not a configured blind"
            raise GuardError(msg)
        target = Target(blind=blind, zone=config.zones[owners[entity]])
        current = positions.get(entity)

        fired: tuple[int, Guard] | None = None
        for index, guard, covered in staged:
            if entity not in covered:
                continue
            if not _direction_matches(index, guard, decided, current):
                continue
            if not evaluate_condition(guard.when, world, target, config.conditions):
                continue
            fired = (index, guard)
            break

        if fired is None:
            outcomes[entity] = Outcome(entity=entity, action=decided, reason=NO_GUARD)
        else:
            outcomes[entity] = _outcome(fired[0], fired[1], entity, world, held=decided)

    return Guarded(outcomes=outcomes)


def _staged(config: Config, stage: str) -> list[tuple[int, Guard, set[str]]]:
    """The guards belonging to one stage, in written order, with their blinds resolved.

    Written order is the whole conflict-resolution mechanism (there are no
    priorities), and `enumerate` runs over the *whole* list before filtering
    so that the index carried into a trace is the guard's position in
    `Config.guards` -- the same identity `validation` and a future `guard`
    subentry use -- not its position among the guards that happen to share a
    stage.
    """
    return [
        (index, guard, guard_blinds(config, guard))
        for index, guard in enumerate(config.guards)
        if guard.stage == stage
    ]


def _outcome(index: int, guard: Guard, entity: str, world: World, held: Action | None) -> Outcome:
    """The outcome of one guard firing on one blind.

    `held` is the action that was going to happen -- the engine's decision at
    the output stage, `None` at the input stage, where there is none.
    """
    reason = f"{guard.label(index)}: {guard.policy}"
    common = {
        "entity": entity,
        "reason": reason,
        "guard": index,
        "policy": guard.policy,
        "stage": guard.stage,
    }

    if guard.policy == GUARD_SKIP:
        return Outcome(action=None, **common)

    if guard.policy == GUARD_FORCE:
        # `then` is resolved here for the same reason a rule's is resolved in
        # the engine: a `Ref` reaching `planner.plan` raises, and a `force`
        # guard holding a helper reference (the house's flower keeper does
        # exactly that) is otherwise unplannable.
        return Outcome(action=resolve_action(guard.then, world), **common)

    return Outcome(
        action=None,
        deferral=Deferral(
            guard=index,
            name=guard.name,
            stage=guard.stage,
            max_wait=guard.max_wait,
            on_timeout=guard.on_timeout,
            recheck_every=guard.recheck_every,
            held=held,
        ),
        **common,
    )


def _direction_matches(index: int, guard: Guard, decided: Action, current: int | None) -> bool:
    """Whether `decided` is the kind of movement `guard.applies_to` is about.

    `closing` is a **decreasing position and nothing else** -- never the
    slats. See `const.GUARD_CLOSING`: reading it as "any downward movement
    including the slats" would make nine of the house's thirteen interlocks
    start refusing tilt commands they have always allowed.

    Three answers, in the order they are decided:

    - `any` never asks. It is not "moving in either direction", it is "do not
      look at the direction at all", which is why a guard written `any` still
      fires on a decision that moves nothing.
    - A `KEEP` position axis is not a movement of the position axis, so a
      directional guard does not fire on it however the tilt axis changes.
      This is the whole point of the axis being singular.
    - Otherwise it is arithmetic against where the blind is now, and if that
      is unreadable the guard fires: a directional interlock silenced by a
      missing state is worse than one that blocks a command it need not have.

    The `planner.DEAD_BAND` is deliberately *not* consulted. A guard judges
    what a command means; the planner judges whether it is worth sending.
    Blocking a command the planner would have skipped anyway costs nothing,
    whereas a guard that stopped protecting inside the dead band would be a
    silent hole exactly where the two layers meet.
    """
    if guard.applies_to == GUARD_ANY:
        return True

    position = decided.position
    if position is KEEP:
        return False
    if isinstance(position, Ref):
        msg = (
            f"{guard.label(index)}: cannot judge direction against an unresolved {position!r}; "
            f"a decision reaching a guard must already have its refs resolved"
        )
        raise GuardError(msg)

    if current is None:
        return True

    if guard.applies_to == GUARD_CLOSING:
        return position < current
    return position > current


def _check_honourable(guards: tuple[Guard, ...]) -> None:
    """Refuse the whole evaluation if any guard cannot be applied as written.

    Every guard is checked, not just the ones that would fire, and not just
    the stage being run: a check that only trips on some worlds is a check
    that ships broken and surfaces on the day the guard was needed. Each of
    these is also an `ERROR` from `validation.validate`, so a configuration
    that reaches here failing one has skipped validation entirely.
    """
    for index, guard in enumerate(guards):
        where = guard.label(index)
        if guard.policy not in GUARD_POLICIES:
            msg = f"{where}: unknown policy {guard.policy!r}"
            raise GuardError(msg)
        if guard.stage not in GUARD_STAGES:
            msg = f"{where}: unknown stage {guard.stage!r}"
            raise GuardError(msg)
        if guard.applies_to not in GUARD_DIRECTIONS:
            msg = f"{where}: unknown 'applies_to' {guard.applies_to!r}"
            raise GuardError(msg)
        if guard.stage == GUARD_STAGE_INPUT and guard.applies_to != GUARD_ANY:
            # An input guard removes the target before anything has been
            # decided for it, so there is no candidate command to have a
            # direction. Applying it anyway would drop the blind from
            # decisions its author never meant to touch -- the same
            # over-blocking `const.GUARD_CLOSING` warns about, reached through
            # the stage instead of the axis -- and ignoring it would silently
            # delete a safety interlock. Neither is acceptable, so it is a
            # configuration error (`validation`'s `guard_input_direction`).
            msg = (
                f"{where}: an 'input' guard cannot name a direction "
                f"({guard.applies_to!r}); there is no decided command to have one"
            )
            raise GuardError(msg)
        if guard.policy == GUARD_FORCE and guard.then is None:
            msg = f"{where}: policy 'force' with no 'then' to impose"
            raise GuardError(msg)
        if guard.policy == GUARD_DEFER:
            if guard.max_wait is UNSET:
                msg = f"{where}: policy 'defer' without 'max_wait'"
                raise GuardError(msg)
            if guard.on_timeout not in GUARD_TIMEOUTS:
                msg = f"{where}: policy 'defer' with unusable 'on_timeout' {guard.on_timeout!r}"
                raise GuardError(msg)
