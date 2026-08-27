"""Tests for the `blind`/`zone`/`value` subentry flows in `config_flow.py`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv -- see `test_config_flow.py`'s own note.

Drives each flow's step methods directly, the same tradeoff
`test_config_flow.py`'s module docstring explains for the top-level flow: a
working `ConfigSubentryFlowManager` needs a real, running `HomeAssistant`
with `config_entries` storage and the loader. `tests/ha/conftest.py`'s
`FakeSubentryEntry`/`FakeSubentryHass` stub exactly the surface
`ConfigSubentryFlow`'s own base-class methods touch
(`_get_entry`, `_get_reconfigure_subentry`, `async_update_and_abort`), not
the whole manager -- and `FakeSubentryEntry.add_subentry` stands in for what
the manager does after a flow finishes, so a test can chain "add a blind"
into "add a zone that references it" the same way a real user would, one
finished flow at a time.
"""

import asyncio

import pytest

pytest.importorskip("homeassistant")

from homeassistant.data_entry_flow import FlowResultType
import voluptuous as vol

from cover_logic.config_flow import (
    SUBENTRY_FLOW_HANDLERS,
    BlindSubentryFlowHandler,
    CoverLogicConfigFlow,
    ValueSubentryFlowHandler,
    ZoneSubentryFlowHandler,
)
from cover_logic.config_schema import (
    _BLIND_KEYS,
    _VALUE_KEYS,
    _ZONE_KEYS,
    _parse_values,
    load_config,
)
from cover_logic.config_store import _ID_KEY, BLIND, MODE, VALUE, ZONE, config_from_subentries
from cover_logic.model import Blind, Ref

_ENTRY_ID = "entry1"


def _make_flow(cls, hass, subentry_type, *, subentry_id=None):
    """Build a subentry flow instance the way `ConfigSubentryFlowManager` would.

    `subentry_id=None` sets up an "add" flow (`source: user`); a real id sets
    up a "reconfigure" (edit) flow -- see `ConfigSubentryFlow._entry_id`/
    `_subentry_type`/`_reconfigure_subentry_id`, the three properties this
    mirrors.
    """
    flow = cls()
    flow.hass = hass
    flow.handler = (_ENTRY_ID, subentry_type)
    flow.flow_id = "test-flow-id"
    if subentry_id is None:
        flow.context = {"source": "user"}
    else:
        flow.context = {"source": "reconfigure", "entry_id": _ENTRY_ID, "subentry_id": subentry_id}
    return flow


def _schema_keys(result):
    """Return the set of field names a shown form's schema declares."""
    return {key.schema for key in result["data_schema"].schema}


# ---------------------------------------------------------------------------
# blind
# ---------------------------------------------------------------------------


def test_supported_subentry_types_includes_blind_zone_value():
    types = CoverLogicConfigFlow.async_get_supported_subentry_types(None)

    assert types is SUBENTRY_FLOW_HANDLERS
    assert set(types) == {BLIND, ZONE, VALUE}


