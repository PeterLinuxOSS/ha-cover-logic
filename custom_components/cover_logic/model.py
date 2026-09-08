"""Frozen data types for cover_logic. No Home Assistant imports.

An action is a pair of axes — height and slats — and either axis may say
"leave it alone". This collapses what used to be five action kinds and eleven
named constants into a single shape.
"""

from dataclasses import dataclass, field
from typing import Self

from .const import GUARD_ANY, GUARD_STAGE_OUTPUT


class Keep:
    """Sentinel meaning 'do not touch this axis'.

    A singleton so that `is KEEP` works and so that equality of two Actions
    built independently still holds.
    """

    # Quoted: self-referencing the class from inside its own body, before the
    # name `Keep` is bound. Without `from __future__ import annotations` (see
    # pyproject.toml -- core bans it) this annotation is evaluated eagerly at
    # class-body execution time, and `Keep` does not exist yet at that point.
    _instance: "Keep | None" = None

    def __new__(cls) -> Self:
        """Return the one and only `Keep` instance, creating it on first use."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        """Return the debug repr, always `"KEEP"`."""
        return "KEEP"

    def __reduce__(self):
        """Unpickle back to the singleton instead of a new `Keep`."""
        return (Keep, ())


KEEP = Keep()


class Unset:
    """Sentinel meaning 'this key was not written at all'.

    Distinct from `None`, which for `Guard.max_wait` is a *value*: `max_wait:
    null` says "wait without any limit", which two of the house's five defers
    genuinely mean. Absence and `null` therefore cannot share a spelling, or
    `validation` could not tell a guard that states an unlimited wait from one
    that forgot to state anything -- see `docs/rationale.md`, "Why `defer`
    states both `max_wait` and `on_timeout`".

    Singleton for the same reasons `Keep` is: `is UNSET` works, and two
    `Guard`s built independently (one from YAML, one from subentries) still
    compare equal.
    """

    _instance: "Unset | None" = None

    def __new__(cls) -> Self:
        """Return the one and only `Unset` instance, creating it on first use."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        """Return the debug repr, always `"UNSET"`."""
        return "UNSET"

    def __reduce__(self):
        """Unpickle back to the singleton instead of a new `Unset`."""
        return (Unset, ())


UNSET = Unset()


@dataclass(frozen=True, slots=True)
class Ref:
    """A value read from a helper entity at evaluation time.

    `default` is used when the entity is missing or unparsable — it mirrors
    Jinja's `| int(34)` fallback in the template being replaced.
    """

    entity: str
    default: int


Value = int | Keep | Ref


@dataclass(frozen=True, slots=True)
class Action:
    """What to do to one blind's position and tilt axes."""

    position: Value = KEEP
    tilt: Value = KEEP


@dataclass(frozen=True, slots=True)
class Blind:
    """One physical cover and the facts the engine needs about it."""

    entity: str
    facade_azimuth: float | None = None
    tolerance: float = 45.0
    travel_time: float = 60.0
    tilt_after_arrival: bool = True
    has_tilt: bool = True


@dataclass(frozen=True, slots=True)
class Zone:
    """A group of blinds decided together, optionally tied to occupants."""

    id: str
    members: tuple[str, ...]
    occupants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Mode:
    """A named operating mode. `when is None` marks the fallback mode."""

    id: str
    when: dict | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    """One row of a first-match-wins list. `when is None` means 'otherwise'."""

    then: Action
    when: dict | None = None
    events: frozenset[str] | None = None
    name: str = ""


@dataclass(frozen=True, slots=True)
class Guard:
    """One safety interlock: a condition, a policy, and what it applies to.

    A guard is deliberately nothing more than that. `when` is an ordinary
    condition body -- the same dialect `conditions.py` already evaluates for
    modes and rules, refs included -- rather than a second condition language
    living inside guards, which would give the same idea two owners and let
    them diverge (`docs/rationale.md`, "Why a guard's `when` is the ordinary
    condition dialect").

    Fields:

    - `policy` -- one of `const.GUARD_POLICIES`. Checked by `validation`, not
      by the parser, so an unknown one is a reported problem rather than an
      unloadable configuration.
    - `when` -- condition body, or `None` for "always".
    - `targets` -- blind entity ids and/or zone ids. Empty means every blind;
      a zone id stands for its members. (The two namespaces cannot collide:
      a zone id may not contain a `.` and a Home Assistant entity id always
      does.)
    - `applies_to` -- which movement this guard is about; see
      `const.GUARD_CLOSING` for why `closing` is the position axis alone.
    - `stage` -- `input` (drop the target before the engine decides it) or
      `output` (override the action the engine decided).
    - `max_wait`/`on_timeout`/`recheck_every` -- `defer` only. `max_wait` is
      `UNSET` when the key was absent, `None` when it was written as `null`
      (wait indefinitely), otherwise a whole number of seconds.
    - `then` -- `force` only: the action to impose.
    - `name` -- free label, for traces and the UI.

    Order in `Config.guards` is meaning, not presentation: guards resolve
    first-match-wins, exactly like rules, so a contradiction between two of
    them is settled by which one is written first rather than by a numeric
    priority nobody can keep globally consistent.
    """

    policy: str
    when: dict | list | None = None
    targets: tuple[str, ...] = ()
    applies_to: str = GUARD_ANY
    stage: str = GUARD_STAGE_OUTPUT
    max_wait: "int | Unset | None" = UNSET
    on_timeout: str | None = None
    recheck_every: int | None = None
    then: Action | None = None
    name: str = ""

    def label(self, index: int) -> str:
        """How this guard is named to a human: its position, plus its name if it has one.

        A method rather than two private helpers because both the health
        report (`validation`) and the evaluation trace (`guards`) name the
        same guard, and a reader who sees `guard #3 'wind'` in one and
        something else in the other has to work out for themselves that they
        are the same row. `index` is the argument because a guard's identity
        *is* its position in a first-match-wins list -- it has no id of its
        own (see `validation._guard_owner`).
        """
        return f"guard #{index} {self.name!r}" if self.name else f"guard #{index}"


@dataclass(frozen=True, slots=True)
class ManualDetection:
    """Which callers' movements must *not* be read as somebody moving a blind by hand.

    `ignore_while_on` names entities that are `on` for exactly as long as an
    automated mover is running -- a script, most usefully, since a Home
    Assistant `script.<x>` entity is `on` only during its own run. A state
    change arriving while any of them is `on` came from that, not from a
    person.

    Named for the mechanism rather than for scripts. The spec that asked for
    this called the field `ignore_scripts`, but the test is "is this entity
    `on`", and restricting it to the `script.` domain would be a constraint
    the code does not actually have: an `input_boolean` a house sets around
    its own bulk movements works identically.

    This is the one part of the configuration the engine never reads. It is
    here rather than in `entry.options` (where `dry_run` lives) so that it
    travels with an export and is covered by the migration gate like
    everything else -- a house's own declaration of what is not a person is
    exactly the kind of thing that goes missing when it lives outside the
    configuration.
    """

    ignore_while_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """The whole parsed configuration: blinds, zones, modes, rules and guards."""

    blinds: dict[str, Blind]
    zones: dict[str, Zone]
    modes: tuple[Mode, ...]
    rules: dict[str, tuple[Rule, ...]]
    conditions: dict[str, dict] = field(default_factory=dict)
    values: dict[str, Ref] = field(default_factory=dict)
    guards: tuple[Guard, ...] = ()
    manual_detection: ManualDetection = ManualDetection()
