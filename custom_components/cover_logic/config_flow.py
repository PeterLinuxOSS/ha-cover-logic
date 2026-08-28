"""Config flow for Cover Logic: the setup menu, plus registering the subentry flows.

**The `user` step is a menu, not a form.** Before phase 5 this was one text
field asking for a configuration file path -- a dead end for anyone who just
installed the integration from HACS and has no file to point at (the
owner's own words, quoted in `options_flow.py`'s module docstring). It is
now `async_show_menu` over four ways to start, each its own step below:

- `blinds_now` (recommended): a multi-select over `cover` entities; one
  `blind` subentry per entity chosen.
- `from_file`: today's original behaviour, kept for migration -- a
  configuration file path, validated on submit exactly as before.
- `from_example`: imports `docs/example-config.yaml`, the worked example for
  a different, invented house (see that file's own header and
  `tests/test_example_config.py`) -- a plausible-looking starting point
  instead of an empty one. Only offered where that file actually ships
  alongside this checkout (see `conformance.repo_example_config_path`); see
  `async_step_from_example`'s own docstring for what happens on an install
  where it does not.
- `empty`: a config entry with no subentries at all.

`async_set_unique_id`/`_abort_if_unique_id_configured` still run first, in
`async_step_user`, before the menu is even shown -- exactly where they ran
before this task, so the single-instance guarantee does not depend on which
menu item is picked. Every one of the four branches ends in
`async_create_entry`, which is what actually enforces "only one instance":
`ConfigFlow.async_step_user` is Home Assistant's own one entry point for a
brand-new flow (`source: user`), so there is no route into `blinds_now`/
`from_file`/`from_example`/`empty` that skips it.

"Whatever this creates must load, even incomplete." A brand-new install
picking `blinds_now` has zero zones and zero modes; `empty` has nothing at
all. `config_from_subentries` (`config_store.py`) still builds a `Config`
from either -- it only rejects a structurally broken shape (a required key
missing), never an incomplete-but-well-formed one. `validate()` is what
finds `blind_without_zone`/`no_fallback_mode` on such a `Config`, and
`__init__.async_setup_entry` turns those into `ConfigEntryNotReady` exactly
as it already does for any subentry-backed entry with an ERROR-severity
problem (this predates phase 5). That is a deliberate line, not an oversight:
`ConfigEntryNotReady` is a normal, expected state for an entry whose
subentries still need work, not a crash -- the entry keeps existing, and its
subentries (and, since the previous task, the options-flow menu over them)
stay reachable regardless of whether setup succeeded, which is how a user
actually finishes what `blinds_now`/`empty` started. What must never happen
is `config_from_subentries` or `async_setup_entry` raising something that is
*not* one of these two clean, already-handled outcomes -- see
`__init__.async_setup_entry`'s own docstring for the one place this had to
change to keep that true for an entry with genuinely zero subentries
(`entry.subentries` truthiness stopped being the signal for "read from
subentries"; `CONF_CONFIG_PATH`'s presence in `entry.data` is, now).

The four setup steps are this module's whole content. The subentry flows
that let a user build the same configuration by clicking, one
`blind`/`zone`/`value`/`condition`/`mode`/`rule` row at a time, instead of
hand-writing YAML, live in `subentry_flow.py` -- this module only registers
them (`async_get_supported_subentry_types` returns that module's
`SUBENTRY_FLOW_HANDLERS`) rather than defining them itself. See
`subentry_flow.py`'s own module docstring for why: those six classes are
also the phase 5 options-flow menu's (`options_flow.py`) second door onto
the same six types, so they belong to neither this module nor that one.
`config_store.py` is the reviewed, tested *reader* of those subentries -- it
already decides exactly which keys it reads out of each type's `data` and
which are required; the flows in `subentry_flow.py` exist only to *produce*
data in that exact shape, never to invent a spelling of their own.
`blinds_now`/`from_example` below reuse that same reader's write side
(`config_store.subentries_from_config`) rather than hand-building subentry
`data` a second time -- see each step's own docstring.

Unlike `__init__.py`, this module has no reason to defer its Home Assistant
imports: it is never imported by `cover_logic/__init__.py` itself (only
discovered and imported by Home Assistant's own config flow machinery, or
-- behind `pytest.importorskip("homeassistant")` -- by `tests/ha/`), so it
never runs anywhere `homeassistant` is not already installed. It also has no
reason to defer its import of `options_flow.py`, or vice versa: since
`subentry_flow.py` split off as the module both of them actually needed,
neither of the two imports the other any more, so `async_get_options_flow`
below imports `CoverLogicOptionsFlow` at module level like anything else.
"""

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
import voluptuous as vol

