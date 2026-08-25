"""Cover Logic: a universal rule-based cover (blind) controller.

Phase 1 is the decision core (`config_schema`, `model`, `world`,
`conditions`, `engine`, `validation`) -- pure Python, no Home Assistant
imports, tested standalone. Phase 2 adds the Home Assistant layer; this
module is the config entry's entry point (`async_setup_entry`,
`async_unload_entry`).

Every Home Assistant name this file needs is either deferred behind
`TYPE_CHECKING` (for annotations, quoted as strings since this project does
not use `from __future__ import annotations` -- see `model.py`'s `Keep`
docstring for the established precedent) or imported inside the function
that needs it at runtime -- never at module level. This is not stylistic:
`custom_components/cover_logic/__init__.py` is executed, as any package's
`__init__.py` is, merely by importing one of the pure submodules
(`from cover_logic.config_schema import ...`) -- including from the
system-Python test run, which has no `homeassistant` installed at all (see
`tests/ha/conftest.py`'s own docstring on the same constraint). An
unconditional `import homeassistant` here would break every one of those
imports, not just this module's own tests.
"""

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from .config_schema import ConfigError, load_config_file
from .const import CONF_CONFIG_PATH
from .model import Config
from .validation import ERROR, validate

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import CoverLogicCoordinator

_LOGGER = logging.getLogger(__name__)

# Plain strings, not `homeassistant.const.Platform.SENSOR` -- an enum import
# would be one more Home Assistant name at module level, exactly what this
# file's own docstring rules out. Home Assistant's platform-forwarding calls
# accept plain strings just as well.
PLATFORMS: list[str] = ["sensor"]


@dataclass
class CoverLogicData:
    """What `entry.runtime_data` carries: the parsed config and its coordinator."""

    config: Config
    coordinator: "CoverLogicCoordinator"


if TYPE_CHECKING:
    type CoverLogicConfigEntry = ConfigEntry[CoverLogicData]


async def async_setup_entry(hass: "HomeAssistant", entry: "CoverLogicConfigEntry") -> bool:
    """Set up cover_logic from a config entry.

    Loads the YAML file named by `entry.data[CONF_CONFIG_PATH]` and runs
    `validate()` over it. An `ERROR`-severity problem refuses the start --
    `ConfigEntryNotReady`, naming every such problem -- rather than running
    with rules nobody checked; a `WARNING`-severity one is only logged, not
    fatal. A missing or unparsable file fails the same clean way, not with a
    raw traceback: both `ConfigError` (bad YAML/schema) and `OSError`
    (missing file, permissions, ...) become `ConfigEntryNotReady`.

    The file is re-read on every call, including a config entry reload --
    nothing about the parsed `Config` is cached across calls or stashed at
    module level. That is deliberate: editing the YAML and reloading the
    entry is the whole debugging loop for phases 2-4.
    """
    # Deferred: see the module docstring for why this cannot be a top-level
    # import.
    from homeassistant.exceptions import ConfigEntryNotReady  # noqa: PLC0415

    path = entry.data[CONF_CONFIG_PATH]

    try:
        config = load_config_file(path)
    except ConfigError as err:
        msg = f"cover_logic: config at {path!r} is invalid: {err}"
        raise ConfigEntryNotReady(msg) from err
    except OSError as err:
        msg = f"cover_logic: config at {path!r} could not be read: {err}"
        raise ConfigEntryNotReady(msg) from err

    problems = validate(config)
    errors = [problem for problem in problems if problem.severity == ERROR]
    if errors:
        summary = "; ".join(f"{problem.code}: {problem.message}" for problem in errors)
        msg = f"cover_logic: config at {path!r} has errors: {summary}"
        raise ConfigEntryNotReady(msg)

    for problem in problems:
        _LOGGER.warning("cover_logic: config at %s: %s: %s", path, problem.code, problem.message)

    # Deferred: see the module docstring for why this cannot be a top-level
    # import.
    from .coordinator import CoverLogicCoordinator  # noqa: PLC0415

    coordinator = CoverLogicCoordinator(hass, config)
    await coordinator.async_setup()

    entry.runtime_data = CoverLogicData(config=config, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: "HomeAssistant", entry: "CoverLogicConfigEntry") -> bool:
    """Unload a config entry, leaving nothing behind.

    Platforms are unloaded first, before the coordinator itself is torn
    down -- `sensor.py`'s entity unsubscribes from
    `CoverLogicCoordinator.add_listener` in its own `async_will_remove_from_hass`,
    so the coordinator must still be alive and holding that listener list
    while `async_unload_platforms` runs. A failed platform unload aborts
    before touching the coordinator at all, matching Home Assistant's own
    convention that an unload returning `False` leaves the entry's state
    untouched for a retry. Only once platforms are confirmed gone are the
    coordinator's own subscription and any pending debounce torn down via
    `CoverLogicCoordinator.async_unload`. This exists so a config entry reload
    -- unload, then set up again -- re-reads the file both times and starts a
    fresh coordinator each time; see `async_setup_entry`'s docstring for why
    the re-read matters.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    await entry.runtime_data.coordinator.async_unload()
    return True
