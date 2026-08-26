"""Pure evaluation of conditions against a World snapshot.

The condition dialect is a subset of the native Home Assistant condition
schema, so the same structures the user edits in the UI are what the engine
runs — plus two target-relative built-ins that generic HA has no notion of.
"""

import datetime as dt
from typing import Any

import jinja2

from .const import COND_EVENT_TARGETS_ZONE, COND_REF, COND_SUN_HITS_TARGET
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
    if kind == "template":
        return _template(cond, world)
    if kind == COND_SUN_HITS_TARGET:
        return _sun_hits_target(cond, world, target)
    if kind == COND_EVENT_TARGETS_ZONE:
        return _event_targets_zone(world, target)

    msg = f"unknown condition type: {kind!r}"
    raise ValueError(msg)


def _state(cond: dict, world: World) -> bool:
    attribute = cond.get("attribute")
    if attribute is not None:
        actual = world.attribute(cond["entity_id"], attribute)
    else:
        actual = world.state(cond["entity_id"])
    wanted = cond["state"]
    if isinstance(wanted, (list, tuple)):
        return actual in wanted
    return actual == wanted


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


def _parse_hhmm(text: str) -> dt.time:
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
    after = _parse_hhmm(cond["after"]) if "after" in cond else None
    before = _parse_hhmm(cond["before"]) if "before" in cond else None

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
        moment = _parse_hhmm(text)
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
