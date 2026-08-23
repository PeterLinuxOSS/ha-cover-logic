"""Pure evaluation of conditions against a World snapshot.

The condition dialect is a subset of the native Home Assistant condition
schema, so the same structures the user edits in the UI are what the engine
runs — plus two target-relative built-ins that generic HA has no notion of.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import jinja2

from .const import COND_EVENT_TARGETS_ZONE, COND_REF, COND_SUN_HITS_TARGET
from .world import Target, World

DEFAULT_AZIMUTH_ENTITY = "sensor.sun_solar_azimuth"
SUN_ENTITY = "sun.sun"

_JINJA = jinja2.Environment(undefined=jinja2.StrictUndefined)


def evaluate_condition(
    cond: dict | list | None,
    world: World,
    target: Target | None = None,
    registry: dict[str, dict] | None = None,
) -> bool:
    """Evaluate `cond`. `None` means 'no condition', which is True."""
    if cond is None:
        return True
    if isinstance(cond, list):
        return all(evaluate_condition(c, world, target, registry) for c in cond)

    kind = cond.get("condition")

    if kind == COND_REF:
        if registry is None:
            raise KeyError(cond["name"])
        return evaluate_condition(registry[cond["name"]], world, target, registry)

    if kind == "and":
        return all(
            evaluate_condition(c, world, target, registry) for c in cond["conditions"]
        )
    if kind == "or":
        return any(
            evaluate_condition(c, world, target, registry) for c in cond["conditions"]
        )
    if kind == "not":
        return not any(
            evaluate_condition(c, world, target, registry) for c in cond["conditions"]
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

    raise ValueError(f"unknown condition type: {kind!r}")


def _state(cond: dict, world: World) -> bool:
    actual = world.state(cond["entity_id"])
    wanted = cond["state"]
    if isinstance(wanted, (list, tuple)):
        return actual in wanted
    return actual == wanted


def _numeric_state(cond: dict, world: World) -> bool:
    """`default` mirrors Jinja's `| float(999)` fallback.

    A dead sensor must fall on the safe side, and which side that is depends on
    the rule — so the default is always explicit in the config, never implied.
    """
    value = world.number(
        cond["entity_id"],
        default=float(cond["default"]),
        attribute=cond.get("attribute"),
    )
    if "above" in cond and not value > float(cond["above"]):
        return False
    if "below" in cond and not value < float(cond["below"]):
        return False
    return True


def _parse_hhmm(text: str) -> dt.time:
    hour, minute = (int(part) for part in text.split(":")[:2])
    return dt.time(hour=hour, minute=minute)


def _time(cond: dict, world: World) -> bool:
    now = world.now.time()
    if "after" in cond and not now >= _parse_hhmm(cond["after"]):
        return False
    if "before" in cond and not now < _parse_hhmm(cond["before"]):
        return False
    return True


def _template(cond: dict, world: World) -> bool:
    """Escape hatch. Exposes the same globals a Home Assistant template gets."""
    rendered = _JINJA.from_string(cond["value_template"]).render(
        **_template_globals(world)
    )
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
        return world.now.replace(
            hour=moment.hour, minute=moment.minute, second=0, microsecond=0
        )

    return {
        "is_state": is_state,
        "states": states,
        "state_attr": state_attr,
        "now": lambda: world.now,
        "today_at": today_at,
    }


def _sun_hits_target(cond: dict, world: World, target: Target | None) -> bool:
    """True when the sun is within the target's facade sector.

    The sector is HALF-OPEN: [facade - tolerance, facade + tolerance). The
    template being replaced used `az >= 45 and az < 135`, and the scenario axis
    contains 45/135/225/315 on purpose, so an inclusive upper bound breaks
    parity on exactly those points.
    """
    if target is None or target.blind.facade_azimuth is None:
        return False
    if world.state(cond.get("sun_entity", SUN_ENTITY)) != "above_horizon":
        return False

    azimuth = world.number(
        cond.get("azimuth_entity", DEFAULT_AZIMUTH_ENTITY), default=-1.0
    )
    tolerance = float(cond.get("tolerance", target.blind.tolerance))
    delta = (azimuth - target.blind.facade_azimuth + 180.0) % 360.0 - 180.0
    return -tolerance <= delta < tolerance


def _event_targets_zone(world: World, target: Target | None) -> bool:
    if target is None or world.event.person is None:
        return False
    return world.event.person in target.zone.occupants
