"""The options flow: one main menu over the same subentries the six subentry flows edit.

**Why this exists.** Phase 4 gave every configuration type its own subentry
flow (`subentry_flow.py`), but the integration had `supports_options: false`
-- the entry page showed nothing but Home Assistant's own generic flat list
of "Add blind / Add zone / ..." buttons, no counts, no way to tell an empty
section from a full one without opening it, and no path back to "everything"
after editing one item. The owner's verdict on the result, verbatim: "vôbec
to teraz nie je intuitívne a hlavne hlavný setup." This module is the menu
that answers that -- see `docs/superpowers/plans/2026-08-28-cover-logic-
faza-5-setup-a-menu.md`'s Task 1.

**Why it cannot just open a subentry flow.** Measured directly against
`homeassistant==2026.8.0`: `FlowResultType` has `FORM`, `MENU`,
`CREATE_ENTRY`, `ABORT`, `EXTERNAL_STEP(_DONE)`, `SHOW_PROGRESS(_DONE)` --
nothing that hands control to a *different* flow. An options flow step can
render a menu, a form, or finish; it cannot say "now run
`BlindSubentryFlowHandler`". The naive fix is a second copy of every form,
which is exactly the class of problem this project's own phase 4 Task 4
review already flagged as an Important finding once (two copies of the rule
sort, both "working", silently able to disagree with each other while the
92,160-scenario migration gate stayed blind to the difference -- see
`config_store.py`'s own "One grouping, not two" section).

**One owner, two doors.** `subentry_flow.py`'s six `_SubentryFlowBase`
subclasses (`BlindSubentryFlowHandler`, ...) live in a module owned by
neither this one nor `config_flow.py`, and separate "what this type's form
looks like and how a submission becomes subentry data" (`_build_schema`,
`_to_data`, `_to_form_values`, `_initial_values`, `_local_problems`,
`_candidate_id`, `_title`, `_blocking_errors`) from "how a `ConfigSubentryFlow`
step turns that into a rendered form and a finished flow" (`_step`,
`async_step_user`/`async_step_reconfigure`). Every method in the first group
needs nothing from `self` beyond the `entry` it is explicitly handed --
`_to_data` was the one exception (`RuleSubentryFlowHandler` read
`self._get_entry()`), fixed as part of landing this menu so the whole group
could be called from a bare instance of the class, never registered with
Home Assistant's flow manager at all. `_render_type_form` below is this
module's one and only caller of that group, for every one of the six types;
it is the "two doors" arrangement the task brief asks for, made possible by
that one constructor-independence property rather than by copying a single
field label, schema or validation rule. What *is* necessarily different
between the two doors -- what happens once a submission validates -- cannot
be shared: a subentry flow step finishes the flow (`async_create_entry`/
`async_update_and_abort`), while this flow keeps going (`hass.config_entries.
async_add_subentry`/`async_update_subentry`, then back to the section menu).
That handful of lines is genuinely different glue, not a second copy of a
form.

The classes did not start out in `subentry_flow.py` -- this module used to
import them straight out of `config_flow.py`, which in turn needed
`CoverLogicOptionsFlow` from here to hand out an options flow instance, a
real `py/cyclic-import` worked around at the time by deferring one side to
call time. Pulling the shared classes into a third module neither door owns
removes the cycle outright rather than hiding it: `config_flow.py` and this
module both import `subentry_flow.py`, never each other.

**Why sections track state on `self` instead of encoding it in the step id.**
A menu's `next_step_id` must name a real `async_step_<name>` method
(`homeassistant.data_entry_flow._async_handle_step`); there is no wildcard
dispatch. A per-item id (`edit_<subentry_id>`) would need one method per
subentry that exists right now, which cannot be written in advance. Instead,
each of the six list-menu entry points (`async_step_blinds`, ...) records
which section is active (`self._section`) before rendering that section's
own menu; the three actions available from there (`add`, `edit`, `remove`)
are generic methods that read `self._section` back out, and are exactly the
same three methods regardless of which section sent the user to them.

**Why menu items disable themselves instead of erroring.** `edit`/`remove`
are only offered when the section already has at least one item -- computed
fresh at render time, not a fixed list -- so there is nothing to catch: the
option a user could pick to reach an empty edit/remove flow is simply not on
the menu.

**One screen writes `entry.options`; the rest write subentries.** Almost all
of this flow's point is to mutate *subentries*, which live independently of
`ConfigEntry.options`, and until phase 3 gave this integration an executor
nothing here touched options at all. `async_step_execution` is the exception
and, so far, the only one: `dry_run` (`const.OPT_DRY_RUN`) is an operational
option of the installation, not a fact about the house, and it belongs in
options precisely because a write there reaches `runner.py` without a reload
-- see `const.OPT_DRY_RUN`'s own comment for why not `entry.data`. It is
written with `async_update_entry(options=...)`, not `self.async_create_entry`:
this flow never finishes. Every screen either shows another form/menu or loops
back to one, so it simply stays open (as any menu-driven UI does) until the
user closes it -- Home Assistant's own generic "flow aborted by the user"
handling, needing no strings.json entry of this integration's own.
"""

from typing import Any

from homeassistant.config_entries import ConfigSubentry, OptionsFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
import voluptuous as vol

