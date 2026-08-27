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
same configuration by clicking, one `blind`/`zone`/`value`/`condition`/`mode`
row at a time, instead of hand-writing YAML. `config_store.py` is the
reviewed, tested *reader* of those subentries -- it already decides exactly
which keys it reads out of each type's `data` and which are required; these
flows exist only to *produce* data in that exact shape, never to invent a
spelling of their own. `rule` is not built yet (see
`SUBENTRY_FLOW_HANDLERS`'s own comment for how it slots in later).

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
# Subentry flows: `blind`, `zone`, `value`, `condition`, `mode`.
#
# `config_store.SUBENTRY_TYPES` also lists `rule` -- it has both ordering
# (`order`, shared with `mode`) and nested structure (an `if`/`then` body
# each of its own), so it is a separate task, not built here (see the task
# brief). Nothing here assumes it will never exist: `SUBENTRY_FLOW_HANDLERS`
# is a plain dict keyed by subentry type, so adding it later is adding one
# more entry and one more `_SubentryFlowBase` subclass, not a rewrite of
# what already works.
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
# Most codes have exactly one owner. `unknown_condition_ref`,
# `bad_condition_shape` and `circular_condition_ref` do not: a condition body
# lives, verbatim, in three different places -- a `condition` subentry's own
# fields, a `mode`'s `when`, and (once built) a `rule`'s `if` -- and any of
# those three forms can be the one that actually wrote the broken reference
# or malformed shape a `validate()` pass is complaining about. Blocking only
# one of them would let a user "fix" a `mode`'s dangling ref by editing an
# unrelated `condition`, or leave them unable to fix it at all from the form
# that actually holds it. So these three map to a *set* of owners, not a
# single one -- `_blocks_on` checks membership, not equality.
#
# A code missing from this dict has no owning subentry type *yet*: `rule`
# has no flow in this phase (see the module docstring /
# `SUBENTRY_FLOW_HANDLERS`'s own comment), so a problem only that future flow
# could fix -- rule ordering -- can never be something a
# `blind`/`zone`/`value`/`condition`/`mode` save either caused or could
# address. This needs no "which flows exist" check of its own:
# `_blocking_errors` only ever calls `_blocks_on` with the `subentry_type` of
# a flow that actually exists (there is no other caller), so a code whose
# owner has no flow yet simply never matches and is exempt for every save --
# automatically, not by a separate "is this phase" test. `unknown_rule_key`
# and `duplicate_rule_order` already name `RULE` for exactly this reason (the
# `mode`/`condition` precedent this pattern was proven on: `no_fallback_mode`
# and `fallback_mode_not_last` named `MODE` while no `mode` flow existed
# yet). The day a `rule` flow is added, this dict needs no change at all --
# `unknown_rule_key`/`duplicate_rule_order` and `rule`'s share of the three
# condition-body codes are already here, ready for it.
_CODE_OWNERS: dict[str, frozenset[str]] = {
    "zone_member_unknown": frozenset({ZONE}),
    "blind_in_two_zones": frozenset({ZONE}),
    "blind_without_zone": frozenset({ZONE}),
    "no_fallback_mode": frozenset({MODE}),
    "fallback_mode_not_last": frozenset({MODE}),
    "unknown_rule_key": frozenset({RULE}),
    "duplicate_rule_order": frozenset({RULE}),
    "unknown_condition_ref": frozenset({CONDITION, MODE, RULE}),
    "bad_condition_shape": frozenset({CONDITION, MODE, RULE}),
    "circular_condition_ref": frozenset({CONDITION, MODE, RULE}),
}


def _blocks_on(subentry_type: str, code: str) -> bool:
    """Whether saving a `subentry_type` subentry is how a user would resolve `code`.

    See `_CODE_OWNERS` for the mapping and the reasoning; this is the one
    place that reads it, so a code missing or misspelled there fails as
    "never blocks anything" -- caught by a test asserting that code *does*
    block its owning form -- rather than as a crash.
    """
    return subentry_type in _CODE_OWNERS.get(code, frozenset())


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
    `_describe_problems` above treats the YAML path -- and even then, only
    the ones `_blocks_on` says `subentry_type`'s own form can actually
    resolve; see `_CODE_OWNERS` for which those are and why the rest do not
    block a save that could not have fixed them anyway.
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

    problems = validate(config) + duplicate_rule_order_problems(candidate)
    return [
        f"{problem.code}: {problem.message}"
        for problem in problems
        if problem.severity == ERROR and _blocks_on(subentry_type, problem.code)
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

    A subclass may also override two more, both identity by default:

    - `_to_data(user_input)`: reshape a raw, schema-coerced submission into
      the exact `dict` `config_store` expects for this type's `data`.
      `blind`/`zone`/`value` submit already in that shape, so they never
      override this; `condition`/`mode` do -- see their own classes for why
      a native HA selector's output is not, quite, that shape yet.
    - `_to_form_values(data)`: the inverse, for prefilling a reconfigure form
      from a saved subentry's `data`.

    And one hook with no default, always empty:

    - `_local_problems(user_input)`: checks that must run *before*
      `_to_data` can even be called -- a submission that is ambiguous in a
      way `_to_data` would otherwise have to silently pick a winner for
      (`mode`'s "named or inline condition, not both"). Returning anything
      here skips `_to_data` and `_blocking_errors` entirely for this
      attempt, the same as a `_blocking_errors` result would.

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

    def _to_data(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Reshape a coerced submission into `config_store`'s expected `data`.

        Identity by default.
        """
        return user_input

    def _to_form_values(self, data: dict[str, Any]) -> dict[str, Any]:
        """Invert `_to_data`, to prefill a reconfigure form. Identity by default."""
        return data

    def _local_problems(self, user_input: dict[str, Any]) -> list[str]:
        """Flow-only checks that must block before `_to_data` runs at all. None by default."""
        return []

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
            problems = self._local_problems(user_input)
            data: dict[str, Any] | None = None
            if not problems:
                data = self._to_data(user_input)
                problems = _blocking_errors(
                    entry, self.subentry_type, subentry_id, self.id_key, data
                )
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
            else (self._to_form_values(subentry.data) if subentry else None)
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
# found while building on top of it.
# ---------------------------------------------------------------------------

_CONDITION_FIELD = "condition"

# `ConditionSelector` always coerces to a *list*, even for one condition
# chosen -- `cv.CONDITIONS_SCHEMA = vol.All(ensure_list, [CONDITION_SCHEMA])`.
# `vol.Length(min=1)` rejects an emptied-out selection at the schema layer,
# before it could ever become a named condition with no body at all.
_CONDITION_SCHEMA = vol.Schema(
    {
        vol.Required(_ID_KEY): selector.TextSelector(),
        vol.Required(_CONDITION_FIELD): vol.All(selector.ConditionSelector(), vol.Length(min=1)),
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

    def _to_data(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Normalize and flatten the selector's list into `config_store`'s expected body."""
        normalized = _normalize_condition_tree(user_input[_CONDITION_FIELD])
        body = _flatten_condition_list(normalized)
        return {_ID_KEY: user_input[_ID_KEY], **body}

    def _to_form_values(self, data: dict[str, Any]) -> dict[str, Any]:
        """Unflatten a saved condition's body back into the selector's list shape."""
        body = {k: v for k, v in data.items() if k != _ID_KEY}
        return {_ID_KEY: data[_ID_KEY], _CONDITION_FIELD: _unflatten_condition_body(body)}


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

    def _to_data(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Fold `condition_ref`/`when` into the single `when` key `config_store` reads.

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
        `validate()`'s `unknown_condition_ref`, a `Problem` `_CODE_OWNERS`
        correctly scopes to `condition`/`mode`/`rule` saves only. See the
        task report's "dangling ref" section for the failure this avoids.
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


# Registered by subentry type. `async_get_supported_subentry_types` above
# just returns this -- adding `rule` later means adding one more entry here
# (and one more `_SubentryFlowBase` subclass), not touching the classmethod
# that reads it.
SUBENTRY_FLOW_HANDLERS: dict[str, type[config_entries.ConfigSubentryFlow]] = {
    BLIND: BlindSubentryFlowHandler,
    ZONE: ZoneSubentryFlowHandler,
    VALUE: ValueSubentryFlowHandler,
    CONDITION: ConditionSubentryFlowHandler,
    MODE: ModeSubentryFlowHandler,
}
