"""Tests for the options flow (`options_flow.py`): the phase 5 Task 1 main menu.

Imports Home Assistant, so this module only collects under the Python 3.14
venv -- see `test_config_flow.py`'s own note for why (`ConfigSubentry`,
`OptionsFlow` and friends have no meaning without a real `homeassistant`
install).

Drives the flow's step methods directly, the same tradeoff
`test_subentry_flows.py`'s own module docstring explains: a working
`OptionsFlowManager` needs a real, running `HomeAssistant` with config-entry
storage and the loader. `conftest.py`'s `FakeOptionsHass`/`FakeOptionsConfigEntries`
stub exactly the surface `OptionsFlow`'s own base-class `config_entry`
property and every add/edit/remove screen actually touch, plus
`services._async_import_config`/`_async_export_config`'s own needs (see
`FakeOptionsConfigEntries`'s own docstring) -- not the whole manager.
"""

import asyncio
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.data_entry_flow import FlowResultType

from cover_logic.config_flow import CoverLogicConfigFlow
from cover_logic.config_store import BLIND, MODE, RULE, VALUE, ZONE
from cover_logic.const import RULE_DEFAULT_ZONE
from cover_logic.options_flow import CoverLogicOptionsFlow

_ENTRY_ID = "entry1"


def _make_flow(hass):
    """Build an options flow instance the way `OptionsFlowManager` would."""
    flow = CoverLogicOptionsFlow()
    flow.hass = hass
    flow.handler = _ENTRY_ID
    flow.flow_id = "test-options-flow-id"
    flow.context = {}
    return flow


def _schema_keys(result):
    """Return the set of field names a shown form's schema declares."""
    return {key.schema for key in result["data_schema"].schema}


def _menu(flow_result):
    assert flow_result["type"] is FlowResultType.MENU
    return flow_result


def _form(flow_result):
    assert flow_result["type"] is FlowResultType.FORM
    return flow_result


# ---------------------------------------------------------------------------
# Main menu: counts and navigation
# ---------------------------------------------------------------------------


def test_init_menu_lists_every_section_plus_import_export_and_check(subentry_entry, options_hass):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    entry.add_subentry(BLIND, {"entity": "cover.b"})
    entry.add_subentry(ZONE, {"id": "terasa", "members": ["cover.a"]})
    flow = _make_flow(options_hass(entry))

    result = _menu(asyncio.run(flow.async_step_init(None)))

    assert result["menu_options"] == [
        "blinds",
        "zones",
        "values",
        "conditions",
        "modes",
        "rules",
        "import_export",
        "check_matrix",
    ]
    placeholders = result["description_placeholders"]
    assert placeholders["blinds_count"] == "2"
    assert placeholders["zones_count"] == "1"
    assert placeholders["values_count"] == "0"
    assert placeholders["conditions_count"] == "0"
    assert placeholders["modes_count"] == "0"
    assert placeholders["rules_count"] == "0"


def test_empty_section_offers_only_add_and_back(subentry_entry, options_hass):
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))

    result = _menu(asyncio.run(flow.async_step_blinds(None)))

    assert result["menu_options"] == ["add", "back"]
    assert result["description_placeholders"] == {"count": "0"}


def test_nonempty_section_offers_edit_and_remove_too(subentry_entry, options_hass):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    flow = _make_flow(options_hass(entry))

    result = _menu(asyncio.run(flow.async_step_blinds(None)))

    assert result["menu_options"] == ["add", "edit", "remove", "back"]
    assert result["description_placeholders"] == {"count": "1"}


def test_config_flow_wires_up_this_options_flow():
    """`CoverLogicConfigFlow.async_get_options_flow` is what makes `supports_
    options_flow` true at all -- without it Home Assistant falls back to its
    own generic flat subentry list, exactly the problem this task exists to
    fix. Calling the classmethod directly is enough to prove the wiring: HA
    itself decides *whether* to call it from `async_supports_options_flow`.
    """
    flow = CoverLogicConfigFlow.async_get_options_flow(None)

    assert isinstance(flow, CoverLogicOptionsFlow)
    assert CoverLogicConfigFlow.async_supports_options_flow(None) is True


def test_back_from_a_section_returns_to_the_main_menu(subentry_entry, options_hass):
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_zones(None))

    result = _menu(asyncio.run(flow.async_step_back(None)))

    assert result["step_id"] == "init"
    assert "blinds" in result["menu_options"]


# ---------------------------------------------------------------------------
# Add / edit / remove -- reusing the exact subentry-flow schema/data/validation
# ---------------------------------------------------------------------------


def test_add_blind_creates_a_subentry_and_returns_to_the_section_menu(subentry_entry, options_hass):
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_blinds(None))

    result = _menu(asyncio.run(flow.async_step_add({"entity": "cover.a"})))

    assert result["step_id"] == "blinds"
    [subentry] = entry.subentries.values()
    assert subentry.subentry_type == BLIND
    assert subentry.data["entity"] == "cover.a"


