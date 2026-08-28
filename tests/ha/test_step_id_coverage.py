"""Guard: every `step_id` a flow in this package can render must be dispatchable.

**The bug class this exists to catch.** Home Assistant's real `FlowManager`
checks, on *every* step it dispatches -- including the very call that renders
a form or a menu, not only a resubmission -- that `result["step_id"]` names a
real `async_step_<id>` method (`FlowManager._raise_if_step_does_not_exist`,
called from `_async_handle_step`). A screen built as `self.async_show_form
(step_id="some_name", ...)` from inside a *different* `async_step_<other>`
method, where no `async_step_some_name` exists, passes that check the moment
a real installation renders it -- `UnknownStep`. Every other test module
under `tests/ha/` drives flow methods directly (`asyncio.run(flow.
async_step_x(...))`), by design and for good reason (a working domain
`FlowManager` -- `ConfigEntriesFlowManager`/`OptionsFlowManager`/
`ConfigSubentryFlowManager` -- needs a real, running `HomeAssistant` with
config-entry storage and the loader; see e.g. `test_config_flow.py`'s own
module docstring), which means none of them ever go through
`_raise_if_step_does_not_exist` at all. Exactly this happened once: a rendered
`step_id="example_not_available"` had no matching method, and the whole
method-calling test suite stayed green because nothing in it used the
manager. Caught by reasoning, fixed by giving that screen its own
`async_step_example_not_available` -- but nothing stopped the same mistake
recurring at the next screen, since the fix was local to that one call site.

**Why this drives the real `FlowManager` instead of a hand-written `hasattr`
sweep.** A `step_id` in this package's source is sometimes a literal
(`step_id="edit"`), sometimes a module-level constant (`step_id=_STEP_EMPTY`
in `config_flow.py`), and sometimes a runtime value read off `self`
(`step_id=self._section` in `options_flow.py`, resolved only once a
particular section has been entered). Statically finding every `step_id=`
call site and resolving each of those three shapes back to a string --
without executing anything -- means reimplementing a chunk of Python's own
name resolution, and would still only be as good as this test's own copy of
that logic; a future refactor could change how a `step_id` is produced
without this guard's static walk keeping up. Actually driving the flows
through `homeassistant.data_entry_flow.FlowManager` sidesteps all three cases
at once: whatever expression a step_id is, the manager only ever sees the
resulting string, and checks it with Home Assistant's own, real
`_raise_if_step_does_not_exist` -- the exact function that will run against
this code the day it is installed for real. That is "the root" the task
brief asks for, rather than a second, hand-rolled copy of the same check
that could itself drift from what Home Assistant actually enforces.

**Why the generic base `FlowManager`, not `ConfigEntriesFlowManager`/
`OptionsFlowManager`/`ConfigSubentryFlowManager`.** `_async_handle_step` and
`_raise_if_step_does_not_exist` -- the two methods this guard cares about --
are defined once, on the base `homeassistant.data_entry_flow.FlowManager`,
and inherited unchanged by all three domain managers (verified against
`homeassistant==2026.8.0`: neither method is overridden anywhere in
`homeassistant/config_entries.py`). The three domain managers each add is
config-entry storage, single-instance checks, the integration loader --
concerns this project's own step logic does not touch and that every other
`tests/ha/` module already avoids building for exactly this reason (see
`test_config_flow.py`'s module docstring: "disproportionate for exercising
one step's logic"). `_CheckingFlowManager` below is a minimal concrete
`FlowManager` -- it implements only the two methods `FlowManager` itself
leaves abstract (`async_create_flow`, `async_finish_flow`) -- wired to one
already-built flow instance, so it gets the real dispatch-and-check behaviour
for free without any of that.

**What this does not (and cannot) prove.** This walks the journeys each
flow's own menus and forms actually offer -- the same journeys the rest of
`tests/ha/` already covers by calling methods directly -- so a step reachable
only through a path this file does not exercise stays uncaught, same as any
other test. What it adds is not more reachability, but a different, stronger
check applied to everything it *does* reach: the literal mechanism Home
Assistant itself uses, in place of a method call that could never have
noticed a dangling `step_id` in the first place.
"""

import asyncio

import pytest

pytest.importorskip("homeassistant")

from homeassistant.data_entry_flow import FlowManager

