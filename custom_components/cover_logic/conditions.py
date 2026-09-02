"""Pure evaluation of conditions against a World snapshot.

The condition dialect is a subset of the native Home Assistant condition
schema, so the same structures the user edits in the UI are what the engine
runs — plus two target-relative built-ins that generic HA has no notion of.
"""

import datetime as dt
from typing import Any

import jinja2

from .const import (
    COND_EVENT_TARGETS_ZONE,
    COND_MANUAL_MOVE,
    COND_REF,
    COND_SUN,
    COND_SUN_HITS_TARGET,
    EVENT_MANUAL_MOVE,
    GUARD_ANY,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
)
from .world import Target, World

DEFAULT_AZIMUTH_ENTITY = "sensor.sun_solar_azimuth"
SUN_ENTITY = "sun.sun"

# A real compass bearing lies in this half-open range -- used to reject a
# missing/unparsable azimuth reading by name rather than trusting a magic
# sentinel to stay clear of `_sun_hits_target`'s sector arithmetic.
_AZIMUTH_MIN = 0.0
_AZIMUTH_MAX = 360.0

# Autoescape off, StrictUndefined on. See docs/rationale.md -- "Why
# autoescape stays off" and "Why a broken template raises instead of
# evaluating False".
_JINJA = jinja2.Environment(undefined=jinja2.StrictUndefined)


def evaluate_condition(
    cond: dict | list | None,
    world: World,
    target: Target | None = None,
    registry: dict[str, dict] | None = None,
    *,
    _ref_chain: frozenset[str] = frozenset(),
) -> bool:
    """Evaluate `cond`. `None` means 'no condition', which is True.

    See docs/rationale.md -- "Why `_ref_chain` must be threaded through every
    recursive call".
    """
    if cond is None:
        return True
    if isinstance(cond, list):
        return all(
            evaluate_condition(c, world, target, registry, _ref_chain=_ref_chain) for c in cond
        )

    kind = cond.get("condition")

    if kind == COND_REF:
        if registry is None:
            raise KeyError(cond["name"])
        name = cond["name"]
        if name in _ref_chain:
            msg = (
                f"circular condition reference: {name!r} refers back to itself "
                f"via {sorted(_ref_chain)!r}"
            )
            raise ValueError(msg)
        return evaluate_condition(
            registry[name],
            world,
            target,
            registry,
            _ref_chain=_ref_chain | {name},
        )

    if kind == "and":
        return all(
            evaluate_condition(c, world, target, registry, _ref_chain=_ref_chain)
            for c in cond["conditions"]
        )
    if kind == "or":
        return any(
            evaluate_condition(c, world, target, registry, _ref_chain=_ref_chain)
            for c in cond["conditions"]
        )
    if kind == "not":
        return not any(
            evaluate_condition(c, world, target, registry, _ref_chain=_ref_chain)
            for c in cond["conditions"]
        )

    if kind == "state":
        return _state(cond, world)
    if kind == "numeric_state":
        return _numeric_state(cond, world)
    if kind == "time":
        return _time(cond, world)
    if kind == COND_SUN:
        return _sun(cond, world)
    if kind == "template":
        return _template(cond, world)
    if kind == COND_SUN_HITS_TARGET:
        return _sun_hits_target(cond, world, target)
    if kind == COND_EVENT_TARGETS_ZONE:
        return _event_targets_zone(world, target)
    if kind == COND_MANUAL_MOVE:
        return _manual_move(cond, world, target)

    msg = f"unknown condition type: {kind!r}"
    raise ValueError(msg)


def _state(cond: dict, world: World) -> bool:
    attribute = cond.get("attribute")
    if attribute is not None:
        actual = world.attribute(cond["entity_id"], attribute)
    else:
        actual = world.state(cond["entity_id"])
    wanted = cond["state"]
    matches = actual in wanted if isinstance(wanted, (list, tuple)) else actual == wanted
    if not matches or "for" not in cond:
        return matches
    return _held_long_enough(cond, world)


