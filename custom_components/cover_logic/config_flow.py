"""Config flow for Cover Logic: the setup menu, plus the subentry flows.

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

Everything below the four setup steps is phase 4: subentry flows that let a
user build the same configuration by clicking, one
`blind`/`zone`/`value`/`condition`/`mode`/`rule` row at a time, instead of
hand-writing YAML. `config_store.py` is the reviewed, tested *reader* of
those subentries -- it already decides exactly which keys it reads out of
each type's `data` and which are required; these flows exist only to
*produce* data in that exact shape, never to invent a spelling of their own.
`blinds_now`/`from_example` below reuse that same reader's write side
(`config_store.subentries_from_config`) rather than hand-building subentry
`data` a second time -- see each step's own docstring.

Unlike `__init__.py`, this module has no reason to defer its Home Assistant
imports: it is never imported by `cover_logic/__init__.py` itself (only
discovered and imported by Home Assistant's own config flow machinery, or
-- behind `pytest.importorskip("homeassistant")` -- by `tests/ha/`), so it
never runs anywhere `homeassistant` is not already installed.
"""

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
import voluptuous as vol

from .config_schema import ConfigError, load_config_file
from .config_store import (
    _ID_KEY,
    BLIND,
    CONDITION,
    MODE,
    RULE,
    VALUE,
    ZONE,
    config_from_subentries,
    duplicate_rule_order_problems,
    rule_owner_ids,
    subentries_from_config,
)
from .conformance import repo_example_config_path
from .const import (
    CONF_CONFIG_PATH,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CONFIG_PATH,
    DOMAIN,
    EVENT_ARRIVAL,
    EVENT_STATE_CHANGE,
)
from .services import _title_for
from .validation import ERROR, Problem, validate

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

        Imported locally, not at module level -- `options_flow.py` imports
        several names back out of *this* module (`SUBENTRY_FLOW_HANDLERS`,
        `_NEW_SUBENTRY_ID`, the per-type schema/data/validation methods) to
        satisfy the phase 5 brief's "one owner, two doors" requirement (see
        that module's own docstring), which would make a top-level import
        here a circular one. Deferring it to call time -- after both modules
        have finished loading -- sidesteps that without the fragility of a
        bottom-of-file import order dependency between the two.
        """
        from .options_flow import CoverLogicOptionsFlow  # noqa: PLC0415

        return CoverLogicOptionsFlow()


# ---------------------------------------------------------------------------
# Subentry flows, one per member of `config_store.SUBENTRY_TYPES`: `blind`,
# `zone`, `value`, `condition`, `mode`, `rule`.
# ---------------------------------------------------------------------------

# Not a real Home Assistant subentry id (those are ULIDs) -- used only as the
# dict key `_candidate_entry` gives a not-yet-created subentry so it can sit
# alongside the real ones for exactly one validation pass, then be discarded.
_NEW_SUBENTRY_ID = "__new__"


# Every ERROR-severity `validation.Problem.code` mapped to the subentry
# type(s) whose *own form* is where a user actually resolves it -- not the
# type that happened to be open when `validate()` noticed it. A blind's form
# has no `members` field, so saving a blind can never fix `blind_without_zone`
# no matter what else is true of the candidate; only a `zone` form's
# `members` can. That is the rule the task brief states directly: "a
# validation problem should block a form only if that form could fix it."
# `_blocks_on`, below, is the one place this is read.
#
# Every code left in this dict has exactly one owning *type*, and every
# subentry of that type is interchangeable for fixing it: `blind_without_zone`
# is fixed by putting the blind in *some* zone, and
# `no_fallback_mode`/`fallback_mode_not_last` are statements about the mode
# list as a whole, which any single mode's `order` (or its condition being
# cleared) can settle. Knowing the type is therefore enough.
#
# Codes where it is *not* enough live in `_ATTRIBUTED_CODES` instead, and are
# matched against `Problem.owners` -- the specific subentry, not just its
# type. Two families need that:
#
# - A condition body lives, verbatim, in three different *places* of the same
#   three types -- a `condition` subentry's own fields, a `mode`'s `when`, a
#   `rule`'s `if` -- so a dangling ref inside one `mode` must not block saving
#   an unrelated `condition`, or an unrelated second `mode`.
# - A rule's own two codes name one `(mode, zone)` pair each. A tie in
#   `bezny.terasa`, or a rule stranded by deleting the mode it named, is not
#   something *adding a rule to a different pair* either caused or can fix;
#   before attribution, either one blocked every rule save in the entry.
_CODE_OWNERS: dict[str, frozenset[str]] = {
    "zone_member_unknown": frozenset({ZONE}),
    "blind_in_two_zones": frozenset({ZONE}),
    "blind_without_zone": frozenset({ZONE}),
    "no_fallback_mode": frozenset({MODE}),
    "fallback_mode_not_last": frozenset({MODE}),
}

# The codes whose owning type is not enough to say which save could fix them
# -- see `_CODE_OWNERS`'s own comment. `_blocks_on` checks `Problem.owners`
# for these instead of `_CODE_OWNERS`. A rule's identity in `owners` is the
# `"<mode>.<zone>#<index>"` string `validation._rule_owner` builds, which
# `config_store.rule_owner_ids` maps a real subentry id onto -- see
# `RuleSubentryFlowHandler._candidate_id`.
_ATTRIBUTED_CODES = frozenset(
    {
        "unknown_condition_ref",
        "bad_condition_shape",
        "circular_condition_ref",
        "unknown_rule_key",
        "duplicate_rule_order",
    }
)


def _blocks_on(subentry_type: str, subentry_id: Any, problem: Problem) -> bool:
    """Whether saving this specific `(subentry_type, subentry_id)` subentry resolves `problem`.

    Two different questions, depending on the code:

    - For a code in `_ATTRIBUTED_CODES`, `validate()` already knows *which*
      subentry the problem lives in -- `problem.owners` -- so the check is
      identity, not type: does `(subentry_type, subentry_id)` appear there.
      This is what stops a dangling ref in `mode` "m1" from blocking a save
      of unrelated `mode` "m3", even though both are the same type: `problem.
      owners` for m1's own dangling ref is `{("mode", "m1")}`, which "m3"
      does not match, while m3's *own* dangling ref produces a problem whose
      owner is `{("mode", "m3")}`, which does.
    - For every other code, `_CODE_OWNERS` already says the single type that
      owns it (see its own comment for why type alone is enough there), so
      the check stays a membership test as before.

    A code missing from both fails as "never blocks anything" -- caught by a
    test asserting that code *does* block its owning form -- rather than as
    a crash.
    """
    if problem.code in _ATTRIBUTED_CODES:
        return (subentry_type, subentry_id) in problem.owners
    return subentry_type in _CODE_OWNERS.get(problem.code, frozenset())


class _SubentryStub:
    """Duck-typed stand-in for `homeassistant.config_entries.ConfigSubentry`.

    `config_store.config_from_subentries` only ever reads `.subentry_type`
    and `.data` off each subentry -- see that module's own "Duck-typed on
    purpose" docstring section. This is exactly that surface, used to carry
    a candidate subentry that does not exist as a real `ConfigSubentry` yet
    (it is still mid-form) into the same reader the real entry uses once
    saved.
    """

    __slots__ = ("data", "subentry_type")

    def __init__(self, subentry_type: str, data: dict[str, Any]) -> None:
        """Store the two attributes `config_from_subentries` reads."""
        self.subentry_type = subentry_type
        self.data = data


class _EntryStub:
    """Duck-typed stand-in for `homeassistant.config_entries.ConfigEntry`.

    Same reasoning as `_SubentryStub`: `config_from_subentries` only reads
    `.data` and `.subentries` off the entry itself.
    """

    __slots__ = ("data", "subentries")

    def __init__(self, data: dict[str, Any], subentries: dict[str, _SubentryStub]) -> None:
        """Store the two attributes `config_from_subentries` reads."""
        self.data = data
        self.subentries = subentries


def _candidate_entry(
    entry: Any, subentry_type: str, subentry_id: str, data: dict[str, Any]
) -> _EntryStub:
    """Build the entry `config_from_subentries` would see if `data` were saved.

    Every other existing subentry carries over unchanged. `subentry_id` is
    either `_NEW_SUBENTRY_ID` (an add: `data` becomes one extra subentry
    alongside the real ones) or an existing subentry's id (an edit: `data`
    replaces that subentry's own, so the edit is validated as a replacement,
    not as an addition sitting next to its own stale copy).
    """
    subentries = {
        sid: _SubentryStub(sub.subentry_type, dict(sub.data))
        for sid, sub in entry.subentries.items()
    }
    subentries[subentry_id] = _SubentryStub(subentry_type, data)
    return _EntryStub(dict(entry.data), subentries)


def _duplicate_errors(
    entry: Any, subentry_type: str, subentry_id: str, id_key: str, candidate_id: Any
) -> list[str]:
    """Catch a second `subentry_type` subentry claiming the same `id_key` value.

    `config_store` collapses every subentry of one type into a `dict` keyed
    by exactly this value (`blinds[blind.entity] = blind`,
    `zones[zone_id] = Zone(...)`, the equivalent for `values`). A second
    subentry claiming an entity or id already in use does not raise there --
    it silently overwrites the first in that dict, and `validate()` never
    sees the collision, because by the time it runs on a `Config` the two
    candidates have already collapsed into one. This must be checked here,
    over `entry.subentries` directly, for exactly the reason
    `config_store.duplicate_rule_order_problems` exists for a rule's
    `order`: see that function's own docstring.
    """
    for sid, sub in entry.subentries.items():
        if sid == subentry_id or sub.subentry_type != subentry_type:
            continue
        if sub.data.get(id_key) == candidate_id:
            return [f"{subentry_type} {candidate_id!r} is already configured"]
    return []


def _parses(entry: Any) -> bool:
    """Whether `entry` as it stands today can be read into a `Config` at all."""
    try:
        config_from_subentries(entry)
    except ConfigError:
        return False
    return True


class _SubentryFlowBase(config_entries.ConfigSubentryFlow):
    """Shared add/edit/validate machinery for the `blind`, `zone` and `value` flows.

    A subclass sets two class attributes and one method:

    - `subentry_type`: one of `config_store.BLIND`/`ZONE`/`VALUE`.
    - `id_key`: the field in that type's `data` that must be unique among
      its own type (`"entity"` for a blind, `"id"` for a zone or value) --
      see `_duplicate_errors`. `None` for a type with no such field at all
      (`rule`, whose identity is `(mode, zone, order)`), which skips the
      uniqueness check; such a type must override `_candidate_id` and
      `_title` instead.
    - `_build_schema(entry)`: the `vol.Schema` for this type's form, given
      the entry (so `zone`'s member multi-select can read the currently
      configured blinds off it).

    A subclass may also override these, all no-ops by default:

    - `_to_data(entry, user_input)`: reshape a raw, schema-coerced submission
      into the exact `dict` `config_store` expects for this type's `data`.
      `blind`/`zone`/`value` submit already in that shape, so they never
      override this; `condition`/`mode`/`rule` do -- see their own classes
      for why a native HA selector's output is not, quite, that shape yet.
      Takes `entry` explicitly (only `rule` reads it, for `_configured_ids`)
      rather than reaching for `self._get_entry()` -- that is what lets
      `options_flow.py` drive this exact method from a bare instance of the
      class, outside any running `ConfigSubentryFlow`, for the phase 5 menu;
      see that module's own docstring for why "one owner, two doors" depends
      on this method needing nothing from `self` at all.
    - `_to_form_values(data)`: the inverse, for prefilling a reconfigure form
      from a saved subentry's `data`.
    - `_initial_values(entry)`: what to prefill an *add* form with, for a
      type where sensible defaults depend on the rest of the entry
      (`rule`'s `order`). `None` -- prefill nothing -- for everyone else.
    - `_candidate_id(candidate, subentry_id, data)`: the identity an
      attributed `Problem.owners` entry is compared against. `data[id_key]`
      by default, which is exactly what `config_from_subentries` uses as a
      condition's or mode's own name.

    And one hook with no default, always empty:

    - `_local_problems(user_input)`: checks that must run *before*
      `_to_data` can even be called -- a submission that is ambiguous in a
      way `_to_data` would otherwise have to silently pick a winner for
      (`mode`'s and `rule`'s "named or inline condition, not both").
      Returning anything here skips `_to_data` and `_blocking_errors`
      entirely for this attempt, the same as a `_blocking_errors` result
      would.

    Removal needs no code here at all: a subentry is deleted by Home
    Assistant's own built-in subentry UI, which never calls into this class
    -- nothing here holds a reference to a subentry that would need
    releasing first.
    """

    subentry_type: str
    id_key: str | None

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Return this type's form schema. Overridden per subclass."""
        raise NotImplementedError

    def _to_data(self, entry: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        """Reshape a coerced submission into `config_store`'s expected `data`.

        Identity by default; `entry` is unused here, only threaded through
        for the one override (`rule`) that needs it.
        """
        return user_input

    def _to_form_values(self, data: dict[str, Any]) -> dict[str, Any]:
        """Invert `_to_data`, to prefill a reconfigure form. Identity by default."""
        return data

    def _initial_values(self, entry: Any) -> dict[str, Any] | None:
        """Prefill values for a fresh add form. Nothing by default."""
        return None

    def _local_problems(self, user_input: dict[str, Any]) -> list[str]:
        """Flow-only checks that must block before `_to_data` runs at all. None by default."""
        return []

    def _candidate_id(self, candidate: Any, subentry_id: str, data: dict[str, Any]) -> Any:
        """The identity `_blocks_on` matches an attributed `Problem.owners` entry against.

        `candidate` is the entry as it would be *after* this save, since a
        type whose identity is positional (`rule`) can only work it out
        relative to its finished neighbours.
        """
        return data.get(self.id_key)

    def _title(self, data: dict[str, Any]) -> str:
        """Return the subentry's display title: the value at `id_key`."""
        return str(data[self.id_key])

    def _blocking_errors(self, entry: Any, subentry_id: str, data: dict[str, Any]) -> list[str]:
        """Problems that must block saving `data` as this subentry, or `[]` if none.

        Runs the pipeline `config_store`'s own docstrings describe as,
        together, one source's complete set of checks: `_duplicate_errors`
        (a collapse `validate()` cannot see, see its own docstring), then
        `config_from_subentries` (structural), then `validate()` (semantic),
        then `duplicate_rule_order_problems` (the one thing `validate()`
        cannot see once subentries collapse into a `Config` -- see that
        function's own docstring).

        Only `ERROR`-severity problems block, matching how
        `_describe_problems` above treats the YAML path -- and even then,
        only the ones `_blocks_on` says this exact
        `(subentry_type, candidate_id)` save can actually resolve; see
        `_CODE_OWNERS`/`_ATTRIBUTED_CODES` for how that is decided and why
        the rest do not block a save that could not have fixed them anyway.

        A `ConfigError` gets the same treatment in spirit, by a cruder
        route: it is raised before there is a `Config` to attribute anything
        against, so the only question askable about it is whether *this save*
        is what broke the entry. If the entry already fails to parse without
        this save, the answer is no, and blocking would lock the user out of
        every form at once -- the integration is already refusing to load, so
        the way out has to stay open. (This is reachable: a `value` subentry
        deleted out from under a rule that refs it makes
        `config_schema._parse_axis` raise, and Home Assistant offers no veto
        hook on subentry removal.) The save that *introduces* a parse failure
        into a healthy entry still blocks, which is the case that matters.
        """
        candidate_id = data.get(self.id_key) if self.id_key is not None else None
        if self.id_key is not None:
            duplicate = _duplicate_errors(
                entry, self.subentry_type, subentry_id, self.id_key, candidate_id
            )
            if duplicate:
                return duplicate

        candidate = _candidate_entry(entry, self.subentry_type, subentry_id, data)
        try:
            config = config_from_subentries(candidate)
        except ConfigError as err:
            return [] if not _parses(entry) else [str(err)]

        problems = validate(config) + duplicate_rule_order_problems(candidate)
        candidate_id = self._candidate_id(candidate, subentry_id, data)
        return [
            f"{problem.code}: {problem.message}"
            for problem in problems
            if problem.severity == ERROR and _blocks_on(self.subentry_type, candidate_id, problem)
        ]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Add a new subentry of this type."""
        return await self._step(user_input, step_id="user")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Edit an existing subentry of this type."""
        return await self._step(user_input, step_id="reconfigure")

    async def _step(
        self, user_input: dict[str, Any] | None, *, step_id: str
    ) -> config_entries.SubentryFlowResult:
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry() if step_id == "reconfigure" else None
        subentry_id = subentry.subentry_id if subentry is not None else _NEW_SUBENTRY_ID

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] | None = None

        if user_input is not None:
            problems = self._local_problems(user_input)
            data: dict[str, Any] | None = None
            if not problems:
                data = self._to_data(entry, user_input)
                problems = self._blocking_errors(entry, subentry_id, data)
            if not problems:
                title = self._title(data)
                if subentry is None:
                    return self.async_create_entry(title=title, data=data)
                return self.async_update_and_abort(entry, subentry, title=title, data=data)

            errors["base"] = "invalid_config"
            description_placeholders = {"error_detail": "; ".join(problems)}
            _LOGGER.debug(
                "cover_logic %s subentry %s rejected: %s", self.subentry_type, subentry_id, problems
            )

        current = (
            user_input
            if user_input is not None
            else (self._to_form_values(subentry.data) if subentry else self._initial_values(entry))
        )
        schema = self.add_suggested_values_to_schema(self._build_schema(entry), current)
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )


# `facade_azimuth`/`tolerance` are compass degrees; `has_tilt`/
# `tilt_after_arrival` mirror `model.Blind`'s own boolean defaults exactly,
# so a blind added through the UI without touching those two fields behaves
# identically to one added by hand-written YAML that omits them.
_BOX = selector.NumberSelectorMode.BOX

_BLIND_SCHEMA = vol.Schema(
    {
        vol.Required("entity"): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="cover")
        ),
        vol.Optional("facade_azimuth"): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=360, step=1, unit_of_measurement="°", mode=_BOX
            ),
        ),
        vol.Optional("tolerance", default=45): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=180, step=1, unit_of_measurement="°", mode=_BOX
            ),
        ),
        vol.Optional("travel_time", default=60): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=600, step=1, unit_of_measurement="s", mode=_BOX
            ),
        ),
        vol.Optional("has_tilt", default=True): selector.BooleanSelector(),
        vol.Optional("tilt_after_arrival", default=True): selector.BooleanSelector(),
    }
)


class BlindSubentryFlowHandler(_SubentryFlowBase):
    """Add, edit or remove one `blind` subentry.

    `entity` is filtered to the `cover` domain -- a blind is a `cover`
    entity by definition (`model.Blind.entity`), so nothing else is ever a
    valid choice.
    """

    subentry_type = BLIND
    id_key = "entity"

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Return the static blind schema; it needs nothing from `entry`."""
        return _BLIND_SCHEMA


_VALUE_SCHEMA = vol.Schema(
    {
        vol.Required(_ID_KEY): selector.TextSelector(),
        vol.Required("entity"): selector.EntitySelector(),
        vol.Optional("default", default=0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=100, step=1, mode=_BOX)
        ),
    }
)


class ValueSubentryFlowHandler(_SubentryFlowBase):
    """Add, edit or remove one `value` subentry.

    `entity` has no domain filter: a `values:` entry reads a helper's numeric
    state (`engine._resolve_value`), and which domain that helper lives in
    (`input_number`, `number`, a plain `sensor`, ...) is a choice this
    project leaves to the house, not something this form should narrow.
    """

    subentry_type = VALUE
    id_key = _ID_KEY

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Return the static value schema; it needs nothing from `entry`."""
        return _VALUE_SCHEMA