from cover_logic.config_flow import SUBENTRY_FLOW_HANDLERS, CoverLogicConfigFlow
from cover_logic.config_store import BLIND, CONDITION, MODE, RULE, VALUE, ZONE
from cover_logic.const import DOMAIN
from cover_logic.options_flow import _SECTION_TYPE, CoverLogicOptionsFlow

_ENTRY_ID = "entry1"


class _CheckingFlowManager(FlowManager):
    """A real `FlowManager`, wired to one already-built flow instance.

    `async_create_flow`/`async_finish_flow` are the only two methods
    `FlowManager` itself leaves abstract; neither is where this guard's
    behaviour lives (that is `_async_handle_step`, inherited unchanged --
    see the module docstring). `async_create_flow` mirrors the one line of
    real substance every domain manager's own override has in common
    (`flow.init_step = context["source"]`, see `ConfigEntriesFlowManager`/
    `ConfigSubentryFlowManager` in `homeassistant/config_entries.py`) so a
    caller only has to pick the right `context` -- `{"source": "user"}` or
    `{"source": "reconfigure", ...}` for a config/subentry flow, `{}` for an
    options flow, whose own manager never sets `init_step` at all and so
    keeps `FlowHandler`'s default of `"init"`, matching `CoverLogicOptions
    Flow.async_step_init` -- the same convention the rest of `tests/ha/`
    already follows when it sets `flow.context` by hand.
    """

    def __init__(self, hass, flow):
        """Wrap `flow`; `hass` is never touched beyond being stored (see base class)."""
        super().__init__(hass)
        self._flow = flow

    async def async_create_flow(self, handler, *, context, data):
        """Hand back the one wrapped flow, first setting `init_step` from `context["source"]`."""
        if context and "source" in context:
            self._flow.init_step = context["source"]
        return self._flow

    async def async_finish_flow(self, flow, result):
        """Return `result` unchanged -- there is no entry/subentry storage to write it into."""
        return result


class _Session:
    """Drives one flow instance through `_CheckingFlowManager`'s real dispatch.

    `start()`/`configure()`/`choose()` mirror exactly what a real frontend
    does (`FlowManager.async_init`/`async_configure`, the latter's own
    `{"next_step_id": ...}` handling for a menu choice -- see
    `FlowManager._async_configure`'s own source, quoted by this project's
    other tests for the same reason). Every call goes through the public
    `async_init`/`async_configure`, never straight at `_async_handle_step`,
    so the flow's `flow_id` gets registered in the manager's own progress
    bookkeeping exactly the way a real flow's does -- calling the private
    method directly would skip that and risk a spurious `UnknownFlow`
    unrelated to what this guard checks.
    """

    def __init__(self, hass, flow, *, handler, context):
        """Wrap `flow`; `handler`/`context` are handed to every `async_init` call."""
        self._manager = _CheckingFlowManager(hass, flow)
        self._handler = handler
        self._context = context
        self.result = None

    def start(self, data=None):
        """(Re)start the flow at its `init_step`, as `context["source"]` selects it."""
        self.result = asyncio.run(
            self._manager.async_init(self._handler, context=self._context, data=data)
        )
        return self.result

    def configure(self, user_input=None):
        """Submit `user_input` to whatever step is currently shown."""
        self.result = asyncio.run(self._manager.async_configure(self.result["flow_id"], user_input))
        return self.result

    def choose(self, next_step_id):
        """Pick `next_step_id` off a rendered menu."""
        return self.configure({"next_step_id": next_step_id})


# ---------------------------------------------------------------------------
# Options flow: the main menu, every section's add/edit/remove, the rule
# two-step wizard, import/export and check_matrix.
# ---------------------------------------------------------------------------