from .config_schema import ConfigError, load_config_file
from .config_store import (
    BLIND,
    CONDITION,
    MODE,
    RULE,
    SUBENTRY_TYPES,
    VALUE,
    ZONE,
    _grouped_rules,
    config_from_subentries,
    duplicate_rule_order_problems,
    effective_rule_items,
)
from .conformance import diff_configs, repo_fixture_path
from .const import DEFAULT_CONFIG_PATH, DEFAULT_DRY_RUN, OPT_DRY_RUN, RULE_DEFAULT_ZONE
from .model import Config
from .services import (
    ATTR_DRY_RUN,
    ATTR_OVERWRITE,
    ATTR_PATH,
    EXPORT_CONFIG_SCHEMA,
    IMPORT_CONFIG_SCHEMA,
    _async_export_config,
    _async_import_config,
)
from .subentry_flow import (
    _NEW_SUBENTRY_ID,
    _RULE_MODE_FIELD,
    _RULE_ZONE_FIELD,
    SUBENTRY_FLOW_HANDLERS,
    _axis_to_form,
    _configured_ids,
    _rule_pick_schema,
)
from .validation import ERROR, WARNING, Problem, validate

# The main menu's options, in the order the task brief's own mock-up lays
# them out column-major (nouns first -- blind/zone/value -- then the logic
# built on top of them -- condition/mode/rule), plus the two actions that are
# not a subentry type at all.
_RULES_SECTION = "rules"

_SECTION_TYPE: dict[str, str] = {
    "blinds": BLIND,
    "zones": ZONE,
    "values": VALUE,
    "conditions": CONDITION,
    "modes": MODE,
    _RULES_SECTION: RULE,
}

_MAIN_MENU_OPTIONS = [*_SECTION_TYPE, "import_export", "execution", "check_matrix"]

_PICK_FIELD = "subentry_id"
_CONFIRM_FIELD = "confirm"
_ACTION_FIELD = "action"
_ACTION_IMPORT = "import"
_ACTION_EXPORT = "export"


def _counts(entry: Any) -> dict[str, int]:
    """How many subentries of each type `entry` currently has.

    Read directly off `entry.subentries` rather than through a built
    `Config` -- a count must stay meaningful (and the menu's "is this section
    empty" distinction must stay accurate) even for a house whose current
    subentries do not parse, which `config_from_subentries` would raise on.
    """
    counts = dict.fromkeys(SUBENTRY_TYPES, 0)
    for subentry in entry.subentries.values():
        if subentry.subentry_type in counts:
            counts[subentry.subentry_type] += 1
    return counts


def _items(entry: Any, subentry_type: str) -> list[tuple[str, str]]:
    """`(subentry_id, title)` for every subentry of `subentry_type`, in display order.

    Every subentry already carries a human-meaningful `.title` -- each
    `_SubentryFlowBase._title()` override guarantees that -- so this needs no
    type-specific label logic of its own, except for `rule`: see
    `_rule_items` for why that one type cannot just sort by title.
    """
    if subentry_type == RULE:
        return _rule_items(entry)
    pairs = [
        (subentry_id, subentry.title or subentry_id)
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == subentry_type
    ]
    return sorted(pairs, key=lambda pair: pair[1])


def _rule_items(entry: Any) -> list[tuple[str, str]]:
    """Every rule subentry as `(subentry_id, title)`, in the real evaluation order.

    Reads `config_store._grouped_rules` -- the single place that groups and
    sorts rule subentries by `(mode, zone, order)` -- instead of sorting
    titles alphabetically (which would *look* ordered but silently lie: "10"
    sorts before "9" as text). A rule's title always leads with its `order`
    (`RuleSubentryFlowHandler._title`), so a picker list built from anything
    but this exact grouping would show rules in an order that is not the one
    the engine evaluates them in -- precisely what the task brief calls out
    as actively misleading, and precisely the "second sort" mistake
    `_grouped_rules`'s own docstring already paid for once.
    """
    return [
        (subentry_id, entry.subentries[subentry_id].title or subentry_id)
        for items in _grouped_rules(entry).values()
        for subentry_id, _data in items
    ]


def _rule_action_text(then: dict[str, Any]) -> str:
    """`position`/`tilt` as a human string: a number, `"keep"`, or a value's name.

    Reuses `subentry_flow._axis_to_form` -- the exact function
    `RuleSubentryFlowHandler._title` already calls to build a single rule
    subentry's own title -- rather than a second axis-rendering rule that
    could show `keep` or a `!ref` differently from what that title already
    promises. `then` is a raw rule subentry's `"then"` mapping (`{"position":
    ..., "tilt": ...}`), the same shape `_grouped_rules` hands back unparsed.
    """
    return f"position={_axis_to_form(then.get('position'))}, tilt={_axis_to_form(then.get('tilt'))}"


def _rule_overview_line(data: dict[str, Any], *, is_default: bool) -> str:
    """One rule row of `_rule_overview_text`'s report, marked when it is an inherited default."""
    then = data.get("then") or {}
    line = f"  {data.get('order')}: {_rule_action_text(then)}"
    name = data.get("name")
    if name:
        line = f"{line} ({name})"
    if is_default:
        line = f"{line} [inherited from mode default]"
    return line