class ZoneSubentryFlowHandler(_SubentryFlowBase):
    """Add, edit or remove one `zone` subentry.

    `members` is a multi-select over the blind entities already configured
    (blind subentries' own `entity` field), per the task brief -- not a free
    `EntitySelector`, so a zone can never reference a blind that does not
    exist as a `blind` subentry. It does *not* exclude blinds another zone
    already claims: `validate()`'s `blind_in_two_zones` (via
    `_blocking_errors`) is what catches that, reactively, after submit --
    see the task report for why that tradeoff was chosen over filtering the
    options proactively.
    """

    subentry_type = ZONE
    id_key = _ID_KEY

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Build the members multi-select from the entry's currently configured blinds."""
        blind_entities = sorted(
            {
                sub.data["entity"]
                for sub in entry.subentries.values()
                if sub.subentry_type == BLIND and "entity" in sub.data
            }
        )
        return vol.Schema(
            {
                vol.Required(_ID_KEY): selector.TextSelector(),
                vol.Optional("members", default=list): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=blind_entities, multiple=True, sort=True)
                ),
                vol.Optional("occupants", default=list): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                ),
            }
        )


# ---------------------------------------------------------------------------
# `condition`: a name plus the native HA condition selector.
#
# The selector's own dialect is a superset of ours by construction (see the
# task brief / `conditions.py`'s module docstring: our language *is* HA
# condition syntax, plus three kinds of our own) and was measured directly
# against `homeassistant==2026.8.0` to confirm `ConditionSelector` accepts
# all three (`sun_hits_target`, `event_targets_zone`, `ref`) without
# rejecting them as unknown -- HA's condition schema validates an unrecognised
# `condition:` value with `extra=ALLOW_EXTRA`, not a closed enum, precisely
# because condition platforms are pluggable. So no fallback text-and-template
# field is needed; see the task brief (finding 1) for the measurement this
# reuses instead of re-deriving, and this task's own report for the further
# selector quirks (entity-id coercion, `numeric_state`'s missing `default`)
# found while building on top of it -- and this fix pass's own report for how
# that second one is now bridged instead of merely documented as a gap.
# ---------------------------------------------------------------------------