def test_add_blind_duplicate_entity_is_rejected_with_the_shared_validation(
    subentry_entry, options_hass
):
    """Proves `_render_type_form` reuses `_blocking_errors`, not a re-implementation:
    the exact same `blind 'cover.a' is already configured` rule the subentry
    flow enforces (`_duplicate_errors`) fires here too.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_blinds(None))

    result = _form(asyncio.run(flow.async_step_add({"entity": "cover.a"})))

    assert result["errors"]["base"] == "invalid_config"
    assert "already configured" in result["description_placeholders"]["error_detail"]
    assert len(entry.subentries) == 1


def test_add_form_shows_the_real_blind_schema_fields(subentry_entry, options_hass):
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_blinds(None))

    result = _form(asyncio.run(flow.async_step_add(None)))

    assert _schema_keys(result) == {
        "entity",
        "facade_azimuth",
        "tolerance",
        "travel_time",
        "has_tilt",
        "tilt_after_arrival",
    }


def test_edit_prefills_from_the_picked_subentry_and_saves_changes(subentry_entry, options_hass):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    sid = entry.add_subentry(ZONE, {"id": "terasa", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_zones(None))

    picker = _form(asyncio.run(flow.async_step_edit(None)))
    assert _schema_keys(picker) == {"subentry_id"}

    edit_form = _form(asyncio.run(flow.async_step_edit({"subentry_id": sid})))
    suggested = {
        key.schema: key.description.get("suggested_value")
        for key in edit_form["data_schema"].schema
        if hasattr(key, "description") and key.description
    }
    assert suggested.get("id") == "terasa"

    result = _menu(
        asyncio.run(
            flow.async_step_edit_form({"id": "terasa", "members": ["cover.a"], "occupants": []})
        )
    )

    assert result["step_id"] == "zones"
    assert entry.subentries[sid].data["members"] == ["cover.a"]


def test_edit_form_vanishing_between_render_and_submit_is_a_form_error_not_a_crash(
    subentry_entry, options_hass
):
    """The sibling of the `zone_rules` `next()` bug: `_render_type_form` used
    to index `entry.subentries[subentry_id]` directly. `subentry_id` here is
    `self._pending_id`, set by `async_step_edit`'s picker in one call and
    read back by a later, separate call to `async_step_edit_form` -- exactly
    the same "cached schema, stale id" race, just reached through a plain
    dict index instead of an unguarded `next()`. A concurrent removal
    between the two (another browser tab, `import_config`) used to
    `KeyError` here; it must instead behave like every other "this is gone"
    outcome in this flow -- a form error, then a route back on the next
    submit -- never an unhandled exception.
    """
    entry = subentry_entry()
    sid = entry.add_subentry(ZONE, {"id": "terasa", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_zones(None))
    asyncio.run(flow.async_step_edit(None))
    # Reaches the form once, the same as any normal edit -- the subentry still
    # exists at this point, and this is what sets `self._pending_id`.
    asyncio.run(flow.async_step_edit({"subentry_id": sid}))

    # Simulate the race: a concurrent flow removes it before this one's edit
    # form is actually submitted.
    del entry.subentries[sid]

    first = _form(asyncio.run(flow.async_step_edit_form(None)))
    assert first["step_id"] == "edit_form"
    assert first["errors"]["base"] == "subentry_vanished"

    # Submitting again (the acknowledgement) returns to a section menu that
    # reflects reality, the same "empty form, second submit moves on" shape
    # `async_step_zone_rules`'s own "no items" branch already uses.
    back = _menu(asyncio.run(flow.async_step_edit_form({})))
    assert back["step_id"] == "zones"


def test_remove_requires_confirmation_before_deleting(subentry_entry, options_hass):
    entry = subentry_entry()
    sid = entry.add_subentry(ZONE, {"id": "terasa", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_zones(None))

    result = _form(asyncio.run(flow.async_step_remove({"subentry_id": sid, "confirm": False})))

    assert result["errors"]["base"] == "confirmation_required"
    assert sid in entry.subentries


def test_remove_confirmed_deletes_the_subentry_and_returns_to_the_section_menu(
    subentry_entry, options_hass
):
    entry = subentry_entry()
    sid = entry.add_subentry(ZONE, {"id": "terasa", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_zones(None))

    result = _menu(asyncio.run(flow.async_step_remove({"subentry_id": sid, "confirm": True})))

    assert result["step_id"] == "zones"
    assert sid not in entry.subentries


# ---------------------------------------------------------------------------
# Rule: the two-step add wizard
# ---------------------------------------------------------------------------


def test_add_rule_is_two_steps_pair_then_fields(subentry_entry, options_hass):
    entry = subentry_entry()
    entry.add_subentry(MODE, {"id": "bezny_den", "order": 0})
    entry.add_subentry(ZONE, {"id": "terasa", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))

    pair_form = _form(asyncio.run(flow.async_step_add(None)))
    assert _schema_keys(pair_form) == {"mode", "zone"}

    fields_form = _form(asyncio.run(flow.async_step_add({"mode": "bezny_den", "zone": "terasa"})))
    # `order` is pre-filled from step one's pick, appending to an empty list.
    suggested_order = next(
        key.description["suggested_value"]
        for key in fields_form["data_schema"].schema
        if key.schema == "order"
    )
    assert suggested_order == 0

    result = _menu(
        asyncio.run(
            flow.async_step_add_rule_fields(
                {
                    "mode": "bezny_den",
                    "zone": "terasa",
                    "order": 0,
                    "position": "keep",
                    "tilt": "keep",
                }
            )
        )
    )

    assert result["step_id"] == "rules"
    [rule] = [s for s in entry.subentries.values() if s.subentry_type == RULE]
    assert rule.data["mode"] == "bezny_den"
    assert rule.data["zone"] == "terasa"
    assert rule.data["then"] == {"position": "keep", "tilt": "keep"}


def test_rules_list_menu_and_picker_use_the_real_evaluation_order(subentry_entry, options_hass):
    """Not a re-sort: `_items`/`_rule_items` read `config_store._grouped_rules`
    directly, so a rule with a numerically-later order (e.g. "20") sorts
    after one with an earlier order ("10") even though "20" < "9" as text --
    the exact trap an alphabetical-by-title picker would fall into.
    """
    entry = subentry_entry()
    entry.add_subentry(
        RULE,
        {"mode": "m", "zone": "z", "order": 20, "then": {"position": "keep", "tilt": "keep"}},
        title="20 m.z -> keep/keep",
    )
    entry.add_subentry(
        RULE,
        {"mode": "m", "zone": "z", "order": 9, "then": {"position": "keep", "tilt": "keep"}},
        title="9 m.z -> keep/keep",
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))

    picker = _form(asyncio.run(flow.async_step_edit(None)))
    options = picker["data_schema"].schema["subentry_id"].config["options"]
    labels_in_order = [option["label"] for option in options]

    assert labels_in_order == ["9 m.z -> keep/keep", "20 m.z -> keep/keep"]


def test_rules_section_offers_list_only_once_a_rule_exists(subentry_entry, options_hass):
    """`list` -- the read-only evaluation-order report -- is not offered on
    an empty `rules` section: there is nothing yet for it to show, the same
    reasoning `edit`/`remove` already follow for every type.
    """
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))

    empty_menu = _menu(asyncio.run(flow.async_step_rules(None)))
    assert "list" not in empty_menu["menu_options"]

    entry.add_subentry(
        RULE, {"mode": "m", "zone": "z", "order": 0, "then": {"position": "keep", "tilt": "keep"}}
    )
    nonempty_menu = _menu(asyncio.run(flow.async_step_rules(None)))
    assert nonempty_menu["menu_options"] == ["add", "list", "zone", "edit", "remove", "back"]


def test_rules_list_shows_every_rule_grouped_in_real_order_with_action_visible(
    subentry_entry, options_hass
):
    """The whole point of Task 3: order is semantics (first match wins within
    a `(mode, zone)` pair), so the report must show the *real* evaluation
    order -- not alphabetical by title, which "20" sorting before "9" as text
    would silently get wrong -- and must not hide `keep` or a `!ref` behind
    an opaque value.
    """
    entry = subentry_entry()
    entry.add_subentry(VALUE, {"id": "kvety_poz", "entity": "input_number.x", "default": 34})
    # Deliberately added out of numeric order and split across two different
    # (mode, zone) pairs, so a naive re-sort (by title, or by subentry
    # insertion order) would show something other than the real order.
    entry.add_subentry(
        RULE,
        {"mode": "m", "zone": "z", "order": 20, "then": {"position": 50, "tilt": "keep"}},
    )
    entry.add_subentry(
        RULE,
        {
            "mode": "m",
            "zone": "z",
            "order": 9,
            "then": {"position": {"ref": "kvety_poz"}, "tilt": 0},
        },
    )
    entry.add_subentry(
        RULE,
        {"mode": "noc", "zone": "spalna", "order": 0, "then": {"position": "keep", "tilt": "keep"}},
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))

    result = _form(asyncio.run(flow.async_step_list(None)))

    assert result["step_id"] == "list"
    text = result["description_placeholders"]["result"]

    # "m.z" lists its two rules with "9" before "20" -- the real evaluation
    # order, not what alphabetical-by-title ("20 ..." before "9 ...") or
    # subentry-add order (20 was added first here) would show.
    m_z_index = text.index("m.z:")
    line_9 = text.index("9: position=kvety_poz, tilt=0")
    line_20 = text.index("20: position=50, tilt=keep")
    noc_spalna_index = text.index("noc.spalna:")
    assert m_z_index < line_9 < line_20
    assert "0: position=keep, tilt=keep" in text
    assert noc_spalna_index > m_z_index


def test_rules_list_reports_no_rules_configured_when_the_section_is_empty(
    subentry_entry, options_hass
):
    """Reachable only if a caller invokes `async_step_list` directly (the
    menu itself hides `list` when empty -- see the test above), but the step
    must still say something sane rather than rendering blank.
    """
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))

    result = _form(asyncio.run(flow.async_step_list(None)))

    assert "No rules configured" in result["description_placeholders"]["result"]


def test_rules_list_merges_a_mode_default_into_every_zone_that_inherits_it(
    subentry_entry, options_hass
):
    """The task brief's own words: "if inheritance is invisible [in the
    list], the list lies about evaluation order." A zone with its own rule
    shows both, own first; a zone with none of its own -- invisible to a
    report that only walked `_grouped_rules(entry).items()`, since no rule
    subentry names its key at all -- must still show the inherited default.
    """
    entry = subentry_entry()
    entry.add_subentry(
        RULE,
        {"mode": "noc", "zone": "terasa", "order": 20, "then": {"position": 0, "tilt": "keep"}},
    )
    entry.add_subentry(
        RULE,
        {
            "mode": "noc",
            "zone": RULE_DEFAULT_ZONE,
            "order": 0,
            "then": {"position": "keep", "tilt": "keep"},
        },
    )
    entry.add_subentry(ZONE, {"id": "spalna", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))

    result = _form(asyncio.run(flow.async_step_list(None)))
    text = result["description_placeholders"]["result"]

    # `terasa` has its own rule, then the inherited default after it.
    terasa_index = text.index("noc.terasa:")
    own_line = text.index("20: position=0, tilt=keep")
    inherited_line_under_terasa = text.index(
        "0: position=keep, tilt=keep [inherited from mode default]"
    )
    assert terasa_index < own_line < inherited_line_under_terasa

    # `spalna` has no rule subentry of its own at all -- only the inherited
    # default, which must still show up under its own key.
    spalna_index = text.index("noc.spalna:")
    assert spalna_index > 0
    assert text.count("[inherited from mode default]") == 2


def test_rules_list_still_fans_a_default_out_across_zones_when_its_mode_is_gone(
    subentry_entry, options_hass
):
    """`_rule_overview_text`'s `default_modes` is collected from rule keys
    alone -- it never checks that the mode a default row names still has a
    `mode` subentry of its own. A rule stranded by a deleted mode (the same
    scenario `validation._check_rule_keys`'s own `unknown_rule_key` already
    names -- deleting a mode does not cascade-delete the rules that were
    filed under it) therefore still fans its default out across every real
    zone, one synthesised block each, exactly as if the mode still existed.

    This is documented as current behaviour, not fixed, by choice: the
    obvious-looking fix (skip a default whose mode has no `mode` subentry)
    would also skip the deliberately mode-less setups the test directly
    above this one (`test_rules_list_merges_a_mode_default_into_every_zone_
    that_inherits_it`) already relies on -- that test's own "noc" is never
    given a `mode` subentry either, only referenced by rule keys, and still
    expects the fan-out to happen. Telling "genuinely orphaned by a delete"
    apart from "not (yet) given its own mode subentry" needs a real `Config`
    and `validate()`'s own `unknown_rule_key` check, which this screen
    deliberately does not run -- it must still render *something* useful for
    an entry that does not even parse (see `_current_problems`'s own
    docstring), and this report is display-only: it never feeds `engine.
    evaluate`, so a misleading block here cannot move a physical blind.
    """
    entry = subentry_entry()
    entry.add_subentry(
        RULE,
        {
            "mode": "ghost",
            "zone": RULE_DEFAULT_ZONE,
            "order": 0,
            "then": {"position": "keep", "tilt": "keep"},
        },
    )
    entry.add_subentry(ZONE, {"id": "spalna", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))

    result = _form(asyncio.run(flow.async_step_list(None)))
    text = result["description_placeholders"]["result"]

    assert "ghost.spalna:" in text
    assert "0: position=keep, tilt=keep [inherited from mode default]" in text


def test_rules_list_submit_returns_to_the_rules_section_menu(subentry_entry, options_hass):
    entry = subentry_entry()
    entry.add_subentry(
        RULE, {"mode": "m", "zone": "z", "order": 0, "then": {"position": "keep", "tilt": "keep"}}
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_list(None))

    result = _menu(asyncio.run(flow.async_step_list({})))

    assert result["step_id"] == "rules"


# ---------------------------------------------------------------------------
# Rule: adding a mode-wide default (phase 6 task 2)
# ---------------------------------------------------------------------------


def test_the_rule_pair_picker_offers_the_wildcard_zone_for_a_mode_default(
    subentry_entry, options_hass
):
    """The same picker `add`'s first step already shows must offer
    `const.RULE_DEFAULT_ZONE` ("*") alongside real zones -- this is the whole
    mechanism the task brief's "a way to add a default for a mode" asks for,
    not a second form.
    """
    entry = subentry_entry()
    entry.add_subentry(MODE, {"id": "noc", "order": 0})
    entry.add_subentry(ZONE, {"id": "terasa", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))

    pair_form = _form(asyncio.run(flow.async_step_add(None)))

    zone_options = pair_form["data_schema"].schema["zone"].config["options"]
    assert RULE_DEFAULT_ZONE in zone_options
    assert "terasa" in zone_options


def test_adding_a_rule_with_the_wildcard_zone_creates_a_mode_default(subentry_entry, options_hass):
    entry = subentry_entry()
    entry.add_subentry(MODE, {"id": "noc", "order": 0})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_add(None))

    result = _menu(
        asyncio.run(
            flow.async_step_add_rule_fields(
                {
                    "mode": "noc",
                    "zone": RULE_DEFAULT_ZONE,
                    "order": 0,
                    "position": "keep",
                    "tilt": "keep",
                }
            )
        )
    )

    assert result["step_id"] == "rules"
    [rule] = [s for s in entry.subentries.values() if s.subentry_type == RULE]
    assert rule.data["zone"] == RULE_DEFAULT_ZONE


# ---------------------------------------------------------------------------
# Rule: one zone's own screen -- own rules plus inherited defaults
# (phase 6 task 2)
# ---------------------------------------------------------------------------


def _zone_options(result):
    return result["data_schema"].schema["subentry_id"].config["options"]


def test_zone_screen_shows_own_rules_then_inherited_defaults_in_real_order(
    subentry_entry, options_hass
):
    entry = subentry_entry()
    own = entry.add_subentry(
        RULE,
        {"mode": "noc", "zone": "terasa", "order": 20, "then": {"position": 0, "tilt": "keep"}},
    )
    default = entry.add_subentry(
        RULE,
        {
            "mode": "noc",
            "zone": RULE_DEFAULT_ZONE,
            "order": 0,
            "then": {"position": "keep", "tilt": "keep"},
        },
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_zone(None))

    result = _form(asyncio.run(flow.async_step_zone({"mode": "noc", "zone": "terasa"})))

    assert result["step_id"] == "zone_rules"
    options = _zone_options(result)
    assert [option["value"] for option in options] == [own, default]
    assert "(inherited from mode default)" not in options[0]["label"]
    assert "(inherited from mode default)" in options[1]["label"]


def test_zone_with_no_own_rules_shows_only_the_inherited_defaults(subentry_entry, options_hass):
    entry = subentry_entry()
    default = entry.add_subentry(
        RULE,
        {
            "mode": "noc",
            "zone": RULE_DEFAULT_ZONE,
            "order": 0,
            "then": {"position": "keep", "tilt": "keep"},
        },
    )
    entry.add_subentry(ZONE, {"id": "spalna", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_zone(None))

    result = _form(asyncio.run(flow.async_step_zone({"mode": "noc", "zone": "spalna"})))

    options = _zone_options(result)
    assert [option["value"] for option in options] == [default]
    assert "(inherited from mode default)" in options[0]["label"]


def test_zone_pair_with_no_rules_at_all_says_so_and_returns_to_the_section_menu(
    subentry_entry, options_hass
):
    entry = subentry_entry()
    entry.add_subentry(MODE, {"id": "noc", "order": 0})
    entry.add_subentry(ZONE, {"id": "spalna", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_zone(None))

    empty_result = _form(asyncio.run(flow.async_step_zone({"mode": "noc", "zone": "spalna"})))
    assert empty_result["step_id"] == "zone_rules"
    assert _schema_keys(empty_result) == set()

    back_result = _menu(asyncio.run(flow.async_step_zone_rules({})))
    assert back_result["step_id"] == "rules"


def test_picking_an_inherited_rule_on_the_zone_screen_is_refused_with_a_route_onward(
    subentry_entry, options_hass
):
    """The rule the task brief cares about most: an inherited row is a real
    subentry, but not this zone's to save -- picking it must neither edit it
    silently nor no-op silently, it must say why and where to go instead.
    """
    entry = subentry_entry()
    default = entry.add_subentry(
        RULE,
        {
            "mode": "noc",
            "zone": RULE_DEFAULT_ZONE,
            "order": 0,
            "then": {"position": "keep", "tilt": "keep"},
        },
    )
    original_data = dict(entry.subentries[default].data)
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_zone(None))
    asyncio.run(flow.async_step_zone({"mode": "noc", "zone": "terasa"}))

    result = _form(
        asyncio.run(flow.async_step_zone_rules({"subentry_id": default, "confirm": False}))
    )

    assert result["step_id"] == "zone_rules"
    assert result["errors"]["base"] == "rule_is_inherited"
    assert result["description_placeholders"]["mode"] == "noc"
    # Refused, not silently applied: the subentry this would have "edited"
    # (there was nothing else to submit) is untouched, and it still exists.
    assert entry.subentries[default].data == original_data


def test_picking_an_inherited_rule_to_remove_on_the_zone_screen_is_also_refused(
    subentry_entry, options_hass
):
    entry = subentry_entry()
    default = entry.add_subentry(
        RULE,
        {
            "mode": "noc",
            "zone": RULE_DEFAULT_ZONE,
            "order": 0,
            "then": {"position": "keep", "tilt": "keep"},
        },
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_zone(None))
    asyncio.run(flow.async_step_zone({"mode": "noc", "zone": "terasa"}))

    result = _form(
        asyncio.run(flow.async_step_zone_rules({"subentry_id": default, "confirm": True}))
    )

    assert result["errors"]["base"] == "rule_is_inherited"
    assert default in entry.subentries


def test_picking_the_zone_s_own_rule_opens_it_for_editing(subentry_entry, options_hass):
    entry = subentry_entry()
    own = entry.add_subentry(
        RULE,
        {"mode": "noc", "zone": "terasa", "order": 20, "then": {"position": 0, "tilt": "keep"}},
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_zone(None))
    asyncio.run(flow.async_step_zone({"mode": "noc", "zone": "terasa"}))

    result = _form(asyncio.run(flow.async_step_zone_rules({"subentry_id": own, "confirm": False})))

    assert result["step_id"] == "edit_form"
    assert _schema_keys(result) >= {"mode", "zone", "order", "position", "tilt"}


def test_removing_the_zone_s_own_rule_from_the_zone_screen(subentry_entry, options_hass):
    entry = subentry_entry()
    own = entry.add_subentry(
        RULE,
        {"mode": "noc", "zone": "terasa", "order": 20, "then": {"position": 0, "tilt": "keep"}},
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_zone(None))
    asyncio.run(flow.async_step_zone({"mode": "noc", "zone": "terasa"}))

    result = _menu(asyncio.run(flow.async_step_zone_rules({"subentry_id": own, "confirm": True})))

    assert result["step_id"] == "rules"
    assert own not in entry.subentries


def test_picking_a_rule_removed_between_render_and_submit_is_a_form_error_not_a_crash(
    subentry_entry, options_hass
):
    """The bug a code review caught: `items` is recomputed fresh on every
    call, but Home Assistant validates the submitted `subentry_id` against
    the *schema* cached when the form was rendered -- so a rule removed in
    between (another browser tab, an `import_config` call, the same user
    navigating elsewhere) is still schema-valid but no longer in `items`.
    The unguarded `next()` this replaced had no default, so it raised
    `StopIteration` -- `RuntimeError` from inside a coroutine, an HTTP 500 --
    instead of the ordinary form error every other "this cannot be done"
    outcome on this screen already gets (`rule_is_inherited`,
    `confirmation_required`).
    """
    entry = subentry_entry()
    own = entry.add_subentry(
        RULE,
        {"mode": "noc", "zone": "terasa", "order": 20, "then": {"position": 0, "tilt": "keep"}},
    )
    # A default row too, so `items` still has something in it once `own` is
    # removed below -- otherwise this would only exercise the earlier "no
    # items at all" branch, not the `next()` lookup this test targets.
    entry.add_subentry(
        RULE,
        {
            "mode": "noc",
            "zone": RULE_DEFAULT_ZONE,
            "order": 0,
            "then": {"position": "keep", "tilt": "keep"},
        },
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_zone(None))
    asyncio.run(flow.async_step_zone({"mode": "noc", "zone": "terasa"}))

    # Simulate the race: gone by the time the pick is actually submitted.
    del entry.subentries[own]

    result = _form(asyncio.run(flow.async_step_zone_rules({"subentry_id": own, "confirm": False})))

    assert result["step_id"] == "zone_rules"
    assert result["errors"]["base"] == "rule_no_longer_exists"


# ---------------------------------------------------------------------------
# Import / export -- calling the existing handlers, not the service bus
# ---------------------------------------------------------------------------


def test_import_replaces_subentries_via_the_real_import_handler(
    subentry_entry, options_hass, tmp_path
):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.stale"})
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        "blinds:\n"
        "  - entity: cover.a\n"
        "zones:\n"
        "  terasa:\n"
        "    members: [cover.a]\n"
        "modes:\n"
        "  - {id: m}\n"
        "rules:\n"
        "  m.terasa:\n"
        "    - {then: {position: keep, tilt: keep}}\n",
        encoding="utf-8",
    )
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_init(None))

    result = _menu(
        asyncio.run(
            flow.async_step_import_export(
                {
                    "action": "import",
                    "path": str(yaml_path),
                    "dry_run": False,
                    "overwrite": True,
                }
            )
        )
    )

    assert result["step_id"] == "init"
    entities = {s.data.get("entity") for s in entry.subentries.values() if s.subentry_type == BLIND}
    assert entities == {"cover.a"}


def test_import_without_overwrite_onto_existing_config_is_rejected(
    subentry_entry, options_hass, tmp_path
):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text("blinds:\n  - entity: cover.b\n", encoding="utf-8")
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_import_export(None))

    result = _form(
        asyncio.run(
            flow.async_step_import_export(
                {"action": "import", "path": str(yaml_path), "dry_run": False, "overwrite": False}
            )
        )
    )

    assert result["errors"]["base"] == "import_export_failed"
    assert len(entry.subentries) == 1


def test_export_writes_the_file_via_the_real_export_handler(subentry_entry, options_hass, tmp_path):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    entry.add_subentry(ZONE, {"id": "terasa", "members": ["cover.a"]})
    entry.add_subentry(MODE, {"id": "m", "order": 0})
    entry.add_subentry(
        RULE,
        {"mode": "m", "zone": "terasa", "order": 0, "then": {"position": "keep", "tilt": "keep"}},
    )
    out_path = tmp_path / "out.yaml"
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_import_export(None))

    result = _menu(
        asyncio.run(flow.async_step_import_export({"action": "export", "path": str(out_path)}))
    )

    assert result["step_id"] == "init"
    assert out_path.is_file()
    assert "cover.a" in out_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Check against the old matrix
# ---------------------------------------------------------------------------


def test_check_matrix_reports_no_fixture_when_none_ships(subentry_entry, options_hass, monkeypatch):
    monkeypatch.setattr("cover_logic.options_flow.repo_fixture_path", lambda: None)
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))

    result = _form(asyncio.run(flow.async_step_check_matrix(None)))

    assert "nothing to check" in result["description_placeholders"]["result"]


def test_check_matrix_reports_a_match(subentry_entry, options_hass, monkeypatch, tmp_path):
    fixture = tmp_path / "dom_peter.yaml"
    fixture.write_text(
        "blinds:\n  - entity: cover.a\nzones:\n  terasa:\n    members: [cover.a]\n"
        "modes:\n  - {id: m}\nrules:\n  m.terasa:\n    - {then: {position: keep, tilt: keep}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("cover_logic.options_flow.repo_fixture_path", lambda: fixture)
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    entry.add_subentry(ZONE, {"id": "terasa", "members": ["cover.a"]})
    entry.add_subentry(MODE, {"id": "m", "order": 0})
    entry.add_subentry(
        RULE,
        {"mode": "m", "zone": "terasa", "order": 0, "then": {"position": "keep", "tilt": "keep"}},
    )
    flow = _make_flow(options_hass(entry))

    result = _form(asyncio.run(flow.async_step_check_matrix(None)))

    assert "Matches" in result["description_placeholders"]["result"]


def test_check_matrix_reports_a_diff(subentry_entry, options_hass, monkeypatch, tmp_path):
    fixture = tmp_path / "dom_peter.yaml"
    fixture.write_text("blinds:\n  - entity: cover.other\n", encoding="utf-8")
    monkeypatch.setattr("cover_logic.options_flow.repo_fixture_path", lambda: fixture)
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    flow = _make_flow(options_hass(entry))

    result = _form(asyncio.run(flow.async_step_check_matrix(None)))

    assert "Differs" in result["description_placeholders"]["result"]
    assert "blinds" in result["description_placeholders"]["result"]


def test_check_matrix_submit_returns_to_main_menu(subentry_entry, options_hass, monkeypatch):
    monkeypatch.setattr("cover_logic.options_flow.repo_fixture_path", lambda: None)
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_check_matrix(None))

    result = _menu(asyncio.run(flow.async_step_check_matrix({})))

    assert result["step_id"] == "init"


# ---------------------------------------------------------------------------
# Task 4: a health overview -- validation counts (with attribution), the
# fixture check, and the coordinator's last recompute -- reachable both as
# an at-a-glance summary on the main menu and as `check_matrix`'s full
# report.
# ---------------------------------------------------------------------------


def test_main_menu_health_summary_says_no_problems_for_a_clean_config(subentry_entry, options_hass):
    """A config with no static problems at all: one mode (the fallback), one
    zone owning the one blind, one rule for that (mode, zone) pair -- nothing
    for `validate()`/`duplicate_rule_order_problems` to flag.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a"})
    entry.add_subentry(ZONE, {"id": "terasa", "members": ["cover.a"]})
    entry.add_subentry(MODE, {"id": "m", "order": 0})
    entry.add_subentry(
        RULE,
        {"mode": "m", "zone": "terasa", "order": 0, "then": {"position": "keep", "tilt": "keep"}},
    )
    flow = _make_flow(options_hass(entry))

    result = _menu(asyncio.run(flow.async_step_init(None)))

    assert result["description_placeholders"]["health_summary"] == "no problems found"