def _held_long_enough(cond: dict, world: World) -> bool:
    """Whether the entity has been in this state for at least `for:` seconds.

    An extension over Home Assistant, whose conditions have no `for:` at all --
    only triggers do. It exists because a debounce is sometimes the whole point
    of a flag: a bed sensor that flickers for thirty seconds must not move a
    blind. See docs/rationale.md, "Why `condition: state` takes `for:`".

    A missing `since` entry **ignores** `for:` rather than failing the
    condition: absence of timing information should not change what the
    condition means, and `ha_world` fills `since` for every entity it
    snapshots (pinned by its own test), so this path never runs in a live
    house.
    """
    held = world.held_for(cond["entity_id"], world.now)
    if held is None:
        return True
    return held >= dt.timedelta(seconds=int(cond["for"]))


def _numeric_state(cond: dict, world: World) -> bool:
    """`default` mirrors Jinja's `| float(999)` fallback.

    See docs/rationale.md -- "Why `numeric_state` requires an explicit
    `default`".
    """
    value = world.number(
        cond["entity_id"],
        default=float(cond["default"]),
        attribute=cond.get("attribute"),
    )
    # Two independent bounds, either of which may be absent. Collapsing the
    # second guard into `return not (...)` as SIM103 suggests would obscure
    # that symmetry in code the migration gate depends on being obviously
    # correct, so the rule is suppressed rather than followed here.
    if "above" in cond and not value > float(cond["above"]):
        return False
    if "below" in cond and not value < float(cond["below"]):  # noqa: SIM103
        return False
    return True


def parse_hhmm(text: str) -> dt.time:
    """Parse `HH:MM` or `HH:MM:SS`, honouring seconds when given.

    Home Assistant's own `cv.time` accepts `HH:MM:SS`, and this module's
    docstring claims to be a subset of the native condition schema -- a time
    condition copied out of HA's UI must not be silently truncated to the
    minute. `today_at`'s HH:MM-only default ("00:00") still works: a missing
    third field defaults to 0 seconds, same as before.
    """
    hour_text, minute_text, *rest = text.split(":")
    second = int(rest[0]) if rest else 0
    return dt.time(hour=int(hour_text), minute=int(minute_text), second=second)


def _time(cond: dict, world: World) -> bool:
    now = world.now.time()
    after = parse_hhmm(cond["after"]) if "after" in cond else None
    before = parse_hhmm(cond["before"]) if "before" in cond else None

    if after is not None and before is not None:
        if after <= before:
            # Same-day window, e.g. 08:00-18:00.
            return after <= now < before
        # Wrap-around window, e.g. 22:00-06:00. See docs/rationale.md --
        # "Why the wrap-around time window is an OR, not an AND".
        return now >= after or now < before
    if after is not None:
        return now >= after
    if before is not None:
        return now < before
    return True


def _sun(cond: dict, world: World) -> bool:
    """Test `world.now` against today's sunrise/sunset, as HA's `condition: sun` does.

    Offsets are **seconds**, matching `max_wait` and every other duration this
    schema takes, rather than HA's `"-00:20:00"` string. See
    docs/rationale.md -- "Why `condition: sun` takes seconds and skips the
    polar rollover".
    """
    before = cond.get("before")
    after = cond.get("after")
    sunrise = world.sun.sunrise
    sunset = world.sun.sunset

    # No sunrise/sunset today is a polar latitude; HA answers "not satisfied"
    # rather than raising, and a house that never sees the sun set should not
    # have its blinds decided by an exception.
    if sunrise is None and SUN_EVENT_SUNRISE in (before, after):
        return False
    if sunset is None and SUN_EVENT_SUNSET in (before, after):
        return False

    now = world.now
    before_at = dt.timedelta(seconds=int(cond.get("before_offset", 0)))
    after_at = dt.timedelta(seconds=int(cond.get("after_offset", 0)))

    # `before: sunrise` together with `after: sunset` names the dark window
    # around midnight, so it is an OR -- the one place this condition is not
    # the usual AND of two bounds. HA does the same.
    if (
        before == SUN_EVENT_SUNRISE
        and after == SUN_EVENT_SUNSET
        and sunrise is not None
        and sunset is not None
    ):
        return now < sunrise + before_at or now > sunset + after_at

    if before == SUN_EVENT_SUNRISE and sunrise is not None and now > sunrise + before_at:
        return False
    if before == SUN_EVENT_SUNSET and sunset is not None and now > sunset + before_at:
        return False
    if after == SUN_EVENT_SUNRISE and sunrise is not None and now < sunrise + after_at:
        return False
    return not (after == SUN_EVENT_SUNSET and sunset is not None and now < sunset + after_at)


