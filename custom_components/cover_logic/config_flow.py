"""Config flow for Cover Logic: the YAML setup step, plus the subentry flows.

The `user` step below (unchanged from phase 2) is deliberately minimal: one
field, the configuration file path. It exists only so a config entry can be
created at all, and only ever lets one be created (`async_set_unique_id` /
`_abort_if_unique_id_configured`, checked before the form is even shown). The
file is validated on submit -- loaded and run through `validate()` -- so a
broken path never becomes a broken entry; without this, the failure would
only surface later as `async_setup_entry` raising `ConfigEntryNotReady` on
every start.

Everything below that is phase 4: subentry flows that let a user build the
same configuration by clicking, one `blind`/`zone`/`value` row at a time,
instead of hand-writing YAML. `config_store.py` is the reviewed, tested
*reader* of those subentries -- it already decides exactly which keys it
reads out of each type's `data` and which are required; these flows exist
only to *produce* data in that exact shape, never to invent a spelling of
their own. `condition`, `mode` and `rule` subentry types are not built yet
(see `SUBENTRY_FLOW_HANDLERS`'s own comment for how they slot in later).

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
    MODE,
    VALUE,
    ZONE,
    config_from_subentries,
    duplicate_rule_order_problems,
)
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

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return the subentry flows this integration supports.

        Just returns `SUBENTRY_FLOW_HANDLERS` -- see that dict's own comment
        for why adding `condition`, `mode` and `rule` later is a one-line
        addition there, not a change here.
        """
        return SUBENTRY_FLOW_HANDLERS


# ---------------------------------------------------------------------------
# Subentry flows: `blind`, `zone`, `value`.
#
# `config_store.SUBENTRY_TYPES` also lists `mode`, `condition` and `rule` --
# those three have ordering (`order`) or nested structure (a condition body,
# a rule's `if`/`then`) that `blind`/`zone`/`value` do not, so they are a
# separate task, not built here (see the task brief). Nothing here assumes
# they will never exist: `SUBENTRY_FLOW_HANDLERS` is a plain dict keyed by
# subentry type, so adding the other three later is adding three more
# entries and three more `_SubentryFlowBase` subclasses, not a rewrite of
# what already works.
# ---------------------------------------------------------------------------

# Not a real Home Assistant subentry id (those are ULIDs) -- used only as the
# dict key `_candidate_entry` gives a not-yet-created subentry so it can sit
# alongside the real ones for exactly one validation pass, then be discarded.
_NEW_SUBENTRY_ID = "__new__"


def _transient_error_codes(subentries: dict[str, Any]) -> frozenset[str]:
    """ERROR-severity `validation.Problem` codes to drop for this candidate's subentry mix.

    Neither code is exempt forever -- each is exempt only while the subentry
    type that would let a user actually *fix* it does not exist yet in
    `subentries`. The moment that type appears, the same problem stops being
    "haven't gotten there yet" and starts being something the form that just
    ran could have addressed, so the exemption must lift on that same save,
    not some later phase. Computed from `subentries` fresh on every call
    (never cached) so it reflects the *candidate* -- existing subentries plus
    whatever is about to be saved -- not just what was on disk before this
    step ran; see `_blocking_errors`, the only caller.

    - `no_fallback_mode` needs at least one `mode` subentry to mean anything:
      with zero, `config.modes` is empty in literally every configuration
      buildable in this phase (no `mode` subentry flow exists yet), so the
      code cannot distinguish "the feature to fix this does not exist yet"
      from "modes exist but none is a fallback" -- those are the same state,
      always, until a `mode` flow exists. The instant a `mode` subentry
      exists -- even the very first one, saved in this very step -- that
      question becomes real and answerable, so it is enforced from then on.
    - `blind_without_zone` cannot reuse that reasoning as-is: unlike `mode`,
      the `zone` subentry type already exists in this phase, so "no zone
      subentry flow exists yet" is not true today and this code is not
      unconditionally unanswerable the way `no_fallback_mode` is. What
      remains true is the *narrower* claim the task brief actually makes:
      a lone blind with no zone, before any zone exists at all, is the
      ordinary "just added the blind, about to go add its zone next" case.
      Once at least one zone subentry exists, an unclaimed blind is no
      longer that -- the user already has the tool to fix it (add it to
      that zone, or another) -- so this exemption lifts on a shorter fuse
      than `no_fallback_mode`'s: as soon as any `zone` subentry exists, not
      only once every blind has one.

    Both are dropped uniformly across all three flows (see
    `_blocking_errors`) rather than per-type, because both are properties of
    the *whole* config, not of any one subentry -- the same reason
    `config_store.config_from_subentries` does not special-case them either.
    """
    present_types = {sub.subentry_type for sub in subentries.values()}
    codes = set()
    if MODE not in present_types:
        codes.add("no_fallback_mode")
    if ZONE not in present_types:
        codes.add("blind_without_zone")
    return frozenset(codes)


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