_CONDITION_FIELD = "condition"

# `ConditionSelector` always coerces to a *list*, even for one condition
# chosen -- `cv.CONDITIONS_SCHEMA = vol.All(ensure_list, [CONDITION_SCHEMA])`.
# `vol.Length(min=1)` rejects an emptied-out selection at the schema layer,
# before it could ever become a named condition with no body at all.
#
# `_NUMERIC_STATE_DEFAULT_FIELD` sits *outside* the selector, not inside it:
# HA's own `NUMERIC_STATE_CONDITION_SCHEMA` has no `default` key and rejects
# one as an unrecognised extra (unlike the `ALLOW_EXTRA` path this project's
# own three custom kinds get -- `numeric_state` is a kind HA already knows,
# so its schema is closed), so a value typed into the selector's own body
# would never survive HA's own schema coercion to reach `_to_data` at all.
# This field is a plain `NumberSelector`, coerced by *this* form's schema,
# not HA's condition one -- `_to_data` merges it into the flattened body's
# `default` key after both schemas have already run, exactly where the task
# brief's finding 2 points out `_to_data` already sits: downstream of HA's
# validation, upstream of `config_store`'s. Scoped deliberately to a single
# top-level `numeric_state` node (`body.get("condition") == "numeric_state"`
# in `_to_data`, not a walk of the whole tree) -- the real need this solves
# (the live house's three `numeric_state` conditions, each its own single-
# node `condition` subentry) is exactly that shape; a `numeric_state` nested
# inside an `and`/`or`/`not` built in one selector still has no field to
# supply its `default` from and still blocks with `bad_condition_shape`,
# same as before this field existed. See this fix pass's own report for why
# that boundary was chosen over a dynamic per-node form.
_NUMERIC_STATE_DEFAULT_FIELD = "numeric_state_default"

_CONDITION_SCHEMA = vol.Schema(
    {
        vol.Required(_ID_KEY): selector.TextSelector(),
        vol.Required(_CONDITION_FIELD): vol.All(selector.ConditionSelector(), vol.Length(min=1)),
        vol.Optional(_NUMERIC_STATE_DEFAULT_FIELD): selector.NumberSelector(
            selector.NumberSelectorConfig(mode=_BOX, step="any")
        ),
    }
)


def _normalize_entity_id(cond: dict[str, Any]) -> dict[str, Any]:
    """Undo the native selector's list coercion of a `state`/`numeric_state` `entity_id`.

    Measured directly against `homeassistant==2026.8.0`: `ConditionSelector`
    runs a `state`/`numeric_state` condition through `cv.entity_ids_or_uuids`,
    which *always* normalises `entity_id` to a list, even for one entity
    picked -- `{"entity_id": "input_boolean.a"}` in becomes
    `{"entity_id": ["input_boolean.a"]}` out. `world.state`/`world.attribute`
    key their snapshot by a bare entity id string; a list is unhashable, so a
    saved condition carrying one straight through would not misbehave, it
    would raise `TypeError: unhashable type: 'list'` the first time
    `evaluate_condition` actually ran it -- a crash `validate()` cannot catch
    (it checks a condition's required keys, never a value's type). One
    selected entity unwraps back to the plain string every other condition in
    this project already uses (see `tests/ha/conftest.py`'s own fixture);
    more than one is expanded into an explicit `and`/`or` of one condition
    per entity, mirroring what HA's own `match: all`/`any` would otherwise
    have meant, since this evaluator has no multi-entity form to hand a list
    to.
    """
    entity_id = cond.get("entity_id")
    if not isinstance(entity_id, list):
        return cond
    # `match` only means anything alongside a list of several entities --
    # stripped here in both branches so a single-entity result matches what
    # every hand-written YAML fixture in this project already looks like.
    shared = {k: v for k, v in cond.items() if k != "match"}
    if len(entity_id) == 1:
        return {**shared, "entity_id": entity_id[0]}
    combinator = "or" if cond.get("match") == "any" else "and"
    per_entity = [{**shared, "entity_id": eid} for eid in entity_id]
    return {"condition": combinator, "conditions": per_entity}


def _normalize_condition_tree(node: Any) -> Any:
    """Recursively apply `_normalize_entity_id` to every `state`/`numeric_state` node.

    Walks the same `conditions` nesting `config_schema.walk_condition_nodes`
    does, so an `and`/`or`/`not` block built in the selector's UI is fixed up
    exactly as thoroughly as one bare condition would be -- the entity-id
    bug this exists for is not a top-level-only artefact, it reproduces at
    every depth the selector lets a user nest to.
    """
    if isinstance(node, list):
        return [_normalize_condition_tree(child) for child in node]
    if not isinstance(node, dict):
        return node
    node = dict(node)
    if "conditions" in node:
        node["conditions"] = [_normalize_condition_tree(child) for child in node["conditions"]]
    if node.get("condition") in ("state", "numeric_state"):
        node = _normalize_entity_id(node)
    return node