def test_main_menu_health_summary_counts_errors_on_an_empty_config(subentry_entry, options_hass):
    """An entry with nothing configured at all has no fallback mode
    (`no_fallback_mode`) -- exactly one `ERROR`, no `WARNING` -- so the
    at-a-glance summary must say "1 error", not "no problems found", the
    moment a section actually needs attention.
    """
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))

    result = _menu(asyncio.run(flow.async_step_init(None)))

    assert result["description_placeholders"]["health_summary"] == "1 error"


def test_check_matrix_reports_validation_counts_and_attributes_the_problem(
    subentry_entry, options_hass, monkeypatch
):
    """The whole point of Task 4: a count alone is not "findable" -- the
    report must name *which* subentry a problem belongs to. Here, mode "m1"'s
    `when` refs a condition that does not exist; `m2` is the fallback (no
    `when`), so this is the *only* static problem, and it must be attributed
    to `("mode", "m1")` -- the exact `(subentry_type, id)` pair a user would
    open the "Modes" section and pick to fix it, not just "some mode,
    somewhere".
    """
    monkeypatch.setattr("cover_logic.options_flow.repo_fixture_path", lambda: None)
    entry = subentry_entry()
    entry.add_subentry(
        MODE, {"id": "m1", "order": 0, "when": {"condition": "ref", "name": "missing"}}
    )
    entry.add_subentry(MODE, {"id": "m2", "order": 10})
    flow = _make_flow(options_hass(entry))

    result = _form(asyncio.run(flow.async_step_check_matrix(None)))
    text = result["description_placeholders"]["result"]

    assert "Validation: 1 error(s), 0 warning(s)." in text
    assert "unknown_condition_ref" in text
    assert "mode 'm1'" in text
    # Not attributed to the *other* mode -- attribution must be specific.
    assert "mode 'm2'" not in text