def _blocking_errors(
    entry: Any, subentry_type: str, subentry_id: str, id_key: str, data: dict[str, Any]
) -> list[str]:
    """Problems that must block saving `data` as this subentry, or `[]` if none.

    Runs the pipeline `config_store`'s own docstrings describe as, together,
    one source's complete set of checks: `_duplicate_errors` (a collapse
    `validate()` cannot see, see its own docstring), then
    `config_from_subentries` (structural), then `validate()` (semantic),
    then `duplicate_rule_order_problems` (the one thing `validate()` cannot
    see once subentries collapse into a `Config` -- see that function's own
    docstring; a no-op today since no `rule` subentry flow exists yet, kept
    here so nothing has to change when one is added).

    Only `ERROR`-severity `validate()` problems block, matching how
    `_describe_problems` above treats the YAML path; codes from
    `_transient_error_codes` are dropped even at `ERROR` severity for the
    reason given on that function -- computed from this candidate's own
    subentries, not a fixed set, so the exemption tracks what is actually
    still unbuildable in this phase rather than outliving it.
    """
    candidate_id = data.get(id_key)
    duplicate = _duplicate_errors(entry, subentry_type, subentry_id, id_key, candidate_id)
    if duplicate:
        return duplicate

    candidate = _candidate_entry(entry, subentry_type, subentry_id, data)
    try:
        config = config_from_subentries(candidate)
    except ConfigError as err:
        return [str(err)]

    exempt = _transient_error_codes(candidate.subentries)
    problems = validate(config) + duplicate_rule_order_problems(candidate)
    return [
        f"{problem.code}: {problem.message}"
        for problem in problems
        if problem.severity == ERROR and problem.code not in exempt
    ]


class _SubentryFlowBase(config_entries.ConfigSubentryFlow):
    """Shared add/edit/validate machinery for the `blind`, `zone` and `value` flows.

    A subclass sets two class attributes and one method:

    - `subentry_type`: one of `config_store.BLIND`/`ZONE`/`VALUE`.
    - `id_key`: the field in that type's `data` that must be unique among
      its own type (`"entity"` for a blind, `"id"` for a zone or value) --
      see `_duplicate_errors`.
    - `_build_schema(entry)`: the `vol.Schema` for this type's form, given
      the entry (so `zone`'s member multi-select can read the currently
      configured blinds off it).

    Removal needs no code here at all: a subentry is deleted by Home
    Assistant's own built-in subentry UI, which never calls into this class
    -- nothing here holds a reference to a subentry that would need
    releasing first.
    """

    subentry_type: str
    id_key: str

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Return this type's form schema. Overridden per subclass."""
        raise NotImplementedError

    def _title(self, data: dict[str, Any]) -> str:
        """Return the subentry's display title: the value at `id_key`."""
        return str(data[self.id_key])

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
            problems = _blocking_errors(
                entry, self.subentry_type, subentry_id, self.id_key, user_input
            )
            if not problems:
                title = self._title(user_input)
                if subentry is None:
                    return self.async_create_entry(title=title, data=user_input)
                return self.async_update_and_abort(entry, subentry, title=title, data=user_input)

            errors["base"] = "invalid_config"
            description_placeholders = {"error_detail": "; ".join(problems)}
            _LOGGER.debug(
                "cover_logic %s subentry %s rejected: %s", self.subentry_type, subentry_id, problems
            )

        current = user_input if user_input is not None else (subentry.data if subentry else None)
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


# Registered by subentry type. `async_get_supported_subentry_types` above
# just returns this -- adding `condition`, `mode` and `rule` later means
# adding three more entries here (and three more `_SubentryFlowBase`
# subclasses), not touching the classmethod that reads it.
SUBENTRY_FLOW_HANDLERS: dict[str, type[config_entries.ConfigSubentryFlow]] = {
    BLIND: BlindSubentryFlowHandler,
    ZONE: ZoneSubentryFlowHandler,
    VALUE: ValueSubentryFlowHandler,
}
