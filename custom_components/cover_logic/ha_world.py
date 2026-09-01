"""Build a `World` snapshot from a live Home Assistant state machine.

The only module in this package allowed to import `homeassistant` -- see
`tests/test_purity.py`'s `PURE_MODULES` list, which this file is
deliberately absent from. Everything downstream of `build_world` (engine,
conditions, config_schema) stays pure; this is the seam where the live house
turns into the plain data those pure modules understand.
"""

from typing import Any

from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
import homeassistant.util.dt as dt_util

from .config_schema import referenced_entities
from .model import Config
from .world import Event, SunTimes, World


def build_world(hass: HomeAssistant, config: Config, event: Event | None = None) -> World:
    """Snapshot exactly what `config` reads out of `hass`'s state machine.

    Reads only the entities and attributes named by
    `config_schema.referenced_entities(config)` -- not the whole state
    machine -- so the snapshot stays small and predictable no matter how many
    unrelated entities the house has.

    A referenced entity that is not currently in `hass.states`, or a
    referenced attribute the entity's state does not currently have, is
    simply left out of the snapshot; `World.state`/`World.attribute` then
    return `None` for it, which the engine already treats as "unknown". No
    placeholder such as `"unknown"` or `"unavailable"` is invented here --
    that would fabricate a state the house never reported.

    `World.now` is `homeassistant.util.dt.now()` with its tzinfo stripped.
    `conditions._time`/`_parse_hhmm` only ever compare `.time()` components
    (which are naive regardless of whether the source datetime carries
    tzinfo), so the tzinfo itself is never consulted by the pure engine --
    only the wall-clock reading is. Keeping `World.now` naive matches the
    convention the pure engine and its test suite already use (see
    pyproject.toml's ruff `DTZ` note and `tests/scenarios.py`'s `NOW`
    constant), while still sourcing that wall-clock reading from HA's own
    DST-aware local-time API rather than a hand-rolled UTC-offset conversion
    of the kind that has broken across a DST transition before (see
    docs/rationale.md / CLAUDE.md's `strftime` "+00:00" pitfall). The value
    itself -- the moment in time -- is exactly what a live Jinja `now()`
    reports at the same instant.
    """
    states: dict[str, str] = {}
    attributes: dict[tuple[str, str], Any] = {}

    for entry in referenced_entities(config):
        if isinstance(entry, tuple):
            entity_id, attribute = entry
            state = hass.states.get(entity_id)
            if state is None or attribute not in state.attributes:
                continue
            attributes[(entity_id, attribute)] = state.attributes[attribute]
        else:
            state = hass.states.get(entry)
            if state is None:
                continue
            states[entry] = state.state

    now = dt_util.now()
    return World(
        states=states,
        attributes=attributes,
        now=now.replace(tzinfo=None),
        event=event if event is not None else Event(),
        sun=_sun_times(hass, now),
    )


def _sun_times(hass: HomeAssistant, now: Any) -> SunTimes:
    """Resolve today's sunrise and sunset to naive local, as `World.now` is.

    Always filled, not gated on `referenced_entities`: these are computed from
    the house's own latitude rather than read off an entity, so there is no
    entity whose absence could stand for "do not ask". `None` survives as
    `None` -- a polar day genuinely has no sunrise, and inventing one would
    make `condition: sun` answer a question the sky did not.
    """

    def naive(event: str) -> Any:
        moment = get_astral_event_date(hass, event, now.date())
        return None if moment is None else dt_util.as_local(moment).replace(tzinfo=None)

    return SunTimes(sunrise=naive(SUN_EVENT_SUNRISE), sunset=naive(SUN_EVENT_SUNSET))