def test_check_matrix_reports_not_yet_evaluated_with_no_runtime_data(subentry_entry, options_hass):
    """A `ConfigEntry` that has not finished `async_setup_entry` (or never
    will, because `validate()` already refuses it -- see `__init__.py`) has
    no `runtime_data` at all; the report must say so plainly rather than
    raising `AttributeError` on the one case it exists to describe.
    """
    monkeypatch_free_entry = subentry_entry()
    flow = _make_flow(options_hass(monkeypatch_free_entry))

    result = _form(asyncio.run(flow.async_step_check_matrix(None)))

    assert "not yet evaluated" in result["description_placeholders"]["result"]


def test_check_matrix_reports_the_coordinators_last_success_and_error(subentry_entry, options_hass):
    """Once the entry has a coordinator, its `last_success`/`last_error`
    must be surfaced verbatim -- this is the "last successful recomputation,
    and any error from it" half of Task 4's brief.
    """
    entry = subentry_entry()
    when = dt.datetime(2026, 8, 27, 12, 0, 0, tzinfo=dt.UTC)
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(last_success=when, last_error="ValueError: boom")
    )
    flow = _make_flow(options_hass(entry))

    result = _form(asyncio.run(flow.async_step_check_matrix(None)))
    text = result["description_placeholders"]["result"]

    assert when.isoformat() in text
    assert "ValueError: boom" in text


