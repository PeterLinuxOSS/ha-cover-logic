"""Shared schema, data-conversion and validation for the six subentry types.

**Why this module exists.** Phase 4 gave every configuration type this
integration knows about (`blind`, `zone`, `value`, `condition`, `mode`,
`rule`) its own `ConfigSubentryFlow` handler -- defined once here, and
registered with Home Assistant's own subentry flow manager by
`config_flow.py` (`CoverLogicConfigFlow.async_get_supported_subentry_types`
returns `SUBENTRY_FLOW_HANDLERS`, below). Phase 5 opened a second door onto
the same six types -- the options-flow main menu in `options_flow.py` --
under an explicit "one owner, two doors" requirement: the menu must drive
the *same* schema/data-conversion/validation each subentry flow already has,
never a second copy that could silently disagree with it. That requirement
exists because a duplicated sort already cost this project an Important
review finding once, invisible to the 92,160-scenario migration gate
because the gate never looks at this UI layer at all -- see
`config_store.py`'s own "One grouping, not two" section for that incident.

**Why these classes do not live in `config_flow.py`.** They used to.
`options_flow.py` imported `SUBENTRY_FLOW_HANDLERS` and its neighbours from
`config_flow` at module level, and `config_flow.py`'s own
`async_get_options_flow` needed `options_flow.CoverLogicOptionsFlow` right
back to hand out the options flow instance -- a genuine cycle, at the time
worked around by deferring one side of it to call time. A deferred import
only hides a cycle; it does not remove it, and it leaves behind a comment
explaining why the deferral is there instead of just being unnecessary. The
structural fix is this module: the pieces both doors actually share --
`_build_schema`, `_to_data`, `_to_form_values`, `_local_problems`,
`_candidate_id`, `_title`, `_blocking_errors`, one method group per
subentry-type class -- belong to neither `config_flow.py` nor
`options_flow.py`, so they live here instead, and both of those modules
import *down* into this one. Neither of them imports the other any more.

**What lets a bare instance drive that shared group.** Every method in it
needs nothing from `self` beyond the `entry` it is explicitly handed --
`_to_data` was the one exception (`RuleSubentryFlowHandler` used to reach
for `self._get_entry()`), fixed while landing the options-flow menu so the
whole group could be called from a bare instance of the class, entirely
outside any running `ConfigSubentryFlow` registration. `config_flow.py`
uses these classes the ordinary way, as real subentry flow handlers Home
Assistant's own manager instantiates and drives through its own step
machinery (`async_step_user`/`async_step_reconfigure`, both defined on
`_SubentryFlowBase` below). `options_flow.py` instead builds a bare
instance of whichever class owns the active menu section's type
(`SUBENTRY_FLOW_HANDLERS[subentry_type]()`) and calls that shared group
directly -- see `options_flow._render_type_form`'s own docstring for the
one thing that legitimately differs between the two doors (what "save"
does once a submission validates) and so is not, and should not be, shared.

Same Home Assistant layer as `config_flow.py` and `options_flow.py`:
imports `homeassistant` unconditionally, and is discovered only by Home
Assistant's own config/subentry flow machinery, or -- behind
`pytest.importorskip("homeassistant")` -- by `tests/ha/`.
"""

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.helpers import selector
import voluptuous as vol

