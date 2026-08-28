"""The options flow: one main menu over the same subentries the six subentry flows edit.

**Why this exists.** Phase 4 gave every configuration type its own subentry
flow (`config_flow.py`), but the integration had `supports_options: false`
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

**One owner, two doors.** `config_flow.py`'s six `_SubentryFlowBase`
subclasses (`BlindSubentryFlowHandler`, ...) already separate "what this
type's form looks like and how a submission becomes subentry data" (`_build_
schema`, `_to_data`, `_to_form_values`, `_initial_values`, `_local_problems`,
`_candidate_id`, `_title`, `_blocking_errors`) from "how a `ConfigSubentryFlow`
step turns that into a rendered form and a finished flow" (`_step`,
`async_step_user`/`async_step_reconfigure`). Every method in the first group
needs nothing from `self` beyond the `entry` it is explicitly handed --
`_to_data` was the one exception (`RuleSubentryFlowHandler` read
`self._get_entry()`), fixed as part of this task so the whole group could be
called from a bare instance of the class, never registered with Home
Assistant's flow manager at all. `_render_type_form` below is this module's
one and only caller of that group, for every one of the six types; it is the
"two doors" arrangement the task brief asks for, made possible by that one
constructor-independence property rather than by copying a single field
label, schema or validation rule. What *is* necessarily different between
the two doors -- what happens once a submission validates -- cannot be
shared: a subentry flow step finishes the flow (`async_create_entry`/
`async_update_and_abort`), while this flow keeps going (`hass.config_entries.
async_add_subentry`/`async_update_subentry`, then back to the section menu).
That handful of lines is genuinely different glue, not a second copy of a
form.

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

**Never touches `entry.options`.** This flow's whole point is to mutate
*subentries*, which live independently of `ConfigEntry.options`. It never
calls `self.async_create_entry(...)`; every screen either shows another
form/menu or loops back to one, so the flow simply stays open (as any
menu-driven UI does) until the user closes it -- Home Assistant's own
generic "flow aborted by the user" handling, needing no strings.json entry
of this integration's own.
"""

from typing import Any

from homeassistant.config_entries import ConfigSubentry, OptionsFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
import voluptuous as vol

from .config_flow import _NEW_SUBENTRY_ID, SUBENTRY_FLOW_HANDLERS, _axis_to_form, _rule_pick_schema
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
)
from .conformance import diff_configs, repo_fixture_path
from .const import DEFAULT_CONFIG_PATH
from .services import (
    ATTR_DRY_RUN,
    ATTR_OVERWRITE,
    ATTR_PATH,
    EXPORT_CONFIG_SCHEMA,
    IMPORT_CONFIG_SCHEMA,
    _async_export_config,
    _async_import_config,
)

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

