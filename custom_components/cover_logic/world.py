"""An immutable snapshot of everything the engine is allowed to read.

Taking one snapshot per evaluation is what removes the whole race-condition
class: the same snapshot always produces the same decision, no matter what
changes underneath while the decision is being acted upon.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import datetime as dt
from typing import Any

from .model import Blind, Zone


@dataclass(frozen=True, slots=True)
class Event:
    """What prompted this evaluation.

    `person` carries who arrived or left, so a rule can ask whether the event
    belongs to the occupant of the zone currently being decided.
    """

    kind: str = "state_change"
    person: str | None = None


@dataclass(frozen=True, slots=True)
class Target:
    """What is being decided right now.

    Conditions such as `sun_hits_target` are relative to this, which is how the
    same rule works for a house of any orientation.
    """

    blind: Blind
    zone: Zone


@dataclass(frozen=True, slots=True)
class SunTimes:
    """Today's sunrise and sunset, naive local, exactly as `World.now` is.

    Astronomy lives in `ha_world`, the one seam allowed to import Home
    Assistant, so `condition: sun` here stays plain datetime arithmetic and
    tests can state any sun of any day without a `hass`. `None` means the sun
    does not rise or set today at all -- a polar latitude, which HA's own
    condition treats as "not satisfied" rather than an error.
    """

    sunrise: dt.datetime | None = None
    sunset: dt.datetime | None = None


@dataclass(frozen=True)
class World:
    """One immutable snapshot of every entity state and attribute the engine may read."""

    states: Mapping[str, str]
    attributes: Mapping[tuple[str, str], Any] = field(default_factory=dict)
    now: dt.datetime = dt.datetime(1970, 1, 1)
    event: Event = Event()
    sun: SunTimes = SunTimes()
    # When each entity last *changed* state, naive local like `now`. Only
    # `condition: state`'s `for:` reads it; see that condition's own note for
    # why a missing entry ignores `for:` rather than failing the condition.
    since: Mapping[str, dt.datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Copy the mappings so the snapshot cannot be changed from outside.

        States are strings, so copying the mapping is enough. Attribute values
        are not: Home Assistant hands out lists and dicts (`hvac_modes`,
        forecast lists) that stay shared with its state machine, so a shallow
        copy would leave the snapshot mutable through those objects -- exactly
        what this class exists to prevent. Only the referenced attributes are
        ever in here, a handful of entries, so deep-copying costs nothing worth
        measuring.

        See docs/rationale.md -- "Why `World` takes a defensive copy".
        """
        object.__setattr__(self, "states", dict(self.states))
        object.__setattr__(self, "attributes", deepcopy(dict(self.attributes)))
        object.__setattr__(self, "since", dict(self.since))

    def held_for(self, entity_id: str, now: dt.datetime) -> dt.timedelta | None:
        """How long `entity_id` has been in its current state, or `None` if unknown."""
        changed = self.since.get(entity_id)
        return None if changed is None else now - changed

    def state(self, entity_id: str) -> str | None:
        """Return the entity's state, or `None` if it is not in the snapshot."""
        return self.states.get(entity_id)

    def attribute(self, entity_id: str, attr: str) -> Any | None:
        """Return one attribute of the entity, or `None` if it is not set."""
        return self.attributes.get((entity_id, attr))

    def number(self, entity_id: str, default: float, attribute: str | None = None) -> float:
        """Read a state or attribute as a float, falling back to `default`."""
        raw = (
            self.attribute(entity_id, attribute) if attribute is not None else self.state(entity_id)
        )
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
