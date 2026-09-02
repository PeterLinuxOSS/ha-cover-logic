"""When a time-derived condition next changes its answer.

**The integration had no clock for its own conditions.** Every evaluation is
triggered by a referenced entity changing, and `conditions.py` answers `state`'s
`for:`, `time` and `sun` by comparing against `World.now` -- so those three
change answer at an instant that produces no state change at all and therefore
woke nothing up.

Measured on the owner's live house, 2026-09-02. Nine of its conditions are
time-derived, and only one of the boundaries they name coincides with a state
change:

    sunset                     `sun.sun` flips -- covered
    sunrise - 21 min           nothing happens (`je_noc`)
    sunset - 20 min            nothing happens (`vecer`)
    sunrise + 10 min           nothing happens (`vecer`)
    12:30                      nothing happens
    bed off for 120 s          nothing happens (`vstali`)

What made the house look correct anyway is an accident: its configuration reads
`sensor.sun_solar_azimuth`, which changes every ~66 s, so every boundary was
crossed within the minute by a re-evaluation that had nothing to do with it.
`RECONCILE_FLOOR_SECONDS` (see `docs/rationale.md`, "Why evaluation has a
floor") bounds the error for a house without such an entity, but bounding it to
five minutes is not the same as being on time -- a house that should open at
`sunrise - 21 min` should not open at `sunrise - 16 min`.

So this module answers one question -- *how many seconds until the earliest
instant at which some condition in this configuration could answer
differently* -- and `coordinator._reschedule` arms its existing timer for it.

**Only boundaries inside the current local day are visible**, because
`World.sun` carries *today's* sunrise and sunset and nothing else (see
`SunTimes`). At 20:00 tomorrow's `sunrise - 21 min` is not knowable from the
snapshot; after midnight it is, and it is still hours away. The floor covers
the gap, which is the division of labour those two are meant to have: the floor
is the guarantee, this is the precision.

A boundary is reported only while it is still ahead. An answer that already
flipped needs no timer -- the evaluation asking this question is the one that
saw it flip.

No `homeassistant` import and no clock of its own: the instant comes from
`World.now`, so a test states the time rather than racing it. In
`tests/test_purity.py`'s `PURE_MODULES`, alongside the rest of the decision
core.
"""

import datetime as dt

from .conditions import parse_hhmm
from .config_schema import all_condition_nodes
from .const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from .model import Config
from .world import World


def next_boundary(config: Config, world: World) -> float | None:
    """Seconds until the earliest time-derived condition boundary still ahead.

    `None` means this configuration has no such boundary left today -- either
    it uses no time-derived condition at all, or every one of them has already
    flipped.
    """
    ahead = [
        seconds
        for node in all_condition_nodes(config)
        for seconds in _node_boundaries(node, world)
        if seconds > 0
    ]
    return min(ahead) if ahead else None


def _node_boundaries(node: dict, world: World) -> list[float]:
    """Every instant `node` could change answer at, as seconds from `world.now`."""
    kind = node.get("condition")
    if kind == "state":
        return _held_for_boundary(node, world)
    if kind == "time":
        return _time_boundaries(node, world)
    if kind == "sun":
        return _sun_boundaries(node, world)
    return []


def _held_for_boundary(node: dict, world: World) -> list[float]:
    """When `for:` will have elapsed, if it has not yet.

    A missing `since` yields nothing, matching `conditions._state`: an entity
    the snapshot cannot date **ignores** `for:` rather than failing it, so
    there is no boundary to wait for.
    """
    if "for" not in node:
        return []
    entity_id = node.get("entity_id")
    if not isinstance(entity_id, str):
        return []
    held = world.held_for(entity_id, world.now)
    if held is None:
        return []
    return [float(int(node["for"]) - held.total_seconds())]


def _time_boundaries(node: dict, world: World) -> list[float]:
    """Today's `after:`/`before:` clock times, as seconds from now."""
    out = []
    for key in ("after", "before"):
        if key in node:
            at = dt.datetime.combine(world.now.date(), parse_hhmm(node[key]))
            out.append((at - world.now).total_seconds())
    return out


def _sun_boundaries(node: dict, world: World) -> list[float]:
    """Today's sun events plus this node's offsets, as seconds from now.

    Both bounds are collected regardless of which way the condition combines
    them (`conditions._sun` has one OR case among ANDs). Asking "when could
    this answer change" needs every instant either bound names; which of them
    actually flips the answer is the next evaluation's job, and an extra
    evaluation is cheap -- the dead band turns "already there" into no command.
    """
    events = {SUN_EVENT_SUNRISE: world.sun.sunrise, SUN_EVENT_SUNSET: world.sun.sunset}
    out = []
    for key, offset_key in (("after", "after_offset"), ("before", "before_offset")):
        event = events.get(node.get(key))
        if event is None:
            continue
        at = event + dt.timedelta(seconds=int(node.get(offset_key, 0)))
        out.append((at - world.now).total_seconds())
    return out