_MAIN_MENU_OPTIONS = [*_SECTION_TYPE, "import_export", "check_matrix"]

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

    Reuses `config_flow._axis_to_form` -- the exact function
    `RuleSubentryFlowHandler._title` already calls to build a single rule
    subentry's own title -- rather than a second axis-rendering rule that
    could show `keep` or a `!ref` differently from what that title already
    promises. `then` is a raw rule subentry's `"then"` mapping (`{"position":
    ..., "tilt": ...}`), the same shape `_grouped_rules` hands back unparsed.
    """
    return f"position={_axis_to_form(then.get('position'))}, tilt={_axis_to_form(then.get('tilt'))}"


def _rule_overview_text(entry: Any) -> str:
    """Every rule subentry as the house will actually evaluate it: grouped, ordered, visible.

    Reads `config_store._grouped_rules` directly -- the one place that groups
    rule subentries by `(mode, zone)` and sorts them by `(order, subentry id)`
    -- and renders its output as-is, in the order it comes back. Nothing here
    re-groups or re-sorts: see that function's own "One grouping, not two"
    docstring section, and `_rule_items` above, which already makes the same
    choice for the edit picker. Within a `(mode, zone)` pair the engine tries
    rules in exactly this order and stops at the first match, so a summary
    that reordered them -- even to look tidier, e.g. alphabetically by pair --
    would show a house behaviour that is not the one that actually runs.
    """
    groups = _grouped_rules(entry)
    if not groups:
        return "No rules configured yet."

    lines: list[str] = []
    for key, items in groups.items():
        lines.append(f"{key}:")
        for _subentry_id, data in items:
            then = data.get("then") or {}
            line = f"  {data.get('order')}: {_rule_action_text(then)}"
            name = data.get("name")
            if name:
                line = f"{line} ({name})"
            lines.append(line)
    return "\n".join(lines)


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
    already uses in `config_flow.py` -- a fresh instance is created per flow
    (`CoverLogicConfigFlow.async_get_options_flow`), so nothing here is
    shared across two users' flows.
    """

    _section: str | None = None
    _pending_id: str | None = None
    _rule_pick: dict[str, Any] | None = None

    # -- Main menu -----------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Entry point Home Assistant always dispatches to first."""
        return await self._show_main_menu()

    async def _show_main_menu(self) -> dict[str, Any]:
        counts = _counts(self.config_entry)
        placeholders = {
            f"{section}_count": str(counts[subentry_type])
            for section, subentry_type in _SECTION_TYPE.items()
        }
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
        (`_pending_id`, `_rule_pick`) -- a user who backs out of an edit in
        one section and opens a different one must not carry that pick
        along.
        """
        self._section = section
        self._pending_id = None
        self._rule_pick = None
        return await self._show_section_menu()

    async def _show_section_menu(self) -> dict[str, Any]:
        entry = self.config_entry
        subentry_type = _SECTION_TYPE[self._section]
        items = _items(entry, subentry_type)
        options = ["add"]
        # `list` (a read-only "what will the house actually do" report) only
        # ever makes sense for `rules` -- see `_rule_overview_text`'s own
        # docstring for why order and grouping are the whole point there in
        # a way they are not for the other five, simpler types, whose own
        # titles (shown in the `edit`/`remove` picker) already say everything
        # a summary would.
        if self._section == _RULES_SECTION and items:
            options.append("list")
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
            if user_input.get(_CONFIRM_FIELD):
                self.hass.config_entries.async_remove_subentry(entry, user_input[_PICK_FIELD])
                return await self._show_section_menu()
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
            # accidental reach into `config_flow.py`'s own private API.
            handler._pick = self._rule_pick  # noqa: SLF001
        subentry = entry.subentries[subentry_id] if subentry_id is not None else None

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
        service name, even though that would look like the more natural way
        to "wire it to the service": `tests/test_no_movement.py` still bans
        every such dispatch site anywhere under `custom_components/
        cover_logic/`, unconditionally, until phase 3 gives this integration
        "hands" -- see that test module's own docstring. That guard does not
        distinguish a cover-moving service from this integration's own
        already-reviewed import/export services; calling the identical
        handler function the registered service wraps -- coerced through
        that service's own `vol.Schema` first, so the two paths validate
        input identically -- satisfies both this task's "reuse the service,
        don't reimplement it" requirement and that still-binding guard.
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

    # -- Check against the old matrix --------------------------------------

    async def async_step_check_matrix(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Report whether the live subentries still match `fixtures/dom_peter.yaml`.

        Reads `conformance.diff_configs`/`repo_fixture_path` -- the exact
        comparison `__init__.py`'s own `fixture_drift` repair issue already
        uses -- rather than a second, form-shaped re-implementation of "do
        these two configs mean the same thing". A richer health overview
        (`validate()`'s own findings, clickable to the offending subentry) is
        this plan's Task 4, not this one; this screen only answers the one
        question its menu label promises.
        """
        if user_input is not None:
            return await self._show_main_menu()

        entry = self.config_entry
        try:
            config = config_from_subentries(entry)
        except ConfigError as err:
            result = f"The current configuration could not be read: {err}"
        else:
            fixture = repo_fixture_path()
            if fixture is None:
                result = (
                    "This installation ships no fixtures/dom_peter.yaml to compare "
                    "against -- nothing to check here."
                )
            else:
                try:
                    reference = await self.hass.async_add_executor_job(
                        load_config_file, str(fixture)
                    )
                except (ConfigError, OSError) as err:
                    result = f"fixtures/dom_peter.yaml could not be read: {err}"
                else:
                    diff = diff_configs(config, reference)
                    result = (
                        "Matches fixtures/dom_peter.yaml exactly."
                        if not diff
                        else f"Differs from fixtures/dom_peter.yaml in: {', '.join(diff)}."
                    )

        return self.async_show_form(
            step_id="check_matrix",
            data_schema=vol.Schema({}),
            description_placeholders={"result": result},
        )
