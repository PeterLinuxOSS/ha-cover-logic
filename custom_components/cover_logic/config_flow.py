"""Minimal config flow for Cover Logic: one field, the configuration file path.

Deliberately minimal -- the full rule-editing UI is phase 4, not built here.
This step exists only so a config entry can be created at all, and only ever
lets one be created (`async_set_unique_id` / `_abort_if_unique_id_configured`,
checked before the form is even shown). The file is validated on submit --
loaded and run through `validate()` -- so a broken path never becomes a
broken entry; without this, the failure would only surface later as
`async_setup_entry` raising `ConfigEntryNotReady` on every start.

Unlike `__init__.py`, this module has no reason to defer its Home Assistant
imports: it is never imported by `cover_logic/__init__.py` itself (only
discovered and imported by Home Assistant's own config flow machinery, or
-- behind `pytest.importorskip("homeassistant")` -- by `tests/ha/`), so it
never runs anywhere `homeassistant` is not already installed.
"""

import logging

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
import voluptuous as vol

from .config_schema import ConfigError, load_config_file
from .const import CONF_CONFIG_PATH, DEFAULT_CONFIG_PATH, DOMAIN
from .validation import ERROR, validate

_LOGGER = logging.getLogger(__name__)


async def _describe_problems(hass: HomeAssistant, path: str) -> str | None:
    """Load and validate `path`; return a problem summary, or `None` if it is clean.

    "Clean" means no `ERROR`-severity problem -- a `WARNING`-only config is
    accepted here exactly as `async_setup_entry` accepts it. `load_config_file`
    does a blocking `Path.read_text`, so it runs inside
    `hass.async_add_executor_job` rather than directly on the event loop this
    step is awaited from -- the same reasoning as `__init__.async_setup_entry`.
    """
    try:
        config = await hass.async_add_executor_job(load_config_file, path)
    except ConfigError as err:
        return str(err)
    except OSError as err:
        return str(err)

    errors = [problem for problem in validate(config) if problem.severity == ERROR]
    if not errors:
        return None

    return "; ".join(f"{problem.code}: {problem.message}" for problem in errors)


class CoverLogicConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cover Logic."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for the configuration file path; validate it before creating the entry.

        Checks "already configured" first, before even showing the form --
        this integration supports exactly one instance.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] | None = None

        if user_input is not None:
            path = user_input[CONF_CONFIG_PATH]
            problem = await _describe_problems(self.hass, path)
            if problem is None:
                return self.async_create_entry(title="Cover Logic", data=user_input)

            errors["base"] = "invalid_config"
            description_placeholders = {"error_detail": problem}
            _LOGGER.debug("cover_logic config %s rejected: %s", path, problem)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CONFIG_PATH,
                    default=(user_input or {}).get(CONF_CONFIG_PATH, DEFAULT_CONFIG_PATH),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )
