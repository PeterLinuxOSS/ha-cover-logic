"""Parity-test glue around the old-vocabulary translation.

The translation itself (`to_action`/`expected_actions`) lives in
`cover_logic.legacy` now -- it is not just a test helper, `sensor.py`'s
`matica_diff` uses the exact same functions to compare the engine against the
live matrix in the house. Re-exported here so `test_migration_gate.py` does
not need to change its import. `world_from_stav` stays here: it depends on
`jinja_bridge.now_for` and the `Stav` test fixture, both specific to this
offline gate, not something the production sensor has any use for.
"""

import datetime as dt

from cover_logic.legacy import expected_actions, to_action
from cover_logic.world import Event, SunTimes, World

from .jinja_bridge import now_for

__all__ = ["expected_actions", "to_action", "world_from_stav"]


def world_from_stav(stav, event: Event | None = None) -> World:
    """Feed the engine exactly the state the Jinja render saw.

    One axis now arrives by a different route. The scenario's `lighting_on`
    means "it is dusk"; the Jinja matrix reads that off a helper and the
    engine now derives it from the sky, so it is translated into `World.sun`
    here rather than left to reach the engine as a helper it no longer reads.

    All 92 160 scenarios still run and still have to match. What the gate no
    longer proves is *how* dusk is derived -- that has its own evidence, a
    minute-by-minute replay of 14 days of history (`docs/rationale.md`, "Why
    `vecer` reads the sky"). Stating that boundary is the point: a gate that
    quietly stopped covering something would be worse than one that covers
    less on purpose.
    """
    now = now_for(stav)
    return World(
        states={**stav.entity(), **_bed_for(stav)},
        attributes=stav.atributy(),
        now=now,
        event=event or Event(),
        sun=_sun_for(stav),
        # An hour, so every `for:` in the configuration is satisfied. The gate
        # varies *what* the house looks like, never how long it has looked
        # that way; a debounce that only delays a transition cannot change
        # which target a settled scenario resolves to.
        since={entity: now - dt.timedelta(hours=1) for entity in _BED_SENSORS},
    )


# The engine reads the bed sensor where it used to read a helper another
# automation derived from it. The scenario axis is unchanged -- it still means
# "somebody is asleep" -- so it is translated rather than left behind.
_BED_SENSORS = ("binary_sensor.postel_occupancy_postel_2",)


def _bed_for(stav) -> dict[str, str]:
    return dict.fromkeys(_BED_SENSORS, "on" if stav.some_sleeping else "off")


def _sun_for(stav) -> SunTimes:
    """Place sunrise/sunset so the dark window agrees with `stav.lighting_on`.

    `now_for` is midday, so "dusk" is expressed by putting sunset at `now` --
    which makes `after: sunset` true once its -20 min offset applies -- and
    "not dusk" by putting both events hours away on either side.
    """
    now = now_for(stav)
    sunrise = now - dt.timedelta(hours=6)
    sunset = now if stav.lighting_on else now + dt.timedelta(hours=6)
    return SunTimes(sunrise=sunrise, sunset=sunset)
