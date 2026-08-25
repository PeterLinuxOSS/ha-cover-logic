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

_LOGGER = logging.getLogger(__name__)


@dataclass
class CoverLogicData:
    """What `entry.runtime_data` carries.

    Just the parsed configuration for now -- a later phase's coordinator
    will need to add itself here too, once it exists.
    """

    config: Config


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

    entry.runtime_data = CoverLogicData(config=config)
    return True


async def async_unload_entry(hass: "HomeAssistant", entry: "CoverLogicConfigEntry") -> bool:
    """Unload a config entry, leaving nothing behind.

    No platforms are set up yet and nothing subscribes to state changes (that
    starts with the coordinator, a later phase), so there is nothing to
    forward-unload and no listener to cancel. This exists so a config entry
    reload -- unload, then set up again -- re-reads the file both times; see
    `async_setup_entry`'s docstring for why that matters.
    """
    return True
