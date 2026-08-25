"""Watch state, evaluate the engine, and hold the current `Decision`.

The seam between "the house changed" and "the engine ran": subscribes to
exactly the entities `config_schema.referenced_entities(config)` names,
coalesces bursts of change into one evaluation, and keeps the last-known-good
`Decision` on the shelf so a later diagnostic sensor always has something to
show, even mid-error.

This module imports `homeassistant` unconditionally at module level, the same
choice `ha_world.py` makes and for the same reason (see that module's own
docstring and `tests/test_purity.py`'s `PURE_MODULES` list, which this file is
deliberately absent from): it is never imported at `cover_logic` package
import time. `__init__.py` only reaches this module through a local import
inside `async_setup_entry`, so importing `cover_logic` itself -- which is
what happens merely by importing any *pure* submodule, including from the
system-Python 3.12 test run that has no `homeassistant` installed at all --
never touches this file.
"""

from collections.abc import Callable
import datetime as dt
import logging

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import EventStateChangedData, async_track_state_change_event
import homeassistant.util.dt as dt_util

from .config_schema import referenced_entities
from .engine import Decision, evaluate
from .ha_world import build_world
from .model import Config

_LOGGER = logging.getLogger(__name__)

# Debounce window between the last watched state change and the evaluation it
# triggers.
#
# Two bursts this has to absorb, neither of which is "the weather updates
# every ~10 minutes" -- that cadence is far wider than any window chosen here
# could matter for, so window length is not tuned against it. What it is
# tuned against: a Home Assistant startup state restore, or a bulk automation
# write, that touches several watched entities in the same handful of
# event-loop ticks, each as its own `state_changed` event. 0.5s comfortably
# spans that (such a burst lands within tens of milliseconds of itself in
# practice) while still being short enough that a diagnostic sensor updating
# after it reads, to a person, as instantaneous.
DEBOUNCE_COOLDOWN = 0.5


class CoverLogicCoordinator:
    """Holds the current `Decision` for one config entry's `Config`.

    `decision` is the last successfully computed `Decision`, or `None` before
    the first evaluation has ever completed. A failing evaluation never blanks
    it -- see `_async_evaluate` -- it only ever updates `last_error`, so a
    diagnostic entity reading `decision` mid-outage shows the last good answer
    instead of nothing.
    """

    def __init__(self, hass: HomeAssistant, config: Config) -> None:
        """Store `hass` and `config`; subscribe nothing yet -- see `async_setup`."""
        self.hass = hass
        self.config = config
        self.decision: Decision | None = None
        self.last_error: str | None = None
        self.last_success: dt.datetime | None = None
        self._unsub_state_change: Callable[[], None] | None = None
        self._debouncer: Debouncer | None = None

    async def async_setup(self) -> None:
        """Subscribe to the config's referenced entities and run the first evaluation.

        Subscribing before the first evaluation (rather than after) would
        leave a window where a real state change is missed because nothing is
        listening yet; evaluating here, synchronously, closes that window and
        also satisfies the separate requirement that `decision` be populated
        at startup rather than only after the first subsequent state change.
        """
        self._debouncer = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=DEBOUNCE_COOLDOWN,
            immediate=False,
            function=self._async_evaluate,
        )

        entity_ids = sorted(_entity_ids(self.config))
        if entity_ids:
            self._unsub_state_change = async_track_state_change_event(
                self.hass, entity_ids, self._handle_state_change
            )

        await self._async_evaluate()

    async def async_unload(self) -> None:
        """Remove the state-change subscription and cancel any pending debounce.

        A leaked subscription or timer outlives the config entry that owns
        it -- concretely, a reload while editing the config would otherwise
        leave the old subscription (and a possibly-pending debounce call)
        alive alongside the new one, doubling up on evaluations against
        whichever `Config` object it captured.
        """
        if self._unsub_state_change is not None:
            self._unsub_state_change()
            self._unsub_state_change = None
        if self._debouncer is not None:
            self._debouncer.async_shutdown()
            self._debouncer = None

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Schedule a debounced evaluation; never evaluate inline here.

        `event` itself is not read -- `_async_evaluate` always takes a fresh
        `World` snapshot of every referenced entity, not just the one that
        changed, so there is nothing this handler needs from it beyond "some
        watched entity changed, evaluate soon". `self._debouncer` is only
        `None` before `async_setup` has run or after `async_unload` has --
        neither state has a live subscription that could call this, but the
        guard keeps that invariant from being silently load-bearing.
        """
        if self._debouncer is None:
            return
        self._debouncer.async_schedule_call()

    async def _async_evaluate(self) -> None:
        """Snapshot the world once and evaluate it, keeping `decision` on error.

        Any evaluation failure is caught, logged with its traceback, and
        recorded in `last_error` rather than propagated: a transient bad state
        (e.g. mid-reload, a helper briefly missing) must not blank a diagnostic
        sensor that was showing a perfectly good answer a moment ago. The
        failure stays visible -- in the log, and in `last_error` for a sensor to
        surface -- and is never swallowed silently.

        The catch is deliberately broad, not limited to `EngineError`. The
        failures this will actually meet in a house are not configuration
        invariants: a typo'd condition type raises `ValueError`, and a broken
        user template raises out of Jinja by design, because evaluating it as
        False could mean leaving the house open during a heatwave. Catching only
        `EngineError` would let those escape into the debouncer's callback,
        where `last_error` is never set and the sensor keeps showing a stale
        answer with no sign that anything is wrong.
        """
        world = build_world(self.hass, self.config)
        try:
            decision = evaluate(self.config, world)
        except Exception as err:
            _LOGGER.exception("cover_logic: evaluation failed, keeping previous decision")
            self.last_error = f"{type(err).__name__}: {err}"
            return

        self.decision = decision
        self.last_error = None
        self.last_success = dt_util.utcnow()


def _entity_ids(config: Config) -> set[str]:
    """Plain entity ids to subscribe to, one per `referenced_entities` entry.

    An attribute-read entry is `(entity_id, attribute)` -- there is no
    per-attribute subscription in Home Assistant's state-change event, so its
    `entity_id` is what gets watched; a change to any attribute (or the state
    itself) of that entity is what triggers re-evaluation.
    """
    return {
        entry[0] if isinstance(entry, tuple) else entry for entry in referenced_entities(config)
    }