def _flatten_condition_list(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a `ConditionSelector` list into the single-dict body `config_store` expects.

    `config_store._build_conditions` builds a named condition's raw body by
    merging every key of its subentry `data` *except* `id` straight into one
    dict (`{k: v for k, v in data.items() if k != _ID_KEY}`) and handing that
    dict to `config_schema._parse_condition` unchanged -- so that body must
    already look like one condition node (`{"condition": "state", ...}`), not
    a node wrapped one level deeper under some field name of this form's
    choosing. One selected condition becomes that condition's own dict,
    verbatim; more than one becomes an explicit `and` -- the same meaning a
    bare list already has wherever `evaluate_condition`/`_parse_condition`
    see one directly (a `mode`'s `when`, a `rule`'s `if`), just spelled so it
    survives being merged into a dict instead of standing on its own.
    """
    if len(conditions) == 1:
        return dict(conditions[0])
    return {"condition": "and", "conditions": list(conditions)}


def _unflatten_condition_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Invert `_flatten_condition_list`, to prefill the selector on a reconfigure.

    An `and` body holding nothing but `conditions` came from more than one
    selected condition -- or, indistinguishably, from a single hand-authored
    `and` block, which is not a bug: an `and` of an `and` evaluates exactly
    the same as the flattened list it would round-trip to, so nothing about
    what the config *does* is lost either way, only how many rows the editor
    happens to show it as. Anything else was a single selected condition.
    """
    if set(body) == {"condition", "conditions"} and body["condition"] == "and":
        return list(body["conditions"])
    return [body]


class ConditionSubentryFlowHandler(_SubentryFlowBase):
    """Add, edit or remove one named `condition` subentry.

    A named condition exists to be referenced -- by `!ref` in YAML, or by the
    already-parsed `{"condition": "ref", "name": <this id>}` shape from a
    `mode`'s `when`, a `rule`'s `if`, or another condition's own body (see
    `ModeSubentryFlowHandler._to_data`'s own docstring for why the *parsed*
    shape, not `config_store.py`'s `{"ref": ...}` subentry-side marker, is
    what this project's own flows write). This form itself has no
    ref-picking convenience of its own (unlike `mode`, below): a condition's
    body is built entirely from the native selector, matching the task
    brief's own description of this type ("name plus the native HA condition
    selector") -- composing one named condition from another is still
    possible by typing `condition: ref` into the selector's own YAML-editing
    mode, the same shape this form's own output already uses.
    """

    subentry_type = CONDITION
    id_key = _ID_KEY

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Return the static condition schema; it needs nothing from `entry`."""
        return _CONDITION_SCHEMA

    def _to_data(self, entry: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        """Normalize and flatten the selector's list into `config_store`'s expected body.

        `entry` is unused here (this type needs nothing from it) -- part of
        the uniform `_to_data(entry, user_input)` signature every subclass
        shares; see `_SubentryFlowBase._to_data`'s own docstring for why.

        `_NUMERIC_STATE_DEFAULT_FIELD` is merged in as the flattened body's
        `default` key, but only when the flattened body's own top-level
        `condition` is `numeric_state` -- see that field's own comment for
        why only the top-level, single-node shape is handled. If the field
        was left empty, `default` is simply never added: `_to_data` never
        invents a fallback silently, it hands `_blocking_errors` a body
        `validate()`'s `bad_condition_shape` (`_REQUIRED_CONDITION_KEYS`
        already requires `default` for `numeric_state`) will correctly block
        on -- the loud failure `docs/rationale.md`'s "Why `numeric_state`
        requires an explicit `default`" describes is preserved exactly, not
        routed around.
        """
        normalized = _normalize_condition_tree(user_input[_CONDITION_FIELD])
        body = _flatten_condition_list(normalized)
        numeric_default = user_input.get(_NUMERIC_STATE_DEFAULT_FIELD)
        if body.get("condition") == "numeric_state" and numeric_default is not None:
            body = {**body, "default": numeric_default}
        return {_ID_KEY: user_input[_ID_KEY], **body}

    def _to_form_values(self, data: dict[str, Any]) -> dict[str, Any]:
        """Unflatten a saved condition's body back into the selector's list shape.

        A top-level `numeric_state`'s `default` is pulled back out into
        `_NUMERIC_STATE_DEFAULT_FIELD` rather than left in the body handed to
        the selector -- HA's own `numeric_state` schema has no `default` key
        (see `_NUMERIC_STATE_DEFAULT_FIELD`'s own comment) and would reject a
        prefill that included it.
        """
        body = {k: v for k, v in data.items() if k != _ID_KEY}
        numeric_default = (
            body.pop("default", None) if body.get("condition") == "numeric_state" else None
        )
        return {
            _ID_KEY: data[_ID_KEY],
            _CONDITION_FIELD: _unflatten_condition_body(body),
            _NUMERIC_STATE_DEFAULT_FIELD: numeric_default,
        }


# ---------------------------------------------------------------------------
# `mode`: id, a condition (named or inline), and an `order` deciding which
# mode wins first -- see `config_store.py`'s own "Ordering" docstring section
# for why `order` exists at all (subentries are an unordered flat list;
# first-match-wins mode resolution needs a real order, not iteration order).
# ---------------------------------------------------------------------------

_MODE_ORDER_FIELD = "order"
_MODE_REF_FIELD = "condition_ref"
_MODE_INLINE_FIELD = "when"


class ModeSubentryFlowHandler(_SubentryFlowBase):
    """Add, edit or remove one `mode` subentry.

    `when` (the condition-under-`config_store`'s own key, see `_to_data`) may
    be a reference to an existing named `condition` subentry, or built inline
    with the native selector -- the task brief's own "podmienka (odkaz na
    pomenovanú alebo inline)". Those are two separate, both-optional form
    fields (`condition_ref`/`when`) rather than one field wearing two hats,
    because a `SelectSelector` of existing condition ids and a
    `ConditionSelector` are different widgets with no shared shape to merge
    them into before submission. `_local_problems` below rejects a submission
    that fills both -- silently preferring one over the other would hide a
    mistake instead of reporting it. Neither filled at all is not an error:
    `data` then carries no `when` key, exactly the shape a fallback mode
    (`config_store._build_modes`'s `data.get("when")` -- `None`) needs, and
    whether *some* mode is missing entirely is `validate()`'s
    `no_fallback_mode`/`fallback_mode_not_last`, not this form's job to
    pre-empt with a required field.

    The last mode by `order` must be the fallback (no condition) -- that is
    exactly `no_fallback_mode`/`fallback_mode_not_last`, already enforced by
    `_blocking_errors` via `_CODE_OWNERS`. A user builds a mode set correctly
    by giving the fallback the highest `order` (added first, or at any point
    before every other mode gets a lower `order` than it), rather than by any
    special-casing here -- there is no cross-type dependency the way
    `blind`/`zone` had, only careful `order` values within `mode` itself.
    """

    subentry_type = MODE
    id_key = _ID_KEY

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Build the named-condition choice from the entry's currently configured conditions."""
        condition_ids = sorted(
            sub.data[_ID_KEY]
            for sub in entry.subentries.values()
            if sub.subentry_type == CONDITION and _ID_KEY in sub.data
        )
        return vol.Schema(
            {
                vol.Required(_ID_KEY): selector.TextSelector(),
                vol.Required(_MODE_ORDER_FIELD): selector.NumberSelector(
                    selector.NumberSelectorConfig(step=1, mode=_BOX)
                ),
                vol.Optional(_MODE_REF_FIELD): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=condition_ids, sort=True)
                ),
                vol.Optional(_MODE_INLINE_FIELD, default=list): selector.ConditionSelector(),
            }
        )

    def _local_problems(self, user_input: dict[str, Any]) -> list[str]:
        """Reject a submission that fills both the named and the inline condition."""
        if user_input.get(_MODE_REF_FIELD) and user_input.get(_MODE_INLINE_FIELD):
            return ["mode: choose either a named condition or an inline condition, not both"]
        return []

    def _to_data(self, entry: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        """Fold `condition_ref`/`when` into the single `when` key `config_store` reads.

        `entry` is unused here -- see `_SubentryFlowBase._to_data`'s
        docstring for why every subclass shares this signature regardless.

        A bare list is what `config_store._build_modes` already expects
        directly (`_parse_condition`/`evaluate_condition` both treat a
        top-level list as `and`), so the inline selector's own output needs
        no flattening the way `condition`'s does. `condition_ref` is written
        as `{"condition": "ref", "name": ...}` -- the *parsed* shape
        `config_schema._parse_condition` already produces from a `RefTag` --
        deliberately, not as this project's own `{"ref": ...}` subentry-side
        marker: that marker is unwrapped by `config_store._to_reftag` into a
        real `RefTag`, and `_parse_condition` checks a `RefTag`'s target
        eagerly, raising `ConfigError` if the name is missing -- which
        `_blocking_errors` cannot help but treat as unconditional (it is
        caught before `config_from_subentries` even returns a `Config` for
        `validate()` to look at), so it would block *any* subentry save, not
        just a `mode`'s, the moment some unrelated `condition` this mode
        refs to gets deleted. Writing the already-parsed shape instead skips
        that eager check entirely -- `_parse_condition`'s dict branch passes
        it through unchanged -- deferring "does the name exist" to
        `validate()`'s `unknown_condition_ref`, a `Problem` `_blocks_on`
        correctly scopes to the specific `condition`/`mode`/`rule` subentry
        that actually holds the dangling ref, via `Problem.owners` -- not
        every subentry of those three types. See the task report's "dangling
        ref" section for the failure this avoids.
        It still needs `_normalize_condition_tree`: see that function's own
        docstring for why a `state`/`numeric_state` `entity_id` from the
        selector cannot be saved as-is.
        """
        data = {
            _ID_KEY: user_input[_ID_KEY],
            _MODE_ORDER_FIELD: int(user_input[_MODE_ORDER_FIELD]),
        }
        ref = user_input.get(_MODE_REF_FIELD)
        inline = _normalize_condition_tree(user_input.get(_MODE_INLINE_FIELD))
        if ref:
            data[_MODE_INLINE_FIELD] = {"condition": "ref", "name": ref}
        elif inline:
            data[_MODE_INLINE_FIELD] = inline
        return data

    def _to_form_values(self, data: dict[str, Any]) -> dict[str, Any]:
        """Invert `_to_data`: split a saved `when` back into `condition_ref`/`when`."""
        when = data.get(_MODE_INLINE_FIELD)
        is_ref = isinstance(when, dict) and when.get("condition") == "ref" and "name" in when
        ref = when["name"] if is_ref else None
        if ref is not None or when is None:
            inline: Any = []
        elif isinstance(when, list):
            inline = when
        else:
            # A `when` seeded outside this flow (by hand, or a migration) as
            # one bare condition dict rather than a list -- still valid input
            # to `config_store`, so still worth prefilling correctly here.
            inline = [when]
        return {
            _ID_KEY: data[_ID_KEY],
            _MODE_ORDER_FIELD: data[_MODE_ORDER_FIELD],
            _MODE_REF_FIELD: ref,
            _MODE_INLINE_FIELD: inline,
        }


# ---------------------------------------------------------------------------
# `rule`: one row of a first-match-wins list, filed under a `(mode, zone)`
# pair and placed within it by `order`.
#
# The one type where order is meaning rather than presentation:
# `engine._apply_rules` returns the *first* rule whose events and condition
# match, so moving a rule past another changes what the house does. Home
# Assistant subentries are an unordered flat list, which is why `order` is an
# explicit field rather than something inferred from the UI -- see
# `config_store.py`'s own "Ordering" docstring section.
# ---------------------------------------------------------------------------

_RULE_MODE_FIELD = "mode"
_RULE_ZONE_FIELD = "zone"
_RULE_ORDER_FIELD = "order"
_RULE_REF_FIELD = "if_ref"
_RULE_INLINE_FIELD = "if"
_RULE_POSITION_FIELD = "position"
_RULE_TILT_FIELD = "tilt"
_RULE_EVENTS_FIELD = "events"
_RULE_NAME_FIELD = "name"

# The gap left between one rule's `order` and the next when appending, so a
# rule can later be slipped between two existing ones without renumbering
# either -- the reason to default to "highest + 10" rather than "highest + 1".
_ORDER_GAP = 10

# What a user picks (or types) on an action axis. `config_schema._parse_axis`
# accepts three things per axis -- `"keep"`, an integer 0..100, and a
# `RefTag` naming a `values:` entry -- and dropping any of them would make a
# large share of the live configuration unexpressible through the UI
# (`fixtures/dom_peter.yaml` uses all three, often in the same rule). One
# combo box per axis carries all three instead of three fields per axis:
# `"keep"` and every configured value id are offered as options, and
# `custom_value` lets a number be typed. `_axis_to_data` decides which of the
# three a submission meant.
_AXIS_KEEP = "keep"


def _configured_ids(entry: Any, subentry_type: str) -> list[str]:
    """Sorted `id` of every subentry of `subentry_type` currently on `entry`."""
    return sorted(
        sub.data[_ID_KEY]
        for sub in entry.subentries.values()
        if sub.subentry_type == subentry_type and _ID_KEY in sub.data
    )


def _rule_pick_schema(entry: Any) -> vol.Schema:
    """Step one of adding a rule: the `(mode, zone)` list it will join.

    A free function, not inlined into `RuleSubentryFlowHandler.async_step_user`
    below, so `options_flow.py`'s own rule-add step can render the identical
    form -- see that module's docstring for why the phase 5 menu must read
    this rather than grow a second copy of it.
    """
    return vol.Schema(
        {
            vol.Required(_RULE_MODE_FIELD): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_configured_ids(entry, MODE), sort=True)
            ),
            vol.Required(_RULE_ZONE_FIELD): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_configured_ids(entry, ZONE), sort=True)
            ),
        }
    )