def _rule_overview_text(entry: Any) -> str:
    """Every rule subentry as the house will actually evaluate it: grouped, ordered, visible.

    Reads `config_store._grouped_rules` directly for which `(mode, zone)`
    pairs have their own rule subentries at all, but a real zone's own block
    is rendered through `config_store.effective_rule_items` -- its own rules,
    then the mode's shared defaults, marked `[inherited from mode default]`
    -- not `groups[key]` alone, or a zone whose blinds are entirely decided
    by a mode-wide default would show as if nothing decided it. Neither
    function re-sorts anything `_grouped_rules` already sorted: see that
    function's own "One grouping, not two" docstring section, and `_rule_
    items` above, which already makes the same choice for the edit picker.
    Within a `(mode, zone)` pair the engine tries rules in exactly this
    order and stops at the first match, so a summary that reordered them --
    even to look tidier, e.g. alphabetically by pair -- would show a house
    behaviour that is not the one that actually runs.

    A mode's default group (`f"{mode}.{RULE_DEFAULT_ZONE}"`) is shown once,
    under its own key, exactly as `groups` holds it (nothing to inherit from
    a further default -- see `effective_rule_items`'s own docstring) -- and
    then again under every real zone that has no rule subentry of its own at
    all, which `_grouped_rules` alone would never surface: no rule subentry
    names that (mode, zone) pair's key, so it is invisible to a report that
    only walks `groups.items()`.
    """
    groups = _grouped_rules(entry)
    if not groups:
        return "No rules configured yet."

    lines: list[str] = []
    seen_pairs: set[str] = set()
    default_modes: set[str] = set()
    for key in groups:
        mode_id, _dot, zone_id = key.partition(".")
        if zone_id == RULE_DEFAULT_ZONE:
            default_modes.add(mode_id)

    for key, items in groups.items():
        mode_id, _dot, zone_id = key.partition(".")
        seen_pairs.add(key)
        if zone_id == RULE_DEFAULT_ZONE:
            lines.append(f"{key} (default for every zone in this mode):")
            lines += [_rule_overview_line(data, is_default=False) for _sid, data in items]
            continue
        lines.append(f"{key}:")
        lines += [
            _rule_overview_line(data, is_default=is_default)
            for _sid, data, is_default in effective_rule_items(entry, mode_id, zone_id)
        ]

    for zone_id in _configured_ids(entry, ZONE):
        for mode_id in sorted(default_modes):
            pair_key = f"{mode_id}.{zone_id}"
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            lines.append(f"{pair_key}:")
            lines += [
                _rule_overview_line(data, is_default=is_default)
                for _sid, data, is_default in effective_rule_items(entry, mode_id, zone_id)
            ]

    return "\n".join(lines)


def _current_problems(entry: Any) -> tuple[Config | None, list[Problem]]:
    """Parse `entry`'s subentries and return `(config_or_None, problems)`.

    The one place this task's two health surfaces -- the main menu's
    at-a-glance summary (`_health_summary_text`) and `check_matrix`'s full
    report -- both get their problem list from, so a save that fixes (or
    breaks) something shows the same count in both places rather than two
    independently-computed answers drifting apart. Calls exactly the checks
    `_SubentryFlowBase._blocking_errors` already runs before a save
    (`validate`, `duplicate_rule_order_problems`) -- see that method's own
    docstring for why together they are one source's complete set -- never
    a third, re-derived notion of "is this configuration sound".

    `config` is `None` when the entry does not even parse into a `Config` at
    all; `problems` in that case is a single synthetic `Problem` naming the
    parse failure, unattributed (no single subentry is "the" cause of a
    structural error like a missing required key) -- an unreadable
    configuration is not a healthy one either, and reporting zero problems
    for it would be worse than misleading.
    """
    try:
        config = config_from_subentries(entry)
    except ConfigError as err:
        problem = Problem(ERROR, "config_unparsable", f"configuration does not parse: {err}")
        return None, [problem]
    return config, validate(config) + duplicate_rule_order_problems(entry)


def _owner_text(owners: frozenset[tuple[str, str]]) -> str:
    """A `[type 'id', ...]` suffix naming which subentries `problem.owners` points at.

    Empty for a `Problem` `validate()` cannot attribute to one subentry (see
    `validation.Problem.owners`'s own docstring -- most codes have exactly
    one owning *type*, not a specific instance). For `condition`/`mode`, the
    `id` in `owners` is already the value the type's own form and its
    picker list (`_items`) show as that subentry's title (`_SubentryFlowBase.
    _title`'s default), so no lookup back to a real subentry id is needed
    to make it findable. For `rule`, `id` is the `"<mode>.<zone>#<index>"`
    string `validation._rule_owner`/`config_store.rule_owner_ids` share --
    the same label `engine._apply_rules` puts in a decision trace -- which
    names exactly the `(mode, zone)` group and position the "Rules -> See
    what the house will do" report (`_rule_overview_text`) already lists
    rules under, so it points at that same screen rather than needing its
    own second resolution of "which real subentry is this".
    """
    if not owners:
        return ""
    return " [" + ", ".join(f"{kind} {name!r}" for kind, name in sorted(owners)) + "]"


def _problem_line(problem: Problem) -> str:
    """One problem as `"SEVERITY code: message [owner, ...]"`."""
    owner_text = _owner_text(problem.owners)
    return f"{problem.severity.upper()} {problem.code}: {problem.message}{owner_text}"


def _severity_counts(problems: list[Problem]) -> tuple[int, int]:
    """`(errors, warnings)` -- how many of each severity `problems` holds."""
    errors = sum(1 for p in problems if p.severity == ERROR)
    warnings = sum(1 for p in problems if p.severity == WARNING)
    return errors, warnings


def _health_summary_text(entry: Any) -> str:
    """One short phrase for the main menu's `check_matrix` label: is this configuration sound?

    Read fresh on every main-menu render (`_show_main_menu`) -- `entry`'s
    subentries already live in memory, so this is the same cheap in-process
    parse-and-validate `check_matrix` itself pays per click, not a cached
    counter that could go stale relative to it.
    """
    config, problems = _current_problems(entry)
    if config is None:
        return "configuration does not parse"
    errors, warnings = _severity_counts(problems)
    if not errors and not warnings:
        return "no problems found"
    parts = []
    if errors:
        parts.append(f"{errors} error" + ("s" if errors != 1 else ""))
    if warnings:
        parts.append(f"{warnings} warning" + ("s" if warnings != 1 else ""))
    return ", ".join(parts)


def _execution_summary_text(entry: Any) -> str:
    """One short phrase for the main menu's `execution` label: does this move covers?

    Deliberately on the menu itself rather than only inside the screen. "Is
    this installation actually driving the blinds right now?" is the one
    question whose wrong answer is expensive in both directions, and a menu
    that reads `Execution (dry run)` answers it without a click.
    """
    return "dry run" if entry.options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN) else "live"