from .config_schema import ConfigError, load_config_file
from .config_store import BLIND, subentries_from_config
from .conformance import repo_example_config_path
from .const import CONF_CONFIG_PATH, CONFIG_ENTRY_VERSION, DEFAULT_CONFIG_PATH, DOMAIN
from .options_flow import CoverLogicOptionsFlow
from .services import _title_for
from .subentry_flow import SUBENTRY_FLOW_HANDLERS
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


_STEP_BLINDS_NOW = "blinds_now"
_STEP_FROM_FILE = "from_file"
_STEP_FROM_EXAMPLE = "from_example"
_STEP_EMPTY = "empty"

# The one field `blinds_now` asks for: which `cover` entities to build a
# `blind` subentry for, one each. Filtered to `domain="cover"` for the same
# reason `BlindSubentryFlowHandler`'s own `entity` field is (see that class's
# docstring) -- a blind is a `cover` entity by definition.
_BLINDS_NOW_FIELD = "entities"
_BLINDS_NOW_SCHEMA = vol.Schema(
    {
        vol.Required(_BLINDS_NOW_FIELD, default=list): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="cover", multiple=True)
        ),
    }
)


def _subentry_data(
    subentry_type: str, data: dict[str, Any], title: str
) -> config_entries.ConfigSubentryData:
    """Build one `ConfigSubentryData` entry for `async_create_entry`'s `subentries=`.

    A `TypedDict`, not a real object -- `ConfigFlow.async_create_entry`
    wraps each of these into a real `ConfigSubentry` itself once the entry is
    actually created, the same way `hass.config_entries.async_add_subentry`
    does for `services._async_import_config` (see that function's own
    docstring); this step just needs to hand back plain, JSON-shaped data in
    that exact shape, not build a `ConfigSubentry` by hand.
    """
    return {"data": data, "subentry_type": subentry_type, "title": title, "unique_id": None}


class CoverLogicConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cover Logic: the setup menu, and its four branches."""

    VERSION = CONFIG_ENTRY_VERSION

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the setup menu -- see the module docstring for its four branches.

        Checks "already configured" first, before even showing the menu --
        this integration supports exactly one instance, and every branch
        below ends in `async_create_entry`, so there is no route past this
        check into a second entry.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        return self.async_show_menu(
            step_id="user",
            menu_options=[_STEP_BLINDS_NOW, _STEP_FROM_FILE, _STEP_FROM_EXAMPLE, _STEP_EMPTY],
        )

    async def async_step_blinds_now(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Set up blinds now: a `blind` subentry for each `cover` entity picked.

        The recommended branch -- the one thing a brand-new user can answer
        without knowing anything about zones, modes or rules yet. Creates no
        zone, mode or rule: `validate()` will report `blind_without_zone` and
        `no_fallback_mode` for the result (there is nothing yet to own these
        blinds or decide anything for them), which is `ConfigEntryNotReady`,
        not a crash -- see the module docstring's "whatever this creates
        must load, even incomplete" section. The options-flow menu (previous
        task) is exactly where a user goes next to add a zone and a mode.

        At least one entity must be picked: zero would produce the same
        entry `empty` already offers on purpose, under a menu item that
        promises blinds.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            entities: list[str] = user_input[_BLINDS_NOW_FIELD]
            if not entities:
                errors["base"] = "no_blinds_selected"
            else:
                subentries = [
                    _subentry_data(BLIND, {"entity": entity}, entity) for entity in entities
                ]
                return self.async_create_entry(title="Cover Logic", data={}, subentries=subentries)

        schema = self.add_suggested_values_to_schema(_BLINDS_NOW_SCHEMA, user_input)
        return self.async_show_form(step_id=_STEP_BLINDS_NOW, data_schema=schema, errors=errors)

    async def async_step_from_file(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Load a configuration from a YAML file: the original `user` step, unchanged.

        Kept for migration -- moving a configuration from one house/instance
        to another, or restoring one written by hand -- not removed just
        because it is no longer the first thing a new user sees. Validated
        on submit exactly as before this task: loaded and run through
        `validate()`, so a broken path never becomes a broken entry.
        """
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
            step_id=_STEP_FROM_FILE,
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_from_example(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Start from the example configuration: import `docs/example-config.yaml`.

        That file ships only in this project's own development checkout
        (see `conformance.repo_example_config_path`'s docstring) -- a HACS
        install or a manual copy of just `custom_components/cover_logic` has
        no sibling `docs/` directory at all. Rather than offering a menu
        button that can only ever fail there, or aborting the whole flow the
        one time it is picked on such an install (which would force starting
        the entire setup over, the opposite of "always land somewhere you
        can continue from"), this delegates to `async_step_example_not_
        available` -- a real step of its own, not just a different
        `step_id` rendered from here: Home Assistant's own flow manager
        checks, on *every* step result, that `result["step_id"]` names a
        real `async_step_<step_id>` method (`FlowManager._raise_if_step_
        does_not_exist`) -- including the render that shows a form, not only
        the resume that follows it -- so reusing this method's own name for
        that other form's `step_id` would raise `UnknownStep` the moment a
        real installation (not this project's own tests, which call each
        method directly and so never hit the manager's check) rendered it.

        Where the file *is* available, reuses `config_store.
        subentries_from_config` -- the exact function `services.
        _async_import_config` already uses to turn an imported `Config` into
        subentries -- rather than a second, hand-rolled conversion; see that
        function's own docstring for the ordering (assign strictly
        increasing `order` to modes/rules, then self-check the result reads
        back to an equal `Config`) this reuses unchanged. `services.
        _title_for` supplies the same cosmetic subentry titles
        `_async_import_config`/`async_migrate_entry` already give an
        imported subentry, for the same reason those two reuse it instead of
        inventing a third copy.
        """
        example_path = repo_example_config_path()
        if example_path is None:
            return await self.async_step_example_not_available(user_input)

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] | None = None

        if user_input is not None:
            try:
                config = await self.hass.async_add_executor_job(load_config_file, example_path)
                items = subentries_from_config(config)
            except (ConfigError, OSError) as err:
                errors["base"] = "invalid_config"
                description_placeholders = {"error_detail": str(err)}
            else:
                subentries = [
                    _subentry_data(subentry_type, data, _title_for(subentry_type, data))
                    for subentry_type, data in items
                ]
                # `guards` (see `config_store.py`'s own docstring) is not a
                # subentry type -- it is carried through in `entry.data`
                # unchanged, the same way `services._async_import_config`
                # carries it for an import onto an *existing* entry. The
                # example config has none today, but a future one might.
                return self.async_create_entry(
                    title="Cover Logic",
                    data={"guards": list(config.guards)},
                    subentries=subentries,
                )

        return self.async_show_form(
            step_id=_STEP_FROM_EXAMPLE,
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_example_not_available(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """The dead end `async_step_from_example` delegates to when there is no example to load.

        A real step of its own, reachable only from `async_step_from_example`
        -- never listed in the `user` step's own `menu_options` -- so Home
        Assistant's flow manager has a genuine `async_step_example_not_
        available` to dispatch a resubmission to; see the caller's own
        docstring for why that is not optional. On submit, returns to the
        main menu (`async_step_user` again) rather than aborting the flow
        outright, so picking this menu item on an install that cannot honour
        it costs nothing more than one extra screen.
        """
        if user_input is not None:
            return await self.async_step_user(None)
        return self.async_show_form(step_id="example_not_available", data_schema=vol.Schema({}))

    async def async_step_empty(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Start empty: a config entry with no subentries at all.

        `validate()` will report `no_fallback_mode` (there is not even a
        fallback mode yet) for the resulting empty `Config` -- again
        `ConfigEntryNotReady`, not a crash; see the module docstring's
        "whatever this creates must load, even incomplete" section, and
        `__init__.async_setup_entry`'s own docstring for the change that
        keeps *this* branch specifically (zero subentries, no
        `CONF_CONFIG_PATH`) from crashing outright.

        Still a form, not a direct `async_create_entry` off the menu choice
        itself: an empty schema still needs one submit, so a user reaches
        this deliberately rather than by a single misclick on the menu.
        """
        if user_input is not None:
            return self.async_create_entry(title="Cover Logic", data={}, subentries=[])
        return self.async_show_form(step_id=_STEP_EMPTY, data_schema=vol.Schema({}))

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return the subentry flows this integration supports."""
        return SUBENTRY_FLOW_HANDLERS

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow: the main menu over the same subentries.

        Imported at module level like anything else -- `options_flow.py`
        imports the six subentry-type classes and their shared machinery
        from `subentry_flow.py`, not from here, so there is no cycle left
        for a deferred import to work around. See this module's own
        docstring and `subentry_flow.py`'s for why that module, not either
        door, is where those classes now live.
        """
        return CoverLogicOptionsFlow()
