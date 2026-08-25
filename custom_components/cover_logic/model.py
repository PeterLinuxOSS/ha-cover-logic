"""Frozen data types for cover_logic. No Home Assistant imports.

An action is a pair of axes — height and slats — and either axis may say
"leave it alone". This collapses what used to be five action kinds and eleven
named constants into a single shape.
"""

from dataclasses import dataclass, field
from typing import Self


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


@dataclass(frozen=True)
class Config:
    """The whole parsed configuration: blinds, zones, modes and rules."""

    blinds: dict[str, Blind]
    zones: dict[str, Zone]
    modes: tuple[Mode, ...]
    rules: dict[str, tuple[Rule, ...]]
    conditions: dict[str, dict] = field(default_factory=dict)
    values: dict[str, Ref] = field(default_factory=dict)
    guards: tuple = ()