def _template(cond: dict, world: World) -> bool:
    """Escape hatch. Exposes the same globals a Home Assistant template gets."""
    rendered = _JINJA.from_string(cond["value_template"]).render(**_template_globals(world))
    return rendered.strip().lower() in ("true", "on", "yes", "1")


def _template_globals(world: World) -> dict[str, Any]:
    def is_state(entity_id: str, value: str) -> bool:
        return world.state(entity_id) == value

    def states(entity_id: str) -> str:
        return world.state(entity_id) or "unknown"

    def state_attr(entity_id: str, attr: str) -> Any:
        return world.attribute(entity_id, attr)

    def today_at(text: str = "00:00") -> dt.datetime:
        moment = parse_hhmm(text)
        return world.now.replace(hour=moment.hour, minute=moment.minute, second=0, microsecond=0)

    return {
        "is_state": is_state,
        "now": lambda: world.now,
        "state_attr": state_attr,
        "states": states,
        "today_at": today_at,
    }


def _sun_hits_target(cond: dict, world: World, target: Target | None) -> bool:
    """True when the sun is within the target's facade sector.

    The sector is HALF-OPEN: [facade - tolerance, facade + tolerance). See
    docs/rationale.md -- "Why the sun sector is half-open".
    """
    if target is None or target.blind.facade_azimuth is None:
        return False
    if world.state(cond.get("sun_entity", SUN_ENTITY)) != "above_horizon":
        return False

    azimuth = world.number(
        cond.get("azimuth_entity", DEFAULT_AZIMUTH_ENTITY),
        default=-1.0,
        attribute=cond.get("azimuth_attribute"),
    )
    # -1.0 above is an "impossible" sentinel for a missing/unparsable
    # reading, not a real bearing -- routing it straight into the modular
    # sector arithmetic below would wrap it right back INTO the sector for a
    # facade near north (see docs/rationale.md -- "Why a missing azimuth
    # reading is checked explicitly, not routed through the sector maths").
    if not _AZIMUTH_MIN <= azimuth < _AZIMUTH_MAX:
        return False

    tolerance = float(cond.get("tolerance", target.blind.tolerance))
    delta = (azimuth - target.blind.facade_azimuth + 180.0) % 360.0 - 180.0
    return -tolerance <= delta < tolerance


def _event_targets_zone(world: World, target: Target | None) -> bool:
    if target is None or world.event.person is None:
        return False
    return world.event.person in target.zone.occupants


def _manual_move(cond: dict, world: World, target: Target | None) -> bool:
    """Did somebody move *this* blind by hand, and if `direction` is given, that way?

    Relative to the target, exactly as `sun_hits_target` and
    `event_targets_zone` are: a manual move somewhere else in the house is not
    something the blind being decided should react to. That is what makes the
    same rule work for every room without naming entities.

    `direction` is optional. Omitted means "moved at all", which is the honest
    reading of an absent key and matches how `applies_to` treats
    `GUARD_ANY` -- and `GUARD_ANY` is accepted as an explicit spelling of the
    same thing, so a rule can say it out loud.

    Every part is checked, not just the kind: an `Event` of another kind
    carries no `blind`, so matching on the kind alone would make this true for
    an arrival the moment somebody added `blind` to that event too.
    """
    if target is None or world.event.kind != EVENT_MANUAL_MOVE:
        return False
    if world.event.blind != target.blind.entity:
        return False
    wanted = cond.get("direction")
    if wanted is None or wanted == GUARD_ANY:
        return world.event.direction is not None
    return world.event.direction == wanted