def test_check_matrix_reports_no_error_when_the_coordinator_has_none(subentry_entry, options_hass):
    entry = subentry_entry()
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(last_success=None, last_error=None)
    )
    flow = _make_flow(options_hass(entry))

    result = _form(asyncio.run(flow.async_step_check_matrix(None)))
    text = result["description_placeholders"]["result"]

    assert "last successful recompute never; no error" in text


# ---------------------------------------------------------------------------
# Translation coverage: every step this flow can actually render must have a
# title, and every field it actually shows must have a label -- derived from
# rendering real forms, not a hand-kept list (see `test_translations.py`'s
# own docstring for why this derivation belongs here, next to the flow,
# rather than there).
# ---------------------------------------------------------------------------


def _strings():
    # Derived from this file's location the same way `test_translations.py`
    # locates the component, rather than from an imported module's `__file__`:
    # importing `cover_logic.options_flow` a second time just to read a path
    # is what CodeQL's "imported with 'import' and 'import from'" rule flags,
    # and this project has already had to fix that once (7d758f6).
    component = Path(__file__).parents[2] / "custom_components" / "cover_logic"
    return json.loads((component / "strings.json").read_text(encoding="utf-8"))


_ALL_STEP_IDS = [
    "init",
    "blinds",
    "zones",
    "values",
    "conditions",
    "modes",
    "rules",
    "list",
    "add",
    "add_rule_fields",
    "edit",
    "edit_form",
    "remove",
    "import_export",
    "check_matrix",
]


