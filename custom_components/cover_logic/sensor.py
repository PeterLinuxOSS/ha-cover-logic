"""The diagnostic sensor: `sensor.cover_logic_mode`.

Reports the engine's current mode, the per-blind targets and trace behind it,
and -- the point of this module -- a live comparison against the old Jinja
matrix (`sensor.zaluzie_cielovy_stav`) this engine is replacing. The offline
migration gate (`tests/parity/test_migration_gate.py`) checks the same thing
over 92 160 invented scenarios; this sensor checks it in the real house, in
real time, with real attributes and real timing, using the exact same
translation (`cover_logic.legacy`) so the two checks can never quietly drift
apart.

This module imports Home Assistant unconditionally at module level, the same
choice `coordinator.py`, `ha_world.py` and `config_flow.py` make: it is only
ever imported by Home Assistant's platform loader, or -- behind
`pytest.importorskip("homeassistant")` -- by `tests/ha/test_sensor.py`, never
by anything the system-Python 3.12 pure test run touches (see
`coordinator.py`'s own docstring for the full reasoning).

Built on `SensorEntity`, as a sensor platform should be. Importing it pulls in
`homeassistant.components.http`, which raises an `aiohttp` `DeprecationWarning`
at import time; `pyproject.toml` silences that one warning specifically rather
than letting a third-party deprecation we cannot fix decide which base class
production code uses.
"""

from collections.abc import Callable
import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory

from .legacy import expected_actions
from .model import KEEP, Action

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import CoverLogicConfigEntry
    from .coordinator import CoverLogicCoordinator
    from .engine import Decision

_LOGGER = logging.getLogger(__name__)

# The old matrix this engine is replacing. Its state is the mode it decided;
# its `ciele` attribute is a JSON string mapping entity id -> legacy action.
LEGACY_MATRIX_ENTITY = "sensor.zaluzie_cielovy_stav"

# `legacy.to_action`'s `pozicia`-with-no-`tilt` fallback reads this the same
# way the live Jinja template does -- see `legacy.py`'s own docstring.
LEGACY_TEPLOTNA_OCHRANA_ENTITY = "input_boolean.teplotna_ochrana_dom"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: "CoverLogicConfigEntry",
    async_add_entities: "AddEntitiesCallback",
) -> None:
    """Set up the one diagnostic sensor for this config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([CoverLogicModeSensor(coordinator, entry.entry_id)])


def _serialize_value(value: Any) -> Any:
    """`KEEP` has no JSON/attribute-serialisation form of its own -- spell it "keep"."""
    return "keep" if value is KEEP else value


def _serialize_action(action: Action) -> dict[str, Any]:
    return {"position": _serialize_value(action.position), "tilt": _serialize_value(action.tilt)}


class CoverLogicModeSensor(SensorEntity):
    """`sensor.cover_logic_mode`.

    The active mode, why every blind got its target, and how that compares
    to the old matrix right now.
    """

    _attr_has_entity_name = True
    _attr_name = "Mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_icon = "mdi:window-shutter-cog"

    def __init__(self, coordinator: "CoverLogicCoordinator", entry_id: str) -> None:
        """Store the coordinator to read from and derive a stable `unique_id`."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry_id}_mode"
        self._remove_listener: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator so a new `Decision` refreshes this entity."""
        self._remove_listener = self._coordinator.add_listener(self._handle_coordinator_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe -- an entity torn down on unload must not outlive its listener."""
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Unavailable only before the first evaluation has ever completed.

        A later failing evaluation keeps showing the last good `Decision`
        (see `coordinator.py`'s own docstring) -- `last_error` surfaces the
        problem as an attribute instead of blanking the entity.
        """
        return self._coordinator.decision is not None

    @property
    def native_value(self) -> str | None:
        """The active mode, or `None` before the first evaluation completes."""
        decision = self._coordinator.decision
        return decision.mode if decision is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """`targets`, `trace`, error/success bookkeeping, and the live matrix comparison."""
        decision = self._coordinator.decision
        targets: dict[str, Any] = {}
        trace: dict[str, str] = {}
        if decision is not None:
            targets = {
                entity: _serialize_action(action) for entity, action in decision.targets.items()
            }
            trace = dict(decision.trace)

        matica_mode, matica_diff = self._compare_matrix(decision)

        last_success = self._coordinator.last_success
        return {
            "targets": targets,
            "trace": trace,
            "last_error": self._coordinator.last_error,
            "last_success": last_success.isoformat() if last_success is not None else None,
            "matica_mode": matica_mode,
            "matica_diff": matica_diff,
        }

    def _compare_matrix(self, decision: "Decision | None") -> tuple[str | None, list[str] | None]:
        """Compare `decision` against the live old matrix.

        Returns `(matica_mode, matica_diff)`. `matica_diff` is `None` when
        there is nothing to compare against -- `sensor.zaluzie_cielovy_stav`
        does not exist, its `ciele` attribute is missing or unparsable, or
        this engine has never completed an evaluation -- and an empty list
        when the comparison ran and found no disagreement. The two must never
        be confusable: `None` means "not checked", `[]` means "checked, they
        agree".
        """
        hass = self.hass
        state = hass.states.get(LEGACY_MATRIX_ENTITY) if hass is not None else None
        if state is None:
            return None, None

        matica_mode = state.state
        raw_ciele = state.attributes.get("ciele")
        if raw_ciele is None:
            return matica_mode, None

        try:
            ciele = json.loads(raw_ciele)
            entities = ciele.items()
        except (TypeError, ValueError, AttributeError) as err:
            _LOGGER.warning(
                "cover_logic: could not parse %s's ciele attribute: %s",
                LEGACY_MATRIX_ENTITY,
                err,
            )
            return matica_mode, None

        if decision is None:
            return matica_mode, None

        teplotna_state = hass.states.get(LEGACY_TEPLOTNA_OCHRANA_ENTITY)
        teplotna_ochrana = teplotna_state is not None and teplotna_state.state == "on"

        diff: list[str] = []
        for entity, item in entities:
            try:
                want = expected_actions(item, variant="state", teplotna_ochrana=teplotna_ochrana)
            except (KeyError, TypeError, AssertionError) as err:
                _LOGGER.warning(
                    "cover_logic: could not translate %s's legacy target for %s: %s",
                    LEGACY_MATRIX_ENTITY,
                    entity,
                    err,
                )
                diff.append(entity)
                continue
            if decision.targets.get(entity) != want:
                diff.append(entity)

        return matica_mode, sorted(diff)