def _validation_report_text(problems: list[Problem]) -> str:
    """`"N error(s), M warning(s)."`, followed by one attributed line per problem."""
    errors, warnings = _severity_counts(problems)
    header = f"{errors} error(s), {warnings} warning(s)."
    if not problems:
        return header
    return header + "\n" + "\n".join(_problem_line(p) for p in problems)


def _coordinator_status_text(entry: Any) -> str:
    """Describe the coordinator's last recomputation, or that none has run yet.

    `entry.runtime_data` (`__init__.CoverLogicData`) only exists once
    `async_setup_entry` has gotten as far as constructing the coordinator --
    absent for an entry that has not finished starting yet, or that raised
    `ConfigEntryNotReady` before reaching that point (exactly the case a
    configuration with `ERROR`-severity problems hits -- see `__init__.py`'s
    own docstring). `getattr` rather than a plain attribute read is what
    keeps this screen usable for precisely the entry states the health
    overview exists to describe, instead of raising `AttributeError` on the
    one case ("nothing has evaluated yet") it most needs to report on.
    """
    data = getattr(entry, "runtime_data", None)
    coordinator = getattr(data, "coordinator", None)
    if coordinator is None:
        return "not yet evaluated (the integration has not finished starting)"
    when = coordinator.last_success.isoformat() if coordinator.last_success is not None else "never"
    if coordinator.last_error:
        return f"last successful recompute {when}; current error: {coordinator.last_error}"
    return f"last successful recompute {when}; no error"


def _pick_schema(items: list[tuple[str, str]]) -> vol.Schema:
    """A single required field: choose one of `items` by id, shown by title.

    `sort=False` -- `items` is already in the order each caller wants shown
    (title order for most types, real evaluation order for `rule`; see
    `_items`), and re-sorting here would undo that for `rule` specifically.
    """
    return vol.Schema(
        {
            vol.Required(_PICK_FIELD): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=subentry_id, label=title)
                        for subentry_id, title in items
                    ],
                    sort=False,
                )
            )
        }
    )


def _zone_rule_schema(entry: Any, items: list[tuple[str, dict, bool]]) -> vol.Schema:
    """Pick one rule out of `items` (own rules, then the mode's inherited defaults), plus remove.

    `sort=False`, like `_pick_schema` already does for `rule` -- `items`
    already carries the real evaluation order (`config_store.
    effective_rule_items`: this pair's own rules, then the mode's shared
    defaults), and alphabetising it would show a house behaviour that is not
    the one that actually runs. The label suffix is the "obvious
    distinction" between the two kinds this screen exists to make -- a title
    alone (`"10 noc.* -> keep/keep"`) does not say *from this zone's point of
    view* that the row is not this zone's own.

    A single boolean, not a second confirm-picker screen the way the
    section-wide `remove` step needs one: this picker already asks "which
    row", so "and remove it" is one more field on the same form rather than
    a second round trip, mirroring `_pick_schema(...).extend(...)` in
    `async_step_remove` but folded into one function since this picker's
    options are never reused for anything but this screen.
    """
    options = []
    for subentry_id, _data, is_default in items:
        title = entry.subentries[subentry_id].title or subentry_id
        label = f"{title} (inherited from mode default)" if is_default else title
        options.append(selector.SelectOptionDict(value=subentry_id, label=label))
    return vol.Schema(
        {
            vol.Required(_PICK_FIELD): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, sort=False)
            ),
            vol.Optional(_CONFIRM_FIELD, default=False): selector.BooleanSelector(),
        }
    )