def test_blind_add_shows_a_form_with_the_expected_fields(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(BlindSubentryFlowHandler, subentry_hass(entry), BLIND)

    result = asyncio.run(flow.async_step_user(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert _schema_keys(result) == {
        "entity",
        "facade_azimuth",
        "tolerance",
        "travel_time",
        "has_tilt",
        "tilt_after_arrival",
    }


def test_blind_schema_matches_config_schema_blind_keys_exactly(subentry_entry, subentry_hass):
    """`config_schema._parse_blind` rejects a `data` dict with keys outside
    `_BLIND_KEYS` -- this drift guard fails loudly if the form's field set
    and the reader's expected key set are ever edited out of sync, instead
    of failing silently as "every blind add now raises `ConfigError`".
    """
    entry = subentry_entry()
    flow = _make_flow(BlindSubentryFlowHandler, subentry_hass(entry), BLIND)

    result = asyncio.run(flow.async_step_user(None))

    assert _schema_keys(result) == _BLIND_KEYS


def test_blind_add_creates_a_subentry_with_exactly_the_submitted_keys(
    subentry_entry, subentry_hass
):
    entry = subentry_entry()
    flow = _make_flow(BlindSubentryFlowHandler, subentry_hass(entry), BLIND)
    submitted = {
        "entity": "cover.a",
        "facade_azimuth": 270,
        "tolerance": 45,
        "travel_time": 60,
        "has_tilt": True,
        "tilt_after_arrival": True,
    }

    result = asyncio.run(flow.async_step_user(submitted))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "cover.a"
    assert result["data"] == submitted


def test_blind_reconfigure_updates_the_existing_subentry(subentry_entry, subentry_hass):
    entry = subentry_entry()
    subentry_id = entry.add_subentry(
        BLIND,
        {
            "entity": "cover.a",
            "tolerance": 45,
            "travel_time": 60,
            "has_tilt": True,
            "tilt_after_arrival": True,
        },
        title="cover.a",
    )
    flow = _make_flow(
        BlindSubentryFlowHandler, subentry_hass(entry), BLIND, subentry_id=subentry_id
    )

    # Confirm the form is pre-filled with the existing data before submitting a change.
    # A broken `add_suggested_values_to_schema` call (wrong `current`, or the
    # call dropped entirely) would still leave `type`/`step_id` exactly as
    # asserted below, so those two alone cannot catch it -- the suggested
    # values baked into each field's marker (`add_suggested_values_to_schema`
    # writes `key.description = {"suggested_value": ...}`, see
    # `homeassistant/data_entry_flow.py`) must be inspected directly.
    shown = asyncio.run(flow.async_step_reconfigure(None))
    assert shown["type"] is FlowResultType.FORM
    assert shown["step_id"] == "reconfigure"
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in shown["data_schema"].schema
        if isinstance(key, vol.Marker) and key.description
    }
    assert suggested == {
        "entity": "cover.a",
        "tolerance": 45,
        "travel_time": 60,
        "has_tilt": True,
        "tilt_after_arrival": True,
    }

    updated = {
        "entity": "cover.a",
        "tolerance": 30,
        "travel_time": 90,
        "has_tilt": True,
        "tilt_after_arrival": False,
    }
    result = asyncio.run(flow.async_step_reconfigure(updated))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data == updated


def test_blind_add_is_not_blocked_by_having_no_zone_yet(subentry_entry, subentry_hass):
    """A first blind, alone, would fail `validate()`'s `blind_without_zone` and
    `no_fallback_mode` -- neither is a problem the blind form's own fields
    (`entity`, `tolerance`, ...) can address, since it has no `members` or
    `when` field, so neither blocks a blind save. See
    `config_flow._CODE_OWNERS`/`_blocks_on`.
    """
    entry = subentry_entry()
    flow = _make_flow(BlindSubentryFlowHandler, subentry_hass(entry), BLIND)

    result = asyncio.run(
        flow.async_step_user(
            {
                "entity": "cover.a",
                "tolerance": 45,
                "travel_time": 60,
                "has_tilt": True,
                "tilt_after_arrival": True,
            }
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


def test_blind_add_rejects_a_duplicate_entity(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(
        BLIND,
        {
            "entity": "cover.a",
            "tolerance": 45,
            "travel_time": 60,
            "has_tilt": True,
            "tilt_after_arrival": True,
        },
    )
    flow = _make_flow(BlindSubentryFlowHandler, subentry_hass(entry), BLIND)

    result = asyncio.run(
        flow.async_step_user(
            {
                "entity": "cover.a",
                "tolerance": 45,
                "travel_time": 60,
                "has_tilt": True,
                "tilt_after_arrival": True,
            }
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "already configured" in result["description_placeholders"]["error_detail"]
    # Only the one blind that was seeded ever got created.
    assert len(entry.subentries) == 1


# ---------------------------------------------------------------------------
# zone
# ---------------------------------------------------------------------------


def test_zone_members_options_are_the_configured_blinds(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(BLIND, {"entity": "cover.b", "tolerance": 45, "travel_time": 60})
    flow = _make_flow(ZoneSubentryFlowHandler, subentry_hass(entry), ZONE)

    result = asyncio.run(flow.async_step_user(None))

    assert result["type"] is FlowResultType.FORM
    assert _schema_keys(result) == {"id", "members", "occupants"}
    members_selector = next(
        val for key, val in result["data_schema"].schema.items() if key.schema == "members"
    )
    assert members_selector.config["options"] == ["cover.a", "cover.b"]


def test_zone_schema_matches_config_schema_zone_keys_plus_id(subentry_entry, subentry_hass):
    """Same drift guard as `test_blind_schema_matches_config_schema_blind_keys_exactly`,
    for `_build_zones`'s expectations: `_ZONE_KEYS` (unchecked-but-read) plus
    `_ID_KEY` (required).
    """
    entry = subentry_entry()
    flow = _make_flow(ZoneSubentryFlowHandler, subentry_hass(entry), ZONE)

    result = asyncio.run(flow.async_step_user(None))

    assert _schema_keys(result) == _ZONE_KEYS | {_ID_KEY}


def test_zone_add_creates_a_subentry_referencing_configured_blinds(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    flow = _make_flow(ZoneSubentryFlowHandler, subentry_hass(entry), ZONE)

    result = asyncio.run(
        flow.async_step_user({"id": "terasa", "members": ["cover.a"], "occupants": ["peter"]})
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "terasa"
    assert result["data"] == {"id": "terasa", "members": ["cover.a"], "occupants": ["peter"]}


def test_zone_blocks_a_blind_already_claimed_by_another_zone(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(ZONE, {"id": "zone1", "members": ["cover.a"], "occupants": []})
    flow = _make_flow(ZoneSubentryFlowHandler, subentry_hass(entry), ZONE)

    result = asyncio.run(
        flow.async_step_user({"id": "zone2", "members": ["cover.a"], "occupants": []})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "blind_in_two_zones" in result["description_placeholders"]["error_detail"]
    # Rejected, not created: still exactly the one zone seeded above.
    assert len([s for s in entry.subentries.values() if s.subentry_type == ZONE]) == 1


def test_zone_reject_dot_in_id_surfaces_as_a_form_error(subentry_entry, subentry_hass):
    """`config_schema._reject_dot` raises `ConfigError` for a zone id containing
    '.' -- `_blocking_errors` must catch that and surface it as a form error,
    not let it propagate out of the flow step.
    """
    entry = subentry_entry()
    flow = _make_flow(ZoneSubentryFlowHandler, subentry_hass(entry), ZONE)

    result = asyncio.run(flow.async_step_user({"id": "bad.id", "members": [], "occupants": []}))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}


# ---------------------------------------------------------------------------
# value
# ---------------------------------------------------------------------------


def test_value_add_shows_a_form_with_the_expected_fields(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ValueSubentryFlowHandler, subentry_hass(entry), VALUE)

    result = asyncio.run(flow.async_step_user(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert _schema_keys(result) == {"id", "entity", "default"}


def test_value_schema_matches_config_schema_value_keys_plus_id(subentry_entry, subentry_hass):
    """Same drift guard as the blind and zone versions, for `_build_values`'s
    expectations: `config_schema._parse_values` rejects a body with keys
    outside `_VALUE_KEYS` once `_ID_KEY` has been stripped off it.
    """
    entry = subentry_entry()
    flow = _make_flow(ValueSubentryFlowHandler, subentry_hass(entry), VALUE)

    result = asyncio.run(flow.async_step_user(None))

    assert _schema_keys(result) == _VALUE_KEYS | {_ID_KEY}


def test_value_add_creates_a_subentry(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ValueSubentryFlowHandler, subentry_hass(entry), VALUE)

    result = asyncio.run(
        flow.async_step_user(
            {"id": "kvety_poz", "entity": "input_number.kvety_pozicia_zaluzie", "default": 34}
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "kvety_poz"
    assert result["data"] == {
        "id": "kvety_poz",
        "entity": "input_number.kvety_pozicia_zaluzie",
        "default": 34,
    }


def test_value_add_rejects_a_duplicate_id(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(VALUE, {"id": "kvety_poz", "entity": "input_number.a", "default": 34})
    flow = _make_flow(ValueSubentryFlowHandler, subentry_hass(entry), VALUE)

    result = asyncio.run(
        flow.async_step_user({"id": "kvety_poz", "entity": "input_number.b", "default": 10})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "already configured" in result["description_placeholders"]["error_detail"]


# ---------------------------------------------------------------------------
# the strongest test: build through the flows, compare against YAML
# ---------------------------------------------------------------------------

# Equivalent to the configuration built through the flows below: two blinds
# (one with every optional field set to a non-default value, one bare), a
# zone owning both, and one `values:` entry. No `modes:`/`rules:` on either
# side -- neither the `mode` nor `rule` subentry flow exists yet (a later
# task), so both `Config`s legitimately have `modes=()`/`rules={}`; this
# compares what *does* exist today, not the whole eventual shape.
_EQUIVALENT_YAML = """
blinds:
  - entity: cover.a
    facade_azimuth: 270
    tolerance: 30
    travel_time: 90
    has_tilt: true
    tilt_after_arrival: false
  - entity: cover.b
zones:
  terasa:
    members: [cover.a, cover.b]
    occupants: [peter]
values:
  kvety_poz:
    entity: input_number.kvety_pozicia_zaluzie
    default: 34
"""


def test_config_built_through_the_flows_matches_the_equivalent_yaml(subentry_entry, subentry_hass):
    entry = subentry_entry()
    hass = subentry_hass(entry)

    blind_a = asyncio.run(
        _make_flow(BlindSubentryFlowHandler, hass, BLIND).async_step_user(
            {
                "entity": "cover.a",
                "facade_azimuth": 270,
                "tolerance": 30,
                "travel_time": 90,
                "has_tilt": True,
                "tilt_after_arrival": False,
            }
        )
    )
    entry.add_subentry(BLIND, blind_a["data"], title=blind_a["title"])

    blind_b = asyncio.run(
        _make_flow(BlindSubentryFlowHandler, hass, BLIND).async_step_user(
            {
                "entity": "cover.b",
                "tolerance": 45,
                "travel_time": 60,
                "has_tilt": True,
                "tilt_after_arrival": True,
            }
        )
    )
    entry.add_subentry(BLIND, blind_b["data"], title=blind_b["title"])

    zone = asyncio.run(
        _make_flow(ZoneSubentryFlowHandler, hass, ZONE).async_step_user(
            {"id": "terasa", "members": ["cover.a", "cover.b"], "occupants": ["peter"]}
        )
    )
    entry.add_subentry(ZONE, zone["data"], title=zone["title"])

    value = asyncio.run(
        _make_flow(ValueSubentryFlowHandler, hass, VALUE).async_step_user(
            {"id": "kvety_poz", "entity": "input_number.kvety_pozicia_zaluzie", "default": 34}
        )
    )
    entry.add_subentry(VALUE, value["data"], title=value["title"])

    built_through_flows = config_from_subentries(entry)
    built_from_yaml = load_config(_EQUIVALENT_YAML)

    assert built_through_flows == built_from_yaml


# ---------------------------------------------------------------------------
# the regression this fix pass exists for: the *sequence* a human performs,
# not any single step in isolation. The suite above checks one step at a
# time (add a blind, add a zone, ...); that is exactly how the ordering
# deadlock this fix pass corrects (`a143b86`) passed review unnoticed --
# each step it broke, in isolation, still looked fine on its own.
# ---------------------------------------------------------------------------


def test_full_build_up_sequence_a_human_would_perform(subentry_entry, subentry_hass):
    """Walks a complete, realistic build-up in the order a person clicking
    through the UI would actually produce it, asserting every single save
    succeeds -- including the one step order that used to deadlock: adding
    a *second* blind after a zone already exists (step 3). Before this fix,
    that step failed with `blind_without_zone` and there was no way to
    recover -- the blind form has no field that could ever satisfy it (see
    `test_blind_add_is_never_blocked_by_blind_without_zone` for that same
    defect in isolation, and the task brief for the reproduction that found
    it). The finished config must still equal exactly what building the same
    configuration in one shot
    (`test_config_built_through_the_flows_matches_the_equivalent_yaml`,
    above) produces, through `config_from_subentries` -- proving the
    ownership-based exemption changes *when* a save is accepted, not *what*
    ends up saved.
    """
    entry = subentry_entry()
    hass = subentry_hass(entry)

    # 1. Add the first blind. No zone exists yet -- the uncontroversial case
    # even the pre-fix exemption handled.
    blind_a = asyncio.run(
        _make_flow(BlindSubentryFlowHandler, hass, BLIND).async_step_user(
            {
                "entity": "cover.a",
                "tolerance": 30,
                "travel_time": 90,
                "has_tilt": True,
                "tilt_after_arrival": True,
            }
        )
    )
    assert blind_a["type"] is FlowResultType.CREATE_ENTRY
    blind_a_id = entry.add_subentry(BLIND, blind_a["data"], title=blind_a["title"])

    # 2. Add a zone that claims it -- the ordinary "just added the blind,
    # now give it a zone" step.
    zone = asyncio.run(
        _make_flow(ZoneSubentryFlowHandler, hass, ZONE).async_step_user(
            {"id": "terasa", "members": ["cover.a"], "occupants": ["peter"]}
        )
    )
    assert zone["type"] is FlowResultType.CREATE_ENTRY
    zone_id = entry.add_subentry(ZONE, zone["data"], title=zone["title"])

    # 3. Add a *second* blind, now that a zone already exists -- the exact
    # step `a143b86` deadlocked on: `blind_without_zone` used to stay
    # enforced (dropped only while zero `zone` subentries existed), and a
    # blind cannot be a zone member before it exists as its own subentry, so
    # there was no order in which this step and the zone's own `members`
    # update could both succeed.
    blind_b = asyncio.run(
        _make_flow(BlindSubentryFlowHandler, hass, BLIND).async_step_user(
            {
                "entity": "cover.b",
                "tolerance": 45,
                "travel_time": 60,
                "has_tilt": True,
                "tilt_after_arrival": True,
            }
        )
    )
    assert blind_b["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(BLIND, blind_b["data"], title=blind_b["title"])

    # 4. Add a `values:` entry -- unrelated to blinds/zones, must not be
    # blocked by cover.b still sitting outside every zone at this point.
    value = asyncio.run(
        _make_flow(ValueSubentryFlowHandler, hass, VALUE).async_step_user(
            {"id": "kvety_poz", "entity": "input_number.kvety_pozicia_zaluzie", "default": 34}
        )
    )
    assert value["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(VALUE, value["data"], title=value["title"])

    # 5. Go back and claim cover.b too, by editing the zone from step 2.
    zone_edit = asyncio.run(
        _make_flow(ZoneSubentryFlowHandler, hass, ZONE, subentry_id=zone_id).async_step_reconfigure(
            {"id": "terasa", "members": ["cover.a", "cover.b"], "occupants": ["peter"]}
        )
    )
    assert zone_edit["type"] is FlowResultType.ABORT
    assert zone_edit["reason"] == "reconfigure_successful"

    # 6. Edit the first blind's own settings -- an ordinary update with
    # nothing to do with zone membership, exercised last to prove the
    # earlier steps left the entry in a state further edits still work on.
    blind_edit = asyncio.run(
        _make_flow(
            BlindSubentryFlowHandler, hass, BLIND, subentry_id=blind_a_id
        ).async_step_reconfigure(
            {
                "entity": "cover.a",
                "facade_azimuth": 270,
                "tolerance": 30,
                "travel_time": 90,
                "has_tilt": True,
                "tilt_after_arrival": False,
            }
        )
    )
    assert blind_edit["type"] is FlowResultType.ABORT
    assert blind_edit["reason"] == "reconfigure_successful"

    built = config_from_subentries(entry)
    assert built == load_config(_EQUIVALENT_YAML)


# ---------------------------------------------------------------------------
# `_CODE_OWNERS`/`_blocks_on`: a problem blocks a save only if the form being
# submitted is the one whose fields could fix it (see that pair's own
# docstring in `config_flow.py`, and the task brief this fix pass answers).
# This is also the regression coverage for the defect the fix pass exists
# for: `a143b86` blocked adding a second blind the moment any `zone`
# subentry existed at all -- an ordering deadlock, since a blind must exist
# *before* a zone can list it as a member, so the first zone a user ever
# creates permanently locks out adding another blind. That is exactly the
# scenario `test_blind_add_is_never_blocked_by_blind_without_zone` below
# drives.
# ---------------------------------------------------------------------------


def test_no_fallback_mode_never_blocks_a_value_even_once_a_mode_subentry_exists(
    subentry_entry, subentry_hass
):
    """No `mode` subentry *flow* exists yet, but `config_store.
    config_from_subentries` already reads a `mode` subentry if one is
    present -- a later task's flow, or any other producer of subentries,
    would create exactly this state. Seeded directly via `add_subentry`, the
    same way `tests/test_config_store.py` builds a fake entry with a `mode`
    subentry, to prove the point either way: a `value` form has no `when`
    field, so it is not how a user would fix `no_fallback_mode` no matter
    what else exists in the entry, and must not be blocked by it -- see
    `config_flow._CODE_OWNERS`.
    """
    entry = subentry_entry()
    entry.add_subentry(
        MODE,
        {
            "id": "den",
            "order": 0,
            "when": {"condition": "state", "entity_id": "input_boolean.a", "state": "on"},
        },
    )
    flow = _make_flow(ValueSubentryFlowHandler, subentry_hass(entry), VALUE)

    result = asyncio.run(
        flow.async_step_user(
            {"id": "kvety_poz", "entity": "input_number.kvety_pozicia_zaluzie", "default": 34}
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


def test_blind_add_is_never_blocked_by_blind_without_zone(subentry_entry, subentry_hass):
    """Reproduces the defect this fix pass exists for. Before the fix,
    `_transient_error_codes` exempted `blind_without_zone` only while zero
    `zone` subentries existed -- so the instant a user created their first
    zone, `blind_without_zone` started blocking every subsequent blind add,
    permanently: a blind must exist before a zone can list it as a member,
    so there is no way to satisfy the check for a brand new blind without
    first being allowed to save it unclaimed. The blind form has no
    `members` field; it was never the form that could fix this, at any
    point, regardless of what else exists -- see `config_flow._CODE_OWNERS`,
    which now says so directly instead of tracking zone existence.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(ZONE, {"id": "terasa", "members": ["cover.a"], "occupants": []})
    flow = _make_flow(BlindSubentryFlowHandler, subentry_hass(entry), BLIND)

    result = asyncio.run(
        flow.async_step_user(
            {
                "entity": "cover.b",
                "tolerance": 45,
                "travel_time": 60,
                "has_tilt": True,
                "tilt_after_arrival": True,
            }
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(BLIND, result["data"], title=result["title"])
    assert len([s for s in entry.subentries.values() if s.subentry_type == BLIND]) == 2


def test_zone_edit_that_orphans_a_blind_is_still_blocked(subentry_entry, subentry_hass):
    """The fix above must not widen into "blind_without_zone never blocks
    anything": a `zone` form's own `members` field is exactly what decides a
    blind's zone membership, so a zone edit that drops a blind out of
    `members` -- genuinely orphaning it -- is a problem that same form
    caused and can fix (by keeping the member, or adding it to another
    zone), and must still block. See the task brief's own example of this.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(BLIND, {"entity": "cover.b", "tolerance": 45, "travel_time": 60})
    zone_id = entry.add_subentry(
        ZONE, {"id": "terasa", "members": ["cover.a", "cover.b"], "occupants": []}
    )
    flow = _make_flow(ZoneSubentryFlowHandler, subentry_hass(entry), ZONE, subentry_id=zone_id)

    # Drop cover.b out of the zone's members -- it now belongs to no zone.
    result = asyncio.run(
        flow.async_step_reconfigure({"id": "terasa", "members": ["cover.a"], "occupants": []})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "blind_without_zone" in result["description_placeholders"]["error_detail"]
    # Rejected, not applied: the zone still owns both blinds.
    assert entry.subentries[zone_id].data["members"] == ["cover.a", "cover.b"]


# ---------------------------------------------------------------------------
# real voluptuous coercion (finding 4): every test above hands
# `async_step_user`/`async_step_reconfigure` a fully-resolved dict, matching
# what a flow step receives *after* `ConfigSubentryFlowManager` has already
# run the raw submission through `data_schema`
# (`homeassistant/data_entry_flow.py`'s `_async_handle_step`:
# `user_input = cur_step["data_schema"](user_input)`). Skipping that step
# everywhere else means a default drifting between this module's schema and
# `model.Blind`/`config_schema._parse_values`'s own defaults would pass
# every test above without complaint. These two drive the real schema
# first, the way the flow manager does, submitting only the field(s)
# voluptuous actually requires, and check the result against the *parser's*
# own default, not a literal copied by hand -- so a drift moves one of the
# two sides and still fails here.
# ---------------------------------------------------------------------------


def test_blind_minimal_submission_coerces_to_the_model_defaults(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(BlindSubentryFlowHandler, subentry_hass(entry), BLIND)

    # The exact schema object a real form would have been rendered with --
    # not a hand-built `vol.Schema` -- so this exercises what
    # `ConfigSubentryFlowManager` actually runs the submission through.
    shown = asyncio.run(flow.async_step_user(None))
    coerced = shown["data_schema"]({"entity": "cover.a"})
    result = asyncio.run(flow.async_step_user(coerced))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    expected = Blind(entity="cover.a")
    assert result["data"] == {
        "entity": expected.entity,
        "tolerance": expected.tolerance,
        "travel_time": expected.travel_time,
        "has_tilt": expected.has_tilt,
        "tilt_after_arrival": expected.tilt_after_arrival,
    }


def test_value_minimal_submission_coerces_to_the_parser_default(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ValueSubentryFlowHandler, subentry_hass(entry), VALUE)

    shown = asyncio.run(flow.async_step_user(None))
    coerced = shown["data_schema"]({"id": "kvety_poz", "entity": "input_number.a"})
    result = asyncio.run(flow.async_step_user(coerced))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    body = {k: v for k, v in result["data"].items() if k != _ID_KEY}
    assert _parse_values({"kvety_poz": body}) == {
        "kvety_poz": Ref(entity="input_number.a", default=0)
    }