def test_options_flow_every_reachable_step_id_is_dispatchable(subentry_entry, options_hass):
    """Walks the same journeys `test_options_flow.py` drives directly, through
    the real manager instead -- see the module docstring for why that
    difference is the whole point. In particular, submitting the rule
    wizard's first step (mode+zone) makes `async_step_add` delegate to
    `async_step_add_rule_fields`, and submitting the edit picker makes
    `async_step_edit` delegate to `async_step_edit_form` -- the exact shape
    of bug (a render from inside a *different* step) that escaped every
    method-calling test before this file existed.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    entry.add_subentry(ZONE, {"id": "z", "members": ["cover.a"]})
    entry.add_subentry(VALUE, {"id": "v", "entity": "input_number.x", "default": 0})
    entry.add_subentry(
        CONDITION, {"id": "c", "condition": "state", "entity_id": "x", "state": "on"}
    )
    entry.add_subentry(MODE, {"id": "m", "order": 0})
    entry.add_subentry(
        RULE, {"mode": "m", "zone": "z", "order": 0, "then": {"position": "keep", "tilt": "keep"}}
    )
    hass = options_hass(entry)

    for section in _SECTION_TYPE:
        session = _Session(hass, CoverLogicOptionsFlow(), handler=_ENTRY_ID, context={})
        session.start()
        session.choose(section)
        session.choose("add")
        if section == "rules":
            session.configure({"mode": "m", "zone": "z"})

        if section == "rules":
            session.start()
            session.choose(section)
            session.choose("list")

        session.start()
        session.choose(section)
        session.choose("edit")
        subentry_type = _SECTION_TYPE[section]
        existing_id = next(
            sid for sid, sub in entry.subentries.items() if sub.subentry_type == subentry_type
        )
        session.configure({"subentry_id": existing_id})

        session.start()
        session.choose(section)
        session.choose("remove")

        session.start()
        session.choose(section)
        session.choose("back")

    for step in ("import_export", "check_matrix"):
        session = _Session(hass, CoverLogicOptionsFlow(), handler=_ENTRY_ID, context={})
        session.start()
        session.choose(step)


# ---------------------------------------------------------------------------
# The top-level config flow: the setup menu and all four branches, including
# the "no example shipped" fallback -- the exact regression fixed once already.
# ---------------------------------------------------------------------------


def test_config_flow_every_reachable_step_id_is_dispatchable(flow_hass, monkeypatch):
    hass = flow_hass()

    def _fresh():
        return _Session(hass, CoverLogicConfigFlow(), handler=DOMAIN, context={"source": "user"})

    for step in ("blinds_now", "from_file", "empty"):
        session = _fresh()
        session.start()
        session.choose(step)

    # `from_example` with the example file present (it ships in this dev checkout).
    session = _fresh()
    session.start()
    session.choose("from_example")

    # `from_example` with no example file -- the branch that delegates to
    # `async_step_example_not_available`, the exact screen commit b655664 fixed.
    monkeypatch.setattr("cover_logic.config_flow.repo_example_config_path", lambda: None)
    session = _fresh()
    session.start()
    session.choose("from_example")


# ---------------------------------------------------------------------------
# The six subentry flows: add (`user`) and edit (`reconfigure`) for every
# type `config_store.SUBENTRY_TYPES` names -- not a hand-kept list, so a
# seventh type gained here later is covered automatically.
# ---------------------------------------------------------------------------


def test_subentry_flows_every_reachable_step_id_is_dispatchable(subentry_entry, subentry_hass):
    entry = subentry_entry()
    seeded = {
        BLIND: entry.add_subentry(BLIND, {"entity": "cover.a"}),
        ZONE: entry.add_subentry(ZONE, {"id": "z", "members": []}),
        VALUE: entry.add_subentry(VALUE, {"id": "v", "entity": "input_number.x", "default": 0}),
        CONDITION: entry.add_subentry(
            CONDITION, {"id": "c", "condition": "state", "entity_id": "x", "state": "on"}
        ),
        MODE: entry.add_subentry(MODE, {"id": "m", "order": 0}),
        RULE: entry.add_subentry(
            RULE,
            {"mode": "m", "zone": "z", "order": 0, "then": {"position": "keep", "tilt": "keep"}},
        ),
    }
    hass = subentry_hass(entry)

    for subentry_type, handler_cls in SUBENTRY_FLOW_HANDLERS.items():
        handler_key = (_ENTRY_ID, subentry_type)

        add_session = _Session(hass, handler_cls(), handler=handler_key, context={"source": "user"})
        add_session.start()

        reconfigure_session = _Session(
            hass,
            handler_cls(),
            handler=handler_key,
            context={
                "source": "reconfigure",
                "entry_id": _ENTRY_ID,
                "subentry_id": seeded[subentry_type],
            },
        )
        reconfigure_session.start()

    # `rule` alone has a second, delegated step: picking a (mode, zone) pair
    # on `async_step_user` moves on to `async_step_rule` without the caller
    # asking for it by name, the same delegation shape as the other two
    # guards above.
    rule_session = _Session(
        hass,
        SUBENTRY_FLOW_HANDLERS[RULE](),
        handler=(_ENTRY_ID, RULE),
        context={"source": "user"},
    )
    rule_session.start()
    rule_session.configure({"mode": "m", "zone": "z"})