class _ServiceCallData:
    """Minimal duck-typed stand-in for `homeassistant.core.ServiceCall`.

    `services._async_import_config`/`_async_export_config` read only `.data`
    off the call they are given -- see `async_step_import_export`'s own
    docstring for why this flow calls them directly rather than dispatching
    through Home Assistant's own service-call bus by domain and service
    name. `data` must already be schema-coerced
    (`IMPORT_CONFIG_SCHEMA`/`EXPORT_CONFIG_SCHEMA`, applied by the caller)
    since a real service dispatch would have done that before either handler
    ever saw it.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        """Store the already-coerced `data`."""
        self.data = data


def _describe_error(err: HomeAssistantError) -> str:
    """A description of `err` that never needs a registered `HomeAssistant` instance.

    Every `HomeAssistantError`/`ServiceValidationError` `services.py` raises
    is constructed with only `translation_key`/`translation_placeholders`,
    never a plain message (see, e.g., `_raise_invalid_config`) -- which sets
    `generate_message = True`, and `HomeAssistantError.__str__` then tries to
    render the key through Home Assistant's own translation cache, which
    calls `homeassistant.core.async_get_hass()` and raises `HomeAssistantError
    ("async_get_hass called from the wrong thread")` the moment no hass is
    registered for the running context -- exactly the case for the `hass`
    fakes this project's own test suite uses everywhere else (see `MODELS.
    md`'s own reasoning for testing without a running Home Assistant at
    all), and not something this step should depend on to report an error
    either way. Reads the same two attributes directly instead.
    """
    key = getattr(err, "translation_key", None)
    if not key:
        return str(err)
    placeholders = getattr(err, "translation_placeholders", None) or {}
    detail = ", ".join(f"{name}={value}" for name, value in placeholders.items())
    return f"{key} ({detail})" if detail else key


class CoverLogicOptionsFlow(OptionsFlow):
    """The main menu: browse and edit every subentry type, or import/export/check.

    All navigation state (which section is open, which subentry is being
    edited or removed, the rule-add wizard's first-step pick) lives on
    `self` between steps, the same pattern `RuleSubentryFlowHandler._pick`
    already uses in `subentry_flow.py` -- a fresh instance is created per flow
    (`CoverLogicConfigFlow.async_get_options_flow`), so nothing here is
    shared across two users' flows.
    """

    _section: str | None = None
    _pending_id: str | None = None
    _rule_pick: dict[str, Any] | None = None
    # Set by `async_step_zone`, read by `async_step_zone_rules` -- which
    # `(mode, zone)` pair the per-zone rule screen is currently showing.
    # Kept on `self` for the same reason `_rule_pick` already is: a menu
    # step and the step it delegates to are two different `async_step_*`
    # dispatches, with no argument-passing between them but the instance.
    _zone_pick: dict[str, Any] | None = None

    # -- Main menu -----------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Entry point Home Assistant always dispatches to first."""
        return await self._show_main_menu()

    async def _show_main_menu(self) -> dict[str, Any]:
        entry = self.config_entry
        counts = _counts(entry)
        placeholders = {
            f"{section}_count": str(counts[subentry_type])
            for section, subentry_type in _SECTION_TYPE.items()
        }
        # `health_summary` is Task 4's "at a glance" requirement: shown right
        # on the `check_matrix` menu option's own label (see strings.json),
        # not only after opening that screen -- see `_health_summary_text`'s
        # own docstring for why it is safe to recompute on every render.
        placeholders["health_summary"] = _health_summary_text(entry)
        placeholders["execution_summary"] = _execution_summary_text(entry)
        return self.async_show_menu(
            step_id="init", menu_options=_MAIN_MENU_OPTIONS, description_placeholders=placeholders
        )

    # -- Per-section list menu ------------------------------------------

    async def async_step_blinds(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """List menu for `blind` subentries -- see `_enter_section`."""
        return await self._enter_section("blinds")

    async def async_step_zones(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """List menu for `zone` subentries -- see `_enter_section`."""
        return await self._enter_section("zones")

    async def async_step_values(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """List menu for `value` subentries -- see `_enter_section`."""
        return await self._enter_section("values")

    async def async_step_conditions(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """List menu for `condition` subentries -- see `_enter_section`."""
        return await self._enter_section("conditions")

    async def async_step_modes(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """List menu for `mode` subentries -- see `_enter_section`."""
        return await self._enter_section("modes")

    async def async_step_rules(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """List menu for `rule` subentries -- see `_enter_section`."""
        return await self._enter_section("rules")

    async def _enter_section(self, section: str) -> dict[str, Any]:
        """Record which section is now active and show its list menu.

        Clears the per-item state left over from a previous visit
        (`_pending_id`, `_rule_pick`, `_zone_pick`) -- a user who backs out
        of an edit in one section and opens a different one must not carry
        that pick along.
        """
        self._section = section
        self._pending_id = None
        self._rule_pick = None
        self._zone_pick = None
        return await self._show_section_menu()

    async def _show_section_menu(self) -> dict[str, Any]:
        entry = self.config_entry
        subentry_type = _SECTION_TYPE[self._section]
        items = _items(entry, subentry_type)
        options = ["add"]
        # `list` (a read-only "what will the house actually do" report) and
        # `zone` (one `(mode, zone)` pair's own screen, own rules and
        # inherited defaults together -- see `async_step_zone_rules`) only
        # ever make sense for `rules` -- see `_rule_overview_text`'s own
        # docstring for why order and grouping are the whole point there in
        # a way they are not for the other five, simpler types, whose own
        # titles (shown in the `edit`/`remove` picker) already say everything
        # a summary would.
        if self._section == _RULES_SECTION and items:
            options.append("list")
            options.append("zone")
        if items:
            options.extend(["edit", "remove"])
        options.append("back")
        return self.async_show_menu(
            step_id=self._section,
            menu_options=options,
            description_placeholders={"count": str(len(items))},
        )

    async def async_step_back(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return to the main menu from any section's list menu."""
        self._section = None
        self._pending_id = None
        self._rule_pick = None
        self._zone_pick = None
        return await self._show_main_menu()

    # -- Rules: read-only evaluation-order report --------------------------

    async def async_step_list(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Show every rule subentry the way the engine will actually evaluate it.

        Only ever reached from the `rules` section menu (see
        `_show_section_menu`) -- there is nothing type-specific about the
        step itself, but `_rule_overview_text` only knows how to render a
        rule's `then`, so this is not offered for the other five types.
        Read-only: the one field this shows is submitted purely to return to
        the section menu, the same "empty form as an acknowledgement" shape
        `async_step_check_matrix` already uses for the same reason.
        """
        if user_input is not None:
            return await self._show_section_menu()
        result = _rule_overview_text(self.config_entry)
        return self.async_show_form(
            step_id="list", data_schema=vol.Schema({}), description_placeholders={"result": result}
        )

    # -- Rules: one zone's own screen, own rules and inherited defaults ----
    #
    # `list` above answers "what does the whole configuration do"; this
    # answers "what does *this* zone do" -- the question a user actually has
    # once a mode's defaults exist, since a zone with no rules of its own is
    # invisible to a picker built only from subentries that name it (see
    # `config_store.effective_rule_items`'s own docstring). Two steps for the
    # same "pick the pair, then act" shape `add` already uses for `rule`:
    # `zone` picks `(mode, zone)` (reusing `subentry_flow._rule_pick_schema`
    # unchanged -- the same question the rule-add wizard's own first step
    # asks, extended by phase 6 task 1 to also offer `"*"`, so this screen
    # doubles as "show me this mode's own default list" for free); `zone_
    # rules` shows that pair's effective list and, for a row a user picks,
    # either opens it for editing (own) or refuses with a route onward
    # (inherited) -- never silently no-ops, per the task brief: an inherited
    # rule belongs to the mode, and this screen is scoped to one zone, so it
    # is not this screen's save to make.

    async def async_step_zone(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pick the `(mode, zone)` pair whose effective rule list to look at."""
        entry = self.config_entry
        if user_input is None:
            return self.async_show_form(step_id="zone", data_schema=_rule_pick_schema(entry))
        self._zone_pick = dict(user_input)
        return await self.async_step_zone_rules(None)

    async def async_step_zone_rules(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Show one `(mode, zone)` pair's effective rule list; block editing an inherited row.

        `items` (`config_store.effective_rule_items`) is this pair's own
        rules, then the mode's shared defaults, in the exact order
        `engine._apply_rules` would try them -- the same function `_rule_
        overview_text`'s per-pair blocks read from, so this screen and that
        report cannot disagree about what "this zone's effective list" is.

        Picking an inherited (`is_default`) row is refused, not silently
        ignored and not passed through to a real edit: the row is a real
        subentry, but it belongs to the mode's default group, not to this
        zone, and letting this screen save it would let one zone's edit
        change every other zone that inherits the same row without saying
        so. The error message is this screen's route onward -- pick `"*"` as
        the zone here (or from `zone`'s own picker) to reach the exact same
        row as its actual owner.

        Picking an own row edits it in place (`async_step_edit_form`, the
        same generic edit machinery every other type already uses) or, with
        `confirm` ticked, removes it -- both act on the real subentry id, so
        nothing here is a second copy of what `edit`/`remove` already do,
        only a different, zone-scoped way to reach the same subentry.
        """
        entry = self.config_entry
        mode_id = self._zone_pick[_RULE_MODE_FIELD]
        zone_id = self._zone_pick[_RULE_ZONE_FIELD]
        items = effective_rule_items(entry, mode_id, zone_id)
        placeholders = {"mode": mode_id, "zone": zone_id}

        if not items:
            if user_input is not None:
                return await self._show_section_menu()
            return self.async_show_form(
                step_id="zone_rules",
                data_schema=vol.Schema({}),
                description_placeholders=placeholders,
            )

        errors: dict[str, str] = {}
        if user_input is not None:
            subentry_id = user_input[_PICK_FIELD]
            # `items` is recomputed fresh on every call, but Home Assistant
            # validates the submitted `subentry_id` against the *schema*
            # cached when the form was rendered (`_zone_rule_schema`'s own
            # options) -- if the picked rule was removed in between (another
            # browser tab, an `import_config` call, the same user
            # navigating elsewhere), the pick is still schema-valid but no
            # longer in `items`. `next()` with no default used to raise
            # `StopIteration` here, which a coroutine turns into an
            # unhandled `RuntimeError` -- an HTTP 500 instead of a form
            # error. Treat "the rule is gone" as a normal outcome, the same
            # way `rule_is_inherited` already is, rather than crashing.
            match = next(
                ((sid, is_default) for sid, _data, is_default in items if sid == subentry_id),
                None,
            )
            if match is None:
                errors["base"] = "rule_no_longer_exists"
            else:
                _sid, is_default = match
                if is_default:
                    errors["base"] = "rule_is_inherited"
                elif user_input.get(_CONFIRM_FIELD):
                    self.hass.config_entries.async_remove_subentry(entry, subentry_id)
                    return await self._show_section_menu()
                else:
                    self._pending_id = subentry_id
                    return await self.async_step_edit_form(None)

        schema = self.add_suggested_values_to_schema(_zone_rule_schema(entry, items), user_input)
        return self.async_show_form(
            step_id="zone_rules",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    # -- Add ------------------------------------------------------------

    async def async_step_add(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Add a new subentry of the active section's type.

        Every type but `rule` is one form, handled by `_render_type_form`
        directly. `rule` is two forms -- see `RuleSubentryFlowHandler`'s own
        docstring for why the default `order` cannot be known until the
        `(mode, zone)` pair is picked -- so this step is `rule`'s picker, and
        `async_step_add_rule_fields` is its second half.
        """
        subentry_type = _SECTION_TYPE[self._section]
        if subentry_type != RULE:
            return await self._render_type_form(user_input, subentry_id=None, step_id="add")

        if user_input is None:
            schema = _rule_pick_schema(self.config_entry)
            return self.async_show_form(step_id="add", data_schema=schema)

        self._rule_pick = dict(user_input)
        return await self.async_step_add_rule_fields(None)

    async def async_step_add_rule_fields(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Second half of adding a rule: the rule itself, `order` pre-filled."""
        return await self._render_type_form(user_input, subentry_id=None, step_id="add_rule_fields")

    # -- Edit -------------------------------------------------------------

    async def async_step_edit(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pick which existing subentry of the active section to edit."""
        entry = self.config_entry
        subentry_type = _SECTION_TYPE[self._section]
        if user_input is not None:
            self._pending_id = user_input[_PICK_FIELD]
            return await self.async_step_edit_form(None)
        return self.async_show_form(
            step_id="edit", data_schema=_pick_schema(_items(entry, subentry_type))
        )

    async def async_step_edit_form(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The picked subentry's own edit form."""
        return await self._render_type_form(
            user_input, subentry_id=self._pending_id, step_id="edit_form"
        )

    # -- Remove -----------------------------------------------------------

    async def async_step_remove(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pick an existing subentry of the active section and confirm removing it.

        Picker and confirmation are one form, not two -- unlike edit, there
        is no second, type-specific screen to show once the target is known,
        so a second step would exist only to ask "are you sure" and could be
        collapsed into the same one instead.
        """
        entry = self.config_entry
        subentry_type = _SECTION_TYPE[self._section]
        items = _items(entry, subentry_type)
        errors: dict[str, str] = {}
        if user_input is not None:
            # Home Assistant validates the submitted id against the schema it
            # cached when this form was rendered, so a pick stays "valid" after
            # the subentry itself is gone -- removed in another tab, by
            # `import_config`, or by this same user elsewhere. Handing that id
            # to `async_remove_subentry` raises `UnknownSubEntry`, which reaches
            # the user as a 500 rather than as something the form can say. The
            # same race is guarded in `async_step_zone_rules` and
            # `_render_type_form`; this was the third door into it.
            if user_input[_PICK_FIELD] not in entry.subentries:
                errors["base"] = "subentry_vanished"
            elif user_input.get(_CONFIRM_FIELD):
                self.hass.config_entries.async_remove_subentry(entry, user_input[_PICK_FIELD])
                return await self._show_section_menu()
            else:
                errors["base"] = "confirmation_required"

        schema = _pick_schema(items).extend(
            {vol.Required(_CONFIRM_FIELD, default=False): selector.BooleanSelector()}
        )
        schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="remove", data_schema=schema, errors=errors)

    # -- Shared add/edit form rendering ------------------------------------

    async def _render_type_form(
        self, user_input: dict[str, Any] | None, *, subentry_id: str | None, step_id: str
    ) -> dict[str, Any]:
        """Render, validate and save one type's add/edit form.

        The one caller of every `_SubentryFlowBase` subclass's shared
        schema/data/validation methods -- see the module docstring's "one
        owner, two doors" section. Mirrors `_SubentryFlowBase._step`'s own
        control flow deliberately (same order of operations: local problems,
        then `_to_data`, then `_blocking_errors`, then either save or
        re-show with an error) so the two doors behave identically from a
        user's point of view; what differs is only what "save" means here --
        `hass.config_entries.async_add_subentry`/`async_update_subentry`
        followed by a return to the section menu, in place of finishing the
        flow outright.
        """
        entry = self.config_entry
        subentry_type = _SECTION_TYPE[self._section]
        handler = SUBENTRY_FLOW_HANDLERS[subentry_type]()
        if subentry_type == RULE:
            # `RuleSubentryFlowHandler._initial_values` reads `self._pick` to
            # prefill step two from step one's chosen `(mode, zone)`; `None`
            # here (an edit, which never runs the picker) leaves it unused.
            # Leading-underscore access across modules, deliberately: these
            # are the shared schema/data/validation methods the module
            # docstring's "one owner, two doors" section describes, not an
            # accidental reach into `subentry_flow.py`'s own private API.
            handler._pick = self._rule_pick  # noqa: SLF001
        subentry = entry.subentries.get(subentry_id) if subentry_id is not None else None

        if subentry_id is not None and subentry is None:
            # `subentry_id` was picked in an earlier step (`async_step_edit`'s
            # own picker, or `async_step_zone_rules`'s own-row edit route),
            # from a list that was accurate at the time -- but `entry.
            # subentries` is read fresh right here, and a concurrent flow may
            # have removed that subentry since. The plain `entry.subentries
            # [subentry_id]` this replaced would `KeyError` in that case --
            # the same "cached schema, stale id" race `async_step_zone_
            # rules`'s own `next()` guards against, just reached through a
            # dict index instead. There is nothing left to edit, so say so
            # (the same "empty form as acknowledgement" shape `async_step_
            # zone_rules`'s own "no items" branch already uses) instead of
            # crashing; submitting it is what actually leaves, so a second,
            # unrelated flow re-picking the same stale id cannot loop here.
            if user_input is not None:
                self._pending_id = None
                return await self._show_section_menu()
            return self.async_show_form(
                step_id=step_id, data_schema=vol.Schema({}), errors={"base": "subentry_vanished"}
            )

        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None

        if user_input is not None:
            problems = handler._local_problems(user_input)  # noqa: SLF001
            data: dict[str, Any] | None = None
            if not problems:
                data = handler._to_data(entry, user_input)  # noqa: SLF001
                candidate_id = subentry_id if subentry_id is not None else _NEW_SUBENTRY_ID
                problems = handler._blocking_errors(entry, candidate_id, data)  # noqa: SLF001
            if not problems:
                title = handler._title(data)  # noqa: SLF001
                if subentry is None:
                    self.hass.config_entries.async_add_subentry(
                        entry,
                        ConfigSubentry(
                            data=data, subentry_type=subentry_type, title=title, unique_id=None
                        ),
                    )
                else:
                    self.hass.config_entries.async_update_subentry(
                        entry, subentry, data=data, title=title
                    )
                self._rule_pick = None
                self._pending_id = None
                return await self._show_section_menu()

            errors["base"] = "invalid_config"
            placeholders = {"error_detail": "; ".join(problems)}

        current = (
            user_input
            if user_input is not None
            else (
                handler._to_form_values(subentry.data)  # noqa: SLF001
                if subentry is not None
                else handler._initial_values(entry)  # noqa: SLF001
            )
        )
        schema = self.add_suggested_values_to_schema(handler._build_schema(entry), current)  # noqa: SLF001
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    # -- Import / export ---------------------------------------------------

    async def async_step_import_export(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call the existing `import_config`/`export_config` handlers, never reimplement them.

        Both services already do every check this project's own `MODELS.md`
        requires (validate before writing, refuse a symlinked export target,
        refuse to merge onto an existing configuration without `overwrite`);
        this step's only job is to collect their fields and hand them to the
        exact same handler functions the registered services wrap
        (`services._async_import_config`/`_async_export_config`).

        Deliberately *not* a dispatch through `hass.services` by domain and
        service name, even though that would look like the more natural way to
        "wire it to the service". It calls the identical handler function the
        registered service wraps, coerced through that service's own
        `vol.Schema` first so the two paths validate input identically -- which
        reuses the service without adding a second, name-resolved way to reach
        it. (The original reason was the phase-2 no-movement guard, which
        banned every `services.async_call` site in the package; that guard is
        gone, and only `coordinator._build_runner` calls a service now.)
        """
        errors: dict[str, str] = {}
        placeholders: dict[str, str] | None = None

        if user_input is not None:
            action = user_input[_ACTION_FIELD]
            raw_data: dict[str, Any] = {ATTR_PATH: user_input[ATTR_PATH]}
            if action == _ACTION_IMPORT:
                raw_data[ATTR_DRY_RUN] = user_input.get(ATTR_DRY_RUN, False)
                raw_data[ATTR_OVERWRITE] = user_input.get(ATTR_OVERWRITE, False)
                schema, handler = IMPORT_CONFIG_SCHEMA, _async_import_config
            else:
                schema, handler = EXPORT_CONFIG_SCHEMA, _async_export_config
            try:
                call_data = schema(raw_data)
                await handler(self.hass, _ServiceCallData(call_data))
            except (vol.Invalid, HomeAssistantError) as err:
                errors["base"] = "import_export_failed"
                placeholders = {"error_detail": _describe_error(err)}
            else:
                return await self._show_main_menu()

        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required(_ACTION_FIELD, default=_ACTION_IMPORT): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[_ACTION_IMPORT, _ACTION_EXPORT], sort=False
                        )
                    ),
                    vol.Required(ATTR_PATH, default=DEFAULT_CONFIG_PATH): selector.TextSelector(),
                    vol.Optional(ATTR_DRY_RUN, default=False): selector.BooleanSelector(),
                    vol.Optional(ATTR_OVERWRITE, default=False): selector.BooleanSelector(),
                }
            ),
            user_input,
        )
        return self.async_show_form(
            step_id="import_export",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    # -- Execution mode (the one screen that writes `entry.options`) --------

    async def async_step_execution(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Turn `runner.py`'s hands on or off, without a reload.

        The only screen in this flow that writes `ConfigEntry.options` rather
        than a subentry -- see this module's own docstring and
        `const.OPT_DRY_RUN` for why this one switch belongs there. The write is
        a merge into whatever options already exist, not a replacement: a
        future second option must not be erased by someone toggling this one.
        """
        entry = self.config_entry
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry,
                options={**dict(entry.options), OPT_DRY_RUN: bool(user_input[OPT_DRY_RUN])},
            )
            return await self._show_main_menu()

        current = entry.options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN)
        return self.async_show_form(
            step_id="execution",
            data_schema=vol.Schema(
                {vol.Required(OPT_DRY_RUN, default=bool(current)): selector.BooleanSelector()}
            ),
        )

    # -- Check against the old matrix --------------------------------------

    async def async_step_check_matrix(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The full health report: static problems, drift from the fixture, last recompute.

        Task 4's deepening of what used to be only a fixture-drift check
        (see the git history of this method's own docstring for that
        earlier, narrower version): three independent questions, each
        answered by reading the one place that already owns the answer,
        never by a second, form-shaped re-implementation of any of them.

        - **Validation** (`_current_problems`/`_validation_report_text`):
          reads `validation.validate` and `config_store.
          duplicate_rule_order_problems` -- together, the exact pipeline
          `_SubentryFlowBase._blocking_errors` already runs before a save --
          and reports counts by severity plus one attributed line per
          problem, from `Problem.owners` (see `_owner_text`), rather than
          just "ok"/"not ok". That attribution is the whole point: a count
          with nowhere to click is not "findable" by this task's own
          standard, so each line names the `(subentry_type, id)` the
          Rules/Conditions/Modes sections above already list things by,
          which is where that same problem is fixed.
        - **Conformance** (`diff_configs`/`repo_fixture_path`): the exact
          comparison `__init__.py`'s own `fixture_drift` repair issue
          already uses.
        - **Last recompute** (`_coordinator_status_text`): reads
          `CoverLogicCoordinator.last_success`/`last_error` directly off
          `entry.runtime_data`, never re-evaluates the engine itself here.

        All three read the entry as it stands right now, not a stale copy;
        parsing (`config_from_subentries`) happens once, not once per
        section, so a parse failure is reported consistently across all
        three rather than the conformance section re-deriving its own
        "could not be read" text independently of `_current_problems`.
        """
        if user_input is not None:
            return await self._show_main_menu()

        entry = self.config_entry
        config, problems = _current_problems(entry)
        validation_text = _validation_report_text(problems)
        conformance_text = (
            await self._conformance_text(config)
            if config is not None
            else "not checked -- the configuration does not parse (see Validation above)"
        )

        result = (
            f"Validation: {validation_text}\n\n"
            f"Conformance with fixtures/dom_peter.yaml: {conformance_text}\n\n"
            f"Recomputation: {_coordinator_status_text(entry)}"
        )
        return self.async_show_form(
            step_id="check_matrix",
            data_schema=vol.Schema({}),
            description_placeholders={"result": result},
        )

    async def _conformance_text(self, config: Config) -> str:
        """Fixture-drift half of `async_step_check_matrix`'s report, given an already-parsed config.

        Split out so parsing (`config_from_subentries`) happens exactly once
        per render -- see that method's own docstring.
        """
        fixture = repo_fixture_path()
        if fixture is None:
            return (
                "This installation ships no fixtures/dom_peter.yaml to compare "
                "against -- nothing to check here."
            )
        try:
            reference = await self.hass.async_add_executor_job(load_config_file, str(fixture))
        except (ConfigError, OSError) as err:
            return f"fixtures/dom_peter.yaml could not be read: {err}"
        diff = diff_configs(config, reference)
        return (
            "Matches fixtures/dom_peter.yaml exactly."
            if not diff
            else f"Differs from fixtures/dom_peter.yaml in: {', '.join(diff)}."
        )