def test_every_step_this_flow_can_show_has_a_title():
    steps = _strings()["options"]["step"]
    missing = [step for step in _ALL_STEP_IDS if not steps.get(step, {}).get("title")]
    assert missing == [], f"strings.json is missing a title for step(s): {missing}"


@pytest.mark.parametrize(
    ("section", "add_fields"),
    [
        ("blinds", {"entity": "cover.a"}),
        ("zones", {"id": "z", "members": [], "occupants": []}),
        ("values", {"id": "v", "entity": "input_number.x", "default": 0}),
        (
            "conditions",
            {"id": "c", "condition": [{"condition": "state", "entity_id": "x", "state": "on"}]},
        ),
        ("modes", {"id": "mo", "order": 0}),
    ],
)
def test_every_field_of_every_add_form_has_a_label(
    subentry_entry, options_hass, section, add_fields
):
    """Renders the real `add` form for every non-rule type and checks every
    field name it declares has an `options.step.add.data.<field>` entry --
    proof that reusing the subentry flow's own schema (rather than a second
    copy) still leaves every field translated, not just the common ones.
    """
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))
    asyncio.run(getattr(flow, f"async_step_{section}")(None))

    result = _form(asyncio.run(flow.async_step_add(None)))
    declared = set(_strings()["options"]["step"]["add"]["data"])

    missing = _schema_keys(result) - declared
    assert missing == set(), f"{section} add form has undeclared field(s): {missing}"