from .config_schema import ConfigError
from .config_store import (
    _ID_KEY,
    BLIND,
    CONDITION,
    GUARD,
    MANUAL_DETECTION,
    MODE,
    RULE,
    VALUE,
    ZONE,
    config_from_subentries,
    duplicate_guard_order_problems,
    duplicate_rule_order_problems,
    guard_owner_ids,
    rule_owner_ids,
)
from .const import (
    EVENT_ARRIVAL,
    EVENT_MANUAL_MOVE,
    EVENT_STATE_CHANGE,
    GUARD_ANY,
    GUARD_DEFAULT_RECHECK,
    GUARD_DEFER,
    GUARD_DIRECTIONS,
    GUARD_FORCE,
    GUARD_POLICIES,
    GUARD_STAGE_OUTPUT,
    GUARD_STAGES,
    GUARD_TIMEOUTS,
    RULE_DEFAULT_ZONE,
)
from .validation import ERROR, Problem, validate

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subentry flows, one per member of `config_store.SUBENTRY_TYPES`: `blind`,
# `zone`, `value`, `condition`, `mode`, `rule`, `guard`.
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
    # `bad_travel_time` is about one blind's own field, and the `blind` form
    # is the only one with that field -- but every `blind` subentry is
    # interchangeable for fixing it in the sense this dict means: a save of
    # *any* blind that leaves a bad `travel_time` standing anywhere is a save
    # that could have fixed it, because the form being submitted carries a
    # `travel_time` of its own. It therefore needs no `_ATTRIBUTED_CODES`
    # entry. (Blocking every blind save on one bad blind is not the deadlock
    # `_ATTRIBUTED_CODES` exists for either: unlike a zone, a blind needs
    # nothing else to exist first, so the offending blind can always be
    # edited.)
    "bad_travel_time": frozenset({BLIND}),
    "zone_member_unknown": frozenset({ZONE}),
    "blind_in_two_zones": frozenset({ZONE}),
    "blind_without_zone": frozenset({ZONE}),
    "no_fallback_mode": frozenset({MODE}),
    "fallback_mode_not_last": frozenset({MODE}),
    # Guards now own a subentry type, so their problems block guard saves.
    # Every one of these is a statement about a single guard's own fields,
    # and every guard form carries those fields -- so the *type* is enough
    # and none of them needs `_ATTRIBUTED_CODES`, except the two below that
    # name one specific guard.
    "guard_unknown_policy": frozenset({GUARD}),
    "guard_defer_needs_timeout": frozenset({GUARD}),
    "guard_unused_field": frozenset({GUARD}),
    "guard_force_needs_action": frozenset({GUARD}),
    "guard_bad_direction": frozenset({GUARD}),
    "guard_bad_stage": frozenset({GUARD}),
    "guard_input_direction": frozenset({GUARD}),
    "guard_unknown_target": frozenset({GUARD}),
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
        # A guard's own two positional codes, for the same reason a rule's
        # are here: a tie on one `order`, or a guard naming a deleted zone,
        # is not something adding an unrelated guard caused or can fix. A
        # guard's identity in `owners` is the `"guard#<index>"` string
        # `validation._guard_owner` builds and
        # `config_store.guard_owner_ids` maps a real subentry id onto.
        "duplicate_guard_order",
        "guard_unreachable",
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
        then the two `duplicate_*_order_problems` (the one thing
        `validate()` cannot see once subentries collapse into a `Config`
        -- see those functions' own docstrings).

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

        problems = (
            validate(config)
            + duplicate_rule_order_problems(candidate)
            + duplicate_guard_order_problems(candidate)
        )
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
        # `min=1`, not 0: `planner.plan` derives the arrival wait from this
        # number, so 0 means a wait that expires before the blind has moved
        # and a tilt command discarded mid-travel. `validation._check_blinds`
        # is the check (a YAML file reaches the same `Config` without passing
        # through this form); this stops the form offering the value at all.
        vol.Optional("travel_time", default=60): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=600, step=1, unit_of_measurement="s", mode=_BOX
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


def _zone_options(entry: Any) -> list[str]:
    """Every real zone id, plus `const.RULE_DEFAULT_ZONE` ("*"): a rule's whole choice of "zone".

    One function, used everywhere a rule's `zone` field is built (this
    module's own `_rule_pick_schema`/`RuleSubentryFlowHandler._build_schema`,
    and `options_flow.py`'s zone-browsing picker), so "a rule can target any
    configured zone, or the mode-wide default" is stated once rather than
    the option list and the wildcard being assembled separately at each call
    site. `"*"` sorts before every zone id under plain string ordering
    (`sort=True` on the selector), which is a reasonable side effect --
    "apply to the whole mode" reads as the first, most general choice -- but
    incidental, not the reason it is included.
    """
    return [*_configured_ids(entry, ZONE), RULE_DEFAULT_ZONE]


def _rule_pick_schema(entry: Any) -> vol.Schema:
    """Step one of adding a rule: the `(mode, zone)` list it will join.

    A free function, not inlined into `RuleSubentryFlowHandler.async_step_user`
    below, so `options_flow.py`'s own rule-add step can render the identical
    form -- see that module's docstring for why the phase 5 menu must read
    this rather than grow a second copy of it. Also reused, unchanged, by
    `options_flow.py`'s per-zone rule-browsing screen: picking "which
    `(mode, zone)` pair" is the same question whether the next step adds a
    rule or shows one, so it is asked with the same form both times.
    """
    return vol.Schema(
        {
            vol.Required(_RULE_MODE_FIELD): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_configured_ids(entry, MODE), sort=True)
            ),
            vol.Required(_RULE_ZONE_FIELD): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_zone_options(entry), sort=True)
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

    `mode` and `zone` are `SelectSelector`s over what is actually configured
    (`_zone_options`, which is `zone`'s real choices plus `const.
    RULE_DEFAULT_ZONE`, "*"), never free text, so a rule cannot be filed
    under a pair that does not exist. That also means modes and zones must
    exist before any rule can be added, which is not a deadlock of the kind
    `test_full_build_up_sequence_a_human_would_perform` guards against: a
    rule is *about* a mode and a zone, so there is nothing to add first and
    fix later, and no save is being refused -- the form simply has nothing to
    offer yet. Picking `"*"` for `zone` makes this rule a *default* for the
    whole mode instead of one zone (phase 6 task 1, `engine._apply_rules`):
    every zone in that mode without a matching rule of its own falls
    through to it.

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
                    selector.SelectSelectorConfig(options=_zone_options(entry), sort=True)
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
                        options=[EVENT_ARRIVAL, EVENT_MANUAL_MOVE, EVENT_STATE_CHANGE],
                        multiple=True,
                        sort=True,
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


_GUARD_POLICY_FIELD = "policy"
_GUARD_ORDER_FIELD = "order"
_GUARD_NAME_FIELD = "name"
_GUARD_TARGETS_FIELD = "targets"
_GUARD_APPLIES_FIELD = "applies_to"
_GUARD_STAGE_FIELD = "stage"
_GUARD_REF_FIELD = "condition_ref"
_GUARD_INLINE_FIELD = "when"
_GUARD_POSITION_FIELD = "position"
_GUARD_TILT_FIELD = "tilt"
_GUARD_WAIT_LIMIT_FIELD = "wait_limit"
_GUARD_MAX_WAIT_FIELD = "max_wait_duration"
_GUARD_ON_TIMEOUT_FIELD = "on_timeout"
_GUARD_RECHECK_FIELD = "recheck_every"

# The two answers to "how long may this defer wait". `indefinitely` is a
# *value* (`Guard.max_wait = None`, wait forever), not an absence -- which is
# exactly why this field exists instead of reading a bare duration: a
# `DurationSelector` always returns something (zero), so it cannot say "I
# left this blank", and blank has to stay distinguishable from "no limit".
_GUARD_WAIT_AT_MOST = "for_at_most"
_GUARD_WAIT_INDEFINITELY = "indefinitely"
_GUARD_WAIT_LIMITS = (_GUARD_WAIT_AT_MOST, _GUARD_WAIT_INDEFINITELY)


def _guard_target_options(entry: Any) -> list[str]:
    """Everything a guard's `targets` may name: every configured blind, and every zone.

    Not an `EntitySelector`: a zone is not an entity, and free text lets a
    typo through silently -- the same reasoning `_zone_options` gives for a
    rule's `zone`. An empty selection means "every blind in the house", so
    "unset" and "explicitly all" coincide here and need no third state (they
    do not for `max_wait`, which is why that one is spelled out).
    """
    blinds = sorted(
        sub.data["entity"]
        for sub in entry.subentries.values()
        if sub.subentry_type == BLIND and "entity" in sub.data
    )
    return [*blinds, *_configured_ids(entry, ZONE)]


def _next_guard_order(entry: Any) -> int:
    """The `order` that appends a guard to the end of the list.

    The guard twin of `_next_order`, and malformed existing orders are
    skipped for the same reason: this only suggests a form value.
    """
    orders = []
    for sub in entry.subentries.values():
        if sub.subentry_type != GUARD:
            continue
        try:
            orders.append(int(sub.data[_GUARD_ORDER_FIELD]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(orders) + _ORDER_GAP if orders else 0


def _guard_pick_schema(entry: Any) -> vol.Schema:
    """Step one of adding a guard: which policy, where in the order, and a name.

    A free function for the same reason `_rule_pick_schema` is one:
    `options_flow.py`'s menu renders the identical form rather than growing a
    second copy of it.

    `policy` deliberately has no `default` and no suggested value. The three
    policies do three different things, so prefilling any of them would be a
    silent choice made on the user's behalf -- the same rule `on_timeout`
    follows one level down.
    """
    return vol.Schema(
        {
            vol.Required(_GUARD_POLICY_FIELD): selector.SelectSelector(
                # `sort=False`: the order in `const.GUARD_POLICIES` goes from
                # least to most invasive, which is more useful than alphabetical.
                selector.SelectSelectorConfig(options=list(GUARD_POLICIES), sort=False)
            ),
            vol.Required(_GUARD_ORDER_FIELD, default=_next_guard_order(entry)): (
                selector.NumberSelector(selector.NumberSelectorConfig(step=1, mode=_BOX))
            ),
            vol.Optional(_GUARD_NAME_FIELD, default=""): selector.TextSelector(),
        }
    )


def _duration_seconds(raw: Any) -> int:
    """Total seconds of a `DurationSelector` submission, which is a `{hours, minutes, seconds}`."""
    if not isinstance(raw, dict):
        return 0
    return (
        int(raw.get("hours") or 0) * 3600
        + int(raw.get("minutes") or 0) * 60
        + int(raw.get("seconds") or 0)
    )


def _seconds_to_duration(total: int) -> dict[str, int]:
    """Invert `_duration_seconds`, to prefill a reconfigure form."""
    return {
        "hours": total // 3600,
        "minutes": (total % 3600) // 60,
        "seconds": total % 60,
    }


class GuardSubentryFlowHandler(_SubentryFlowBase):
    """Add, edit or remove one `guard` subentry.

    Two steps, and for a different reason than `rule`'s two: `policy` does
    not merely change another field's *default*, it decides which fields
    **exist**. Home Assistant builds a form's schema when it renders, not as
    the user types, so leaving `policy` editable on the same screen as
    `max_wait`/`on_timeout` would let someone switch `skip` to `defer` and
    submit a form that never contained those fields -- saving a guard that
    fails `guard_defer_needs_timeout` with no way to fix it on screen. So
    step one is the policy (plus `order` and `name`), step two is the guard,
    and the policy appears there only as text in the header. Changing an
    existing guard's policy means walking step one again, which an edit does
    anyway.

    Both edit screens live under the one `reconfigure` step id and are told
    apart by `self._policy is None` -- the same trick `RuleSubentryFlowHandler`
    plays with `self._pick`. That keeps `_step(step_id="reconfigure")`, and
    with it `_get_reconfigure_subentry()`, so an edit writes back into the
    existing subentry instead of creating a second one.

    The condition splits into `condition_ref` and `when` exactly as `mode`
    and `rule` split theirs, and for the same reason (see
    `ModeSubentryFlowHandler`). That split is also the only route to a
    `numeric_state` guard -- Home Assistant's own selector rejects the
    `default` key this project requires -- so the house's wind protection,
    which keys on a numeric threshold, has to name a `condition` subentry
    rather than build its test inline.
    """

    subentry_type = GUARD
    # Like a rule, a guard has no id field: its identity is its position in
    # the one ordered list, so `_duplicate_errors` does not apply and
    # `duplicate_guard_order` covers the collision that does matter.
    id_key = None

    # Set by step one, read by step two's schema and prefill. `None` on the
    # first render of a reconfigure, which is what makes that render step one.
    _policy: str | None = None
    _pick: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Step one of adding a guard: which policy, and where in the order."""
        entry = self._get_entry()
        if user_input is not None:
            self._pick = dict(user_input)
            self._policy = str(user_input[_GUARD_POLICY_FIELD])
            return await self._step(None, step_id="guard")

        return self.async_show_form(step_id="user", data_schema=_guard_pick_schema(entry))

    async def async_step_guard(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Step two of adding a guard: the guard itself."""
        return await self._step(user_input, step_id="guard")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Edit an existing guard: step one on the first render, step two after it.

        The first render has to be step one because the saved policy decides
        step two's schema, and the user must be able to change it -- see the
        class docstring. `self._policy` starting as `None` is what marks
        "step one has not been answered yet" without a second step id.
        """
        if self._policy is None:
            subentry = self._get_reconfigure_subentry()
            if user_input is not None:
                self._pick = dict(user_input)
                self._policy = str(user_input[_GUARD_POLICY_FIELD])
                return await super().async_step_reconfigure(None)

            data = dict(subentry.data)
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self.add_suggested_values_to_schema(
                    _guard_pick_schema(self._get_entry()),
                    {
                        _GUARD_POLICY_FIELD: data.get(_GUARD_POLICY_FIELD),
                        _GUARD_ORDER_FIELD: data.get(_GUARD_ORDER_FIELD),
                        _GUARD_NAME_FIELD: data.get(_GUARD_NAME_FIELD, ""),
                    },
                ),
            )

        return await super().async_step_reconfigure(user_input)

    def _build_schema(self, entry: Any) -> vol.Schema:
        """Build step two from the policy picked in step one, plus what the entry has."""
        fields: dict[Any, Any] = {
            vol.Optional(_GUARD_TARGETS_FIELD, default=list): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_guard_target_options(entry),
                    multiple=True,
                    sort=True,
                )
            ),
            # Default `any`, not `closing`, even though most of this house's
            # guards are `closing`: `any` is the *model's* default (what a
            # missing key means in YAML), so prefilling anything else would
            # make the same guard mean two different things depending on
            # which door it was written through. The hint that most guards
            # are `closing` belongs in `data_description`, not here.
            vol.Required(_GUARD_APPLIES_FIELD, default=GUARD_ANY): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(GUARD_DIRECTIONS), sort=False)
            ),
            vol.Required(_GUARD_STAGE_FIELD, default=GUARD_STAGE_OUTPUT): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(GUARD_STAGES), sort=False)
                )
            ),
            vol.Optional(_GUARD_REF_FIELD): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_configured_ids(entry, CONDITION), sort=True)
            ),
            vol.Optional(_GUARD_INLINE_FIELD, default=list): selector.ConditionSelector(),
        }

        if self._policy == GUARD_FORCE:
            axis_options = [_AXIS_KEEP, *_configured_ids(entry, VALUE)]

            def axis() -> selector.SelectSelector:
                return selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=axis_options, custom_value=True, sort=False
                    )
                )

            fields[vol.Required(_GUARD_POSITION_FIELD, default=_AXIS_KEEP)] = axis()
            fields[vol.Required(_GUARD_TILT_FIELD, default=_AXIS_KEEP)] = axis()

        if self._policy == GUARD_DEFER:
            # Neither `wait_limit` nor `on_timeout` gets a `default`: the two
            # choices in each do opposite things, so Home Assistant refusing
            # to submit an unanswered field is the point. `_initial_values`
            # must not smuggle a default back in either.
            fields[vol.Required(_GUARD_WAIT_LIMIT_FIELD)] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(_GUARD_WAIT_LIMITS), sort=False)
            )
            fields[vol.Optional(_GUARD_MAX_WAIT_FIELD, default=dict)] = selector.DurationSelector(
                selector.DurationSelectorConfig(enable_day=False)
            )
            fields[vol.Required(_GUARD_ON_TIMEOUT_FIELD)] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(GUARD_TIMEOUTS), sort=False)
            )
            # `recheck_every` *does* get its default: it is a safety net, not
            # a choice. A deferred wait does not survive a Home Assistant
            # restart, so every parsed `defer` carries a recheck interval
            # whether its author wrote one or not; hiding it as blank would
            # repeat the very omission it exists to cover.
            fields[
                vol.Required(
                    _GUARD_RECHECK_FIELD,
                    default=lambda: _seconds_to_duration(GUARD_DEFAULT_RECHECK),
                )
            ] = selector.DurationSelector(selector.DurationSelectorConfig(enable_day=False))

        return vol.Schema(fields)

    def _initial_values(self, entry: Any) -> dict[str, Any] | None:
        """Prefill step two. For `defer`, deliberately almost nothing.

        Only the fields whose defaults are honest suggestions are listed.
        `wait_limit` and `on_timeout` are absent on purpose: putting them
        here would restore, through `add_suggested_values_to_schema`, exactly
        the default the schema refuses to give them.
        """
        if self._pick is None:
            return None
        return {
            _GUARD_APPLIES_FIELD: GUARD_ANY,
            _GUARD_STAGE_FIELD: GUARD_STAGE_OUTPUT,
            **(
                {
                    _GUARD_POSITION_FIELD: _AXIS_KEEP,
                    _GUARD_TILT_FIELD: _AXIS_KEEP,
                }
                if self._policy == GUARD_FORCE
                else {}
            ),
            **(
                {_GUARD_RECHECK_FIELD: _seconds_to_duration(GUARD_DEFAULT_RECHECK)}
                if self._policy == GUARD_DEFER
                else {}
            ),
        }

    def _local_problems(self, user_input: dict[str, Any]) -> list[str]:
        """Reject a submission that fills both the named and the inline condition."""
        if user_input.get(_GUARD_REF_FIELD) and user_input.get(_GUARD_INLINE_FIELD):
            return ["guard: choose either a named condition or an inline condition, not both"]
        return []

    def _to_data(self, entry: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        """Reshape the submission into the keys `config_schema._parse_guard` reads.

        Per-policy keys are written *only* for the policy that reads them:
        `then` for `force`, `max_wait`/`on_timeout`/`recheck_every` for
        `defer`. Writing them anyway would trip `guard_unused_field`, which
        is an ERROR -- the form has to produce a guard that validates, not
        one that merely round-trips.

        `max_wait` carries the whole "no limit is a value" distinction from
        the spec: `indefinitely` writes `None`, a non-zero duration writes
        seconds, and a zero duration writes **no key at all** so
        `guard_defer_needs_timeout` blocks the save rather than a silent
        zero-second wait being stored.

        `condition_ref` is written as `{"condition": "ref", "name": ...}` --
        the already-parsed shape -- for the reason
        `ModeSubentryFlowHandler._to_data` spells out: a `{"ref": ...}`
        marker is resolved eagerly, so a deleted condition would raise
        `ConfigError` and block every save of every type instead of one
        attributable problem.
        """
        pick = self._pick or {}
        policy = str(pick.get(_GUARD_POLICY_FIELD) or self._policy or "")
        data: dict[str, Any] = {
            _GUARD_POLICY_FIELD: policy,
            _GUARD_ORDER_FIELD: int(pick.get(_GUARD_ORDER_FIELD, 0)),
            _GUARD_APPLIES_FIELD: user_input.get(_GUARD_APPLIES_FIELD, GUARD_ANY),
            _GUARD_STAGE_FIELD: user_input.get(_GUARD_STAGE_FIELD, GUARD_STAGE_OUTPUT),
        }

        name = pick.get(_GUARD_NAME_FIELD)
        if name:
            data[_GUARD_NAME_FIELD] = name

        targets = user_input.get(_GUARD_TARGETS_FIELD)
        if targets:
            data[_GUARD_TARGETS_FIELD] = list(targets)

        ref = user_input.get(_GUARD_REF_FIELD)
        inline = _normalize_condition_tree(user_input.get(_GUARD_INLINE_FIELD))
        if ref:
            data[_GUARD_INLINE_FIELD] = {"condition": "ref", "name": ref}
        elif inline:
            data[_GUARD_INLINE_FIELD] = inline

        if policy == GUARD_FORCE:
            value_ids = _configured_ids(entry, VALUE)
            data["then"] = {
                _GUARD_POSITION_FIELD: _axis_to_data(
                    user_input.get(_GUARD_POSITION_FIELD, _AXIS_KEEP), value_ids
                ),
                _GUARD_TILT_FIELD: _axis_to_data(
                    user_input.get(_GUARD_TILT_FIELD, _AXIS_KEEP), value_ids
                ),
            }

        if policy == GUARD_DEFER:
            limit = user_input.get(_GUARD_WAIT_LIMIT_FIELD)
            if limit == _GUARD_WAIT_INDEFINITELY:
                data["max_wait"] = None
            else:
                seconds = _duration_seconds(user_input.get(_GUARD_MAX_WAIT_FIELD))
                if seconds:
                    data["max_wait"] = seconds
            on_timeout = user_input.get(_GUARD_ON_TIMEOUT_FIELD)
            if on_timeout:
                data[_GUARD_ON_TIMEOUT_FIELD] = on_timeout
            data[_GUARD_RECHECK_FIELD] = (
                _duration_seconds(user_input.get(_GUARD_RECHECK_FIELD)) or GUARD_DEFAULT_RECHECK
            )

        return data

    def _to_form_values(self, data: dict[str, Any]) -> dict[str, Any]:
        """Invert `_to_data` for step two of an edit.

        `max_wait` maps back three ways, matching the three states `_to_data`
        can write: `None` is `indefinitely`, an int is `for_at_most` plus the
        duration, and `UNSET` (a guard that already fails
        `guard_defer_needs_timeout` today) leaves **both** fields blank
        rather than guessing which the author meant.
        """
        when = data.get(_GUARD_INLINE_FIELD)
        is_ref = isinstance(when, dict) and when.get("condition") == "ref" and "name" in when
        if is_ref or when is None:
            inline: Any = []
        elif isinstance(when, list):
            inline = when
        else:
            inline = [when]

        then = data.get("then") or {}
        values: dict[str, Any] = {
            _GUARD_TARGETS_FIELD: list(data.get(_GUARD_TARGETS_FIELD) or []),
            _GUARD_APPLIES_FIELD: data.get(_GUARD_APPLIES_FIELD, GUARD_ANY),
            _GUARD_STAGE_FIELD: data.get(_GUARD_STAGE_FIELD, GUARD_STAGE_OUTPUT),
            _GUARD_REF_FIELD: when["name"] if is_ref else None,
            _GUARD_INLINE_FIELD: inline,
        }

        if self._policy == GUARD_FORCE:
            values[_GUARD_POSITION_FIELD] = _axis_to_form(then.get(_GUARD_POSITION_FIELD))
            values[_GUARD_TILT_FIELD] = _axis_to_form(then.get(_GUARD_TILT_FIELD))

        if self._policy == GUARD_DEFER:
            if "max_wait" in data:
                max_wait = data["max_wait"]
                if max_wait is None:
                    values[_GUARD_WAIT_LIMIT_FIELD] = _GUARD_WAIT_INDEFINITELY
                else:
                    values[_GUARD_WAIT_LIMIT_FIELD] = _GUARD_WAIT_AT_MOST
                    values[_GUARD_MAX_WAIT_FIELD] = _seconds_to_duration(int(max_wait))
            on_timeout = data.get(_GUARD_ON_TIMEOUT_FIELD)
            if on_timeout:
                values[_GUARD_ON_TIMEOUT_FIELD] = on_timeout
            values[_GUARD_RECHECK_FIELD] = _seconds_to_duration(
                int(data.get(_GUARD_RECHECK_FIELD) or GUARD_DEFAULT_RECHECK)
            )

        return values

    def _candidate_id(self, candidate: Any, subentry_id: str, data: dict[str, Any]) -> Any:
        """This guard's position in the finished list, as `validation` names it (`guard#N`)."""
        return guard_owner_ids(candidate).get(subentry_id)

    def _title(self, data: dict[str, Any]) -> str:
        """Order first, then what the guard does -- so the list reads without opening rows.

        The same reasoning as `RuleSubentryFlowHandler._title`: order is this
        type's whole point and Home Assistant sorts subentries by title.
        """
        label = f"{data.get(_GUARD_ORDER_FIELD)} {data.get(_GUARD_POLICY_FIELD)}"
        name = data.get(_GUARD_NAME_FIELD)
        if name:
            label = f"{label} {name}"
        targets = data.get(_GUARD_TARGETS_FIELD)
        scope = ", ".join(targets) if targets else "all blinds"
        return f"{label} -> {data.get(_GUARD_APPLIES_FIELD, GUARD_ANY)} on {scope}"


_MD_IGNORE_FIELD = "ignore_while_on"


class ManualDetectionSubentryFlowHandler(_SubentryFlowBase):
    """Add or edit the one `manual_detection` subentry.

    The only type with cardinality 0-or-1: it is a single house-wide setting
    -- which callers' movements are not a person's -- rather than one of many
    small items. So `async_step_user` refuses a second one instead of relying
    on `_duplicate_errors`, which is about a unique *field* among several
    subentries and has nothing to say about "there may only be one at all".

    The field is `ignore_while_on` rather than the "scripts" the spec asked
    for, because the test really is "is this entity `on` right now": a
    `script.<x>` entity is `on` for exactly its own run, which is what makes
    it useful here, but an `input_boolean` a house sets around its own bulk
    movements works identically and there is no reason to forbid it.
    """

    subentry_type = MANUAL_DETECTION
    # No id field: there is only ever one of these, so there is nothing for it
    # to be unique among.
    id_key = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Refuse a second one, then behave like any other add."""
        entry = self._get_entry()
        already = any(sub.subentry_type == MANUAL_DETECTION for sub in entry.subentries.values())
        if already:
            return self.async_abort(reason="already_configured")
        return await self._step(user_input, step_id="user")

    def _build_schema(self, entry: Any) -> vol.Schema:
        """One multi-select of entity ids, free-form because they name anything."""
        return vol.Schema(
            {
                vol.Optional(_MD_IGNORE_FIELD, default=list): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
            }
        )

    def _to_data(self, entry: Any, user_input: dict[str, Any]) -> dict[str, Any]:
        """Write the list, always -- an empty one is a meaningful "exclude nothing"."""
        return {_MD_IGNORE_FIELD: list(user_input.get(_MD_IGNORE_FIELD) or [])}

    def _to_form_values(self, data: dict[str, Any]) -> dict[str, Any]:
        """Invert `_to_data`, to prefill a reconfigure form."""
        return {_MD_IGNORE_FIELD: list(data.get(_MD_IGNORE_FIELD) or [])}

    def _candidate_id(self, candidate: Any, subentry_id: str, data: dict[str, Any]) -> Any:
        """There is only one, so its identity is its type."""
        return MANUAL_DETECTION

    def _title(self, data: dict[str, Any]) -> str:
        """Say how many callers are excluded -- the only number worth showing."""
        count = len(data.get(_MD_IGNORE_FIELD) or [])
        return f"Manual detection ({count} ignored)"


# Registered by subentry type. `config_flow.py`'s
# `CoverLogicConfigFlow.async_get_supported_subentry_types` just returns
# this; `options_flow.py`'s `_render_type_form` looks a single type up in it
# to build a bare instance -- see this module's own docstring for both uses.
SUBENTRY_FLOW_HANDLERS: dict[str, type[config_entries.ConfigSubentryFlow]] = {
    BLIND: BlindSubentryFlowHandler,
    ZONE: ZoneSubentryFlowHandler,
    VALUE: ValueSubentryFlowHandler,
    CONDITION: ConditionSubentryFlowHandler,
    MODE: ModeSubentryFlowHandler,
    RULE: RuleSubentryFlowHandler,
    GUARD: GuardSubentryFlowHandler,
    MANUAL_DETECTION: ManualDetectionSubentryFlowHandler,
}