def _axis_to_data(raw: Any, value_ids: list[str]) -> Any:
    """Turn one axis combo box submission into what `config_store` reads.

    A configured value id beats a numeric reading of the same text, because
    the id was offered in the dropdown and picking it from there has to mean
    what the list said it meant. (Reachable only by naming a value something
    like `"40"`; `_reject_dot` does not stop that.)

    Anything that is neither `"keep"`, a known value id, nor a number is
    passed through untouched, so `config_schema._parse_axis` raises the same
    `ConfigError` it would for hand-written YAML and `_blocking_errors`
    surfaces it -- rather than this function quietly picking a position for
    a blind on the user's behalf.
    """
    if raw == _AXIS_KEEP:
        return _AXIS_KEEP
    if raw in value_ids:
        return {"ref": raw}
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


def _axis_to_form(value: Any) -> str:
    """Invert `_axis_to_data`, for prefilling a reconfigure form and for titles."""
    if isinstance(value, dict) and set(value) == {"ref"}:
        return str(value["ref"])
    if value is None:
        return _AXIS_KEEP
    return str(value)


def _next_order(entry: Any, mode: str, zone: str) -> int:
    """The `order` that appends to the end of this `(mode, zone)` list.

    Reads `entry.subentries` rather than a built `Config`, which no longer
    carries `order` at all. A malformed existing `order` is skipped instead
    of raised on: this only computes a suggestion for a form field, and a
    rule broken badly enough to fail `int()` is a problem
    `config_from_subentries` reports properly on save.
    """
    orders = []
    for sub in entry.subentries.values():
        if sub.subentry_type != RULE:
            continue
        if sub.data.get(_RULE_MODE_FIELD) != mode or sub.data.get(_RULE_ZONE_FIELD) != zone:
            continue
        try:
            orders.append(int(sub.data[_RULE_ORDER_FIELD]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(orders) + _ORDER_GAP if orders else 0


class RuleSubentryFlowHandler(_SubentryFlowBase):
    """Add, edit or remove one `rule` subentry.

    Adding is two steps, for one reason: the brief's "default `order` =
    highest existing in that pair + 10, so appending needs no thought" cannot
    be honoured by a single form, because the default depends on the
    `(mode, zone)` pair the same form is still asking for. Step one picks the
    pair; step two is the whole rule, with `order` already filled in and both
    action axes defaulted to `keep`. Both fields stay editable in step two
    (they must, or an edit could never move a rule between pairs) -- changing
    the pair there leaves the suggested `order` computed for the old one,
    which is visible in the field and, if it collides, blocked by
    `duplicate_rule_order` rather than silently applied.

    `mode` and `zone` are `SelectSelector`s over what is actually configured,
    never free text, so a rule cannot be filed under a pair that does not
    exist. That also means modes and zones must exist before any rule can be
    added, which is not a deadlock of the kind
    `test_full_build_up_sequence_a_human_would_perform` guards against: a
    rule is *about* a mode and a zone, so there is nothing to add first and
    fix later, and no save is being refused -- the form simply has nothing to
    offer yet.

    The condition is `if_ref` (a named `condition` subentry) or `if` (built
    inline with the native selector), exactly as `mode` splits `condition_ref`
    from `when` and for the same reason -- see `ModeSubentryFlowHandler`. That
    split is also this flow's whole answer to `numeric_state`: HA's own
    `numeric_state` schema rejects the `default` key this project requires, so
    the `condition` flow carries a `numeric_state_default` field beside its
    selector, and a rule reaches a `numeric_state` by naming such a condition
    rather than by growing a second copy of that workaround. An inline
    `numeric_state` typed directly into `if` still blocks with
    `bad_condition_shape`, loudly, the same as `mode`'s inline `when` does.
    """

    subentry_type = RULE
    # A rule has no id field: its identity is `(mode, zone, order)`, so the
    # `_duplicate_errors` uniqueness check does not apply to it -- two rules
    # in one pair claiming one `order` is `duplicate_rule_order`'s job, and
    # unlike a blind entity or a zone id, a collision there does not silently
    # overwrite anything, it is caught before the tuple is built.
    id_key = None

    # Set by step one, read by `_initial_values` for step two's prefill.
    # `None` on a reconfigure flow, which never runs step one at all.
    _pick: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Step one of adding a rule: which `(mode, zone)` list does it belong to."""
        entry = self._get_entry()
        if user_input is not None:
            self._pick = dict(user_input)
            return await self._step(None, step_id="rule")

        return self.async_show_form(step_id="user", data_schema=_rule_pick_schema(entry))

    async def async_step_rule(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Step two of adding a rule: the rule itself."""
        return await self._step(user_input, step_id="rule")

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Build the form from what the entry currently has: modes, zones, conditions, values."""
        axis_options = [_AXIS_KEEP, *_configured_ids(entry, VALUE)]

        def axis() -> selector.SelectSelector:
            # `sort=False`: `keep` belongs at the top as the do-nothing
            # default, not alphabetised in among the value ids.
            return selector.SelectSelector(
                selector.SelectSelectorConfig(options=axis_options, custom_value=True, sort=False)
            )

        return vol.Schema(
            {
                vol.Required(_RULE_MODE_FIELD): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=_configured_ids(entry, MODE), sort=True)
                ),
                vol.Required(_RULE_ZONE_FIELD): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=_configured_ids(entry, ZONE), sort=True)
                ),
                vol.Required(_RULE_ORDER_FIELD): selector.NumberSelector(
                    selector.NumberSelectorConfig(step=1, mode=_BOX)
                ),
                vol.Optional(_RULE_REF_FIELD): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_configured_ids(entry, CONDITION), sort=True
                    )
                ),
                vol.Optional(_RULE_INLINE_FIELD, default=list): selector.ConditionSelector(),
                vol.Required(_RULE_POSITION_FIELD, default=_AXIS_KEEP): axis(),
                vol.Required(_RULE_TILT_FIELD, default=_AXIS_KEEP): axis(),
                vol.Optional(_RULE_EVENTS_FIELD, default=list): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[EVENT_ARRIVAL, EVENT_STATE_CHANGE], multiple=True, sort=True
                    )
                ),
                vol.Optional(_RULE_NAME_FIELD, default=""): selector.TextSelector(),
            }
        )

    def _initial_values(self, entry: Any) -> dict[str, Any] | None:
        """Prefill step two from step one's pick, with `order` appending to that list."""
        if self._pick is None:
            return None
        mode = self._pick[_RULE_MODE_FIELD]
        zone = self._pick[_RULE_ZONE_FIELD]
        return {
            _RULE_MODE_FIELD: mode,
            _RULE_ZONE_FIELD: zone,
            _RULE_ORDER_FIELD: _next_order(entry, mode, zone),
            _RULE_POSITION_FIELD: _AXIS_KEEP,
            _RULE_TILT_FIELD: _AXIS_KEEP,
        }

    def _local_problems(self, user_input: dict[str, Any]) -> list[str]:
        """Reject a submission that fills both the named and the inline condition."""
        if user_input.get(_RULE_REF_FIELD) and user_input.get(_RULE_INLINE_FIELD):
            return ["rule: choose either a named condition or an inline condition, not both"]
        return []

    def _to_data(self, entry: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        """Reshape the submission into the keys `config_store._grouped_rules`/`_rule_body` read.

        Takes `entry` explicitly, unlike every other override, because this
        is the one type that reads it (`_configured_ids(entry, VALUE)`); see
        `_SubentryFlowBase._to_data`'s own docstring for why the signature is
        uniform across every subclass regardless.

        `then` is always written, because `config_schema._parse_rule` requires
        it; an axis left at `keep` is written as `"keep"` rather than omitted,
        so a saved rule reads the same way the YAML fixtures do.

        `events` and `name` are omitted when empty rather than written as
        `[]`/`""`. For `name` that is cosmetic (`_parse_rule` defaults it to
        `""` either way), but for `events` it is the difference between two
        behaviours: `Rule.events = None` means "any event", while an empty
        `frozenset` means `engine._apply_rules`'s `world.event.kind not in
        rule.events` is true for every event and the rule can never fire.

        `if_ref` is written as `{"condition": "ref", "name": ...}` -- the
        already-parsed shape -- not as `{"ref": ...}`; see
        `ModeSubentryFlowHandler._to_data` for the full reasoning. In short,
        `config_store._to_reftag` would turn the marker into a `RefTag` whose
        target `_parse_condition` checks eagerly, raising `ConfigError` and
        so blocking every save of every type the moment the named condition
        is deleted, instead of one attributable `unknown_condition_ref`.
        """
        value_ids = _configured_ids(entry, VALUE)
        data: dict[str, Any] = {
            _RULE_MODE_FIELD: user_input[_RULE_MODE_FIELD],
            _RULE_ZONE_FIELD: user_input[_RULE_ZONE_FIELD],
            _RULE_ORDER_FIELD: int(user_input[_RULE_ORDER_FIELD]),
            "then": {
                _RULE_POSITION_FIELD: _axis_to_data(
                    user_input.get(_RULE_POSITION_FIELD, _AXIS_KEEP), value_ids
                ),
                _RULE_TILT_FIELD: _axis_to_data(
                    user_input.get(_RULE_TILT_FIELD, _AXIS_KEEP), value_ids
                ),
            },
        }

        ref = user_input.get(_RULE_REF_FIELD)
        inline = _normalize_condition_tree(user_input.get(_RULE_INLINE_FIELD))
        if ref:
            data[_RULE_INLINE_FIELD] = {"condition": "ref", "name": ref}
        elif inline:
            data[_RULE_INLINE_FIELD] = inline

        events = user_input.get(_RULE_EVENTS_FIELD)
        if events:
            data[_RULE_EVENTS_FIELD] = list(events)
        name = user_input.get(_RULE_NAME_FIELD)
        if name:
            data[_RULE_NAME_FIELD] = name
        return data

    def _to_form_values(self, data: dict[str, Any]) -> dict[str, Any]:
        """Invert `_to_data`, to prefill a reconfigure form."""
        when = data.get(_RULE_INLINE_FIELD)
        is_ref = isinstance(when, dict) and when.get("condition") == "ref" and "name" in when
        if is_ref or when is None:
            inline: Any = []
        elif isinstance(when, list):
            inline = when
        else:
            # A single bare condition dict -- the shape a hand-seeded or
            # migrated subentry can carry, still valid input to
            # `config_store`, so still worth prefilling correctly.
            inline = [when]

        then = data.get("then") or {}
        return {
            _RULE_MODE_FIELD: data.get(_RULE_MODE_FIELD),
            _RULE_ZONE_FIELD: data.get(_RULE_ZONE_FIELD),
            _RULE_ORDER_FIELD: data.get(_RULE_ORDER_FIELD),
            _RULE_REF_FIELD: when["name"] if is_ref else None,
            _RULE_INLINE_FIELD: inline,
            _RULE_POSITION_FIELD: _axis_to_form(then.get(_RULE_POSITION_FIELD)),
            _RULE_TILT_FIELD: _axis_to_form(then.get(_RULE_TILT_FIELD)),
            _RULE_EVENTS_FIELD: list(data.get(_RULE_EVENTS_FIELD) or []),
            _RULE_NAME_FIELD: data.get(_RULE_NAME_FIELD, ""),
        }

    def _candidate_id(self, candidate: Any, subentry_id: str, data: dict[str, Any]) -> Any:
        """This rule's position in its finished `(mode, zone)` list, as `validate()` names it.

        Only ever called once `config_from_subentries(candidate)` has already
        succeeded, so `rule_owner_ids` cannot raise here -- every rule
        subentry is known to have a readable `mode`/`zone`/`order` by then.
        """
        return rule_owner_ids(candidate).get(subentry_id)

    def _title(self, data: dict[str, Any]) -> str:
        """Show order, pair and action, so the subentry list reads without opening rows.

        Ordering is this type's whole point and Home Assistant lists
        subentries by title, so the `order` leads -- a list sorted by title
        is then in very nearly the order the engine tries the rules in.
        """
        then = data.get("then") or {}
        action = (
            f"{_axis_to_form(then.get(_RULE_POSITION_FIELD))}"
            f"/{_axis_to_form(then.get(_RULE_TILT_FIELD))}"
        )
        label = f"{data[_RULE_ORDER_FIELD]} {data[_RULE_MODE_FIELD]}.{data[_RULE_ZONE_FIELD]}"
        name = data.get(_RULE_NAME_FIELD)
        if name:
            label = f"{label} {name}"
        return f"{label} -> {action}"


# Registered by subentry type. `async_get_supported_subentry_types` above
# just returns this.
SUBENTRY_FLOW_HANDLERS: dict[str, type[config_entries.ConfigSubentryFlow]] = {
    BLIND: BlindSubentryFlowHandler,
    ZONE: ZoneSubentryFlowHandler,
    VALUE: ValueSubentryFlowHandler,
    CONDITION: ConditionSubentryFlowHandler,
    MODE: ModeSubentryFlowHandler,
    RULE: RuleSubentryFlowHandler,
}