def test_every_field_of_the_rule_fields_form_has_a_label(subentry_entry, options_hass):
    entry = subentry_entry()
    entry.add_subentry(MODE, {"id": "m", "order": 0})
    entry.add_subentry(ZONE, {"id": "z", "members": []})
    flow = _make_flow(options_hass(entry))
    asyncio.run(flow.async_step_rules(None))
    asyncio.run(flow.async_step_add(None))

    result = _form(asyncio.run(flow.async_step_add({"mode": "m", "zone": "z"})))
    declared = set(_strings()["options"]["step"]["add_rule_fields"]["data"])

    missing = _schema_keys(result) - declared
    assert missing == set(), f"rule fields form has undeclared field(s): {missing}"


def test_import_export_form_fields_have_labels(subentry_entry, options_hass):
    entry = subentry_entry()
    flow = _make_flow(options_hass(entry))

    result = _form(asyncio.run(flow.async_step_import_export(None)))
    declared = set(_strings()["options"]["step"]["import_export"]["data"])

    missing = _schema_keys(result) - declared
    assert missing == set(), f"import_export form has undeclared field(s): {missing}"


def test_main_menu_options_all_have_labels():
    menu = _strings()["options"]["step"]["init"]["menu_options"]
    assert set(menu) == {
        "blinds",
        "zones",
        "values",
        "conditions",
        "modes",
        "rules",
        "import_export",
        "check_matrix",
    }


def test_section_menu_options_all_have_labels():
    for section in ("blinds", "zones", "values", "conditions", "modes", "rules"):
        menu = _strings()["options"]["step"][section]["menu_options"]
        expected = {"add", "edit", "remove", "back"}
        if section == "rules":
            # `rules` alone also offers `list` (the read-only evaluation-
            # order report) and `zone` (one zone's own screen, own rules
            # plus inherited defaults); see `options_flow._show_section_
            # menu`'s own reasoning for why the other five types do not.
            expected.update({"list", "zone"})
        assert set(menu) == expected
