"""Tests for the `blind`/`zone`/`value`/`condition`/`mode` subentry flows in `subentry_flow.py`.

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
import json
from pathlib import Path

import pytest

pytest.importorskip("homeassistant")

from homeassistant.data_entry_flow import FlowResultType
import voluptuous as vol

import cover_logic
from cover_logic.conditions import evaluate_condition
from cover_logic.config_flow import CoverLogicConfigFlow
from cover_logic.config_schema import (
    _BLIND_KEYS,
    _RULE_KEYS,
    _VALUE_KEYS,
    _ZONE_KEYS,
    _parse_values,
    load_config,
)
from cover_logic.config_store import (
    _ID_KEY,
    BLIND,
    CONDITION,
    MODE,
    RULE,
    SUBENTRY_TYPES,
    VALUE,
    ZONE,
    _rule_body,
    config_from_subentries,
    duplicate_rule_order_problems,
    rule_owner_ids,
)
from cover_logic.const import RULE_DEFAULT_ZONE
from cover_logic.model import KEEP, Blind, Ref
from cover_logic.subentry_flow import (
    SUBENTRY_FLOW_HANDLERS,
    BlindSubentryFlowHandler,
    ConditionSubentryFlowHandler,
    ModeSubentryFlowHandler,
    RuleSubentryFlowHandler,
    ValueSubentryFlowHandler,
    ZoneSubentryFlowHandler,
)
from cover_logic.validation import ERROR, validate
from cover_logic.world import World

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


def test_every_subentry_type_the_store_reads_has_a_flow():
    """Not a hand-written list: `config_store.SUBENTRY_TYPES` is what
    `config_from_subentries` will actually read out of a saved entry, so a
    type present there and missing here is a type a user can never create
    through the UI at all -- the exact gap that existed for `rule` until this
    task.
    """
    types = CoverLogicConfigFlow.async_get_supported_subentry_types(None)

    assert types is SUBENTRY_FLOW_HANDLERS
    assert set(types) == SUBENTRY_TYPES
    assert set(types) == {BLIND, ZONE, VALUE, CONDITION, MODE, RULE}


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
    `subentry_flow._CODE_OWNERS`/`_blocks_on`.
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
# side -- this fixture exercises the blind/zone/value flows only, so both
# `Config`s legitimately have `modes=()`/`rules={}`. The full vocabulary,
# rules included, is built through the flows in
# `test_full_build_up_sequence_through_rules` at the end of this module.
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
# docstring in `subentry_flow.py`, and the task brief this fix pass answers).
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
    `subentry_flow._CODE_OWNERS`.
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
    point, regardless of what else exists -- see `subentry_flow._CODE_OWNERS`,
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


def test_a_blind_save_is_blocked_by_a_non_positive_travel_time(subentry_entry, subentry_hass):
    """`bad_travel_time` must actually block its owning form.

    A code in neither `_CODE_OWNERS` nor `_ATTRIBUTED_CODES` silently blocks
    nothing at all -- no crash, no message, just a validation error that never
    stops a save (MODELS.md §9). This is the test that says otherwise for the
    new code. The `blind` form is the one with a `travel_time` field, so it is
    the one that can fix it.

    The schema's own `min=1` means the form will not *offer* this value; a
    YAML file or a hand-edited subentry still reaches the same `Config`, which
    is why the check exists in `validation.py` rather than only in the
    selector, and why this test submits the resolved dict directly.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(ZONE, {"id": "terasa", "members": ["cover.a", "cover.b"], "occupants": []})
    flow = _make_flow(BlindSubentryFlowHandler, subentry_hass(entry), BLIND)

    result = asyncio.run(
        flow.async_step_user(
            {
                "entity": "cover.b",
                "tolerance": 45,
                "travel_time": 0,
                "has_tilt": True,
                "tilt_after_arrival": True,
            }
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "bad_travel_time" in result["description_placeholders"]["error_detail"]


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


# ---------------------------------------------------------------------------
# condition
# ---------------------------------------------------------------------------


def test_condition_add_shows_a_form_with_the_expected_fields(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    result = asyncio.run(flow.async_step_user(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert _schema_keys(result) == {"id", "condition", "numeric_state_default"}


def test_condition_add_creates_a_single_condition_subentry(subentry_entry, subentry_hass):
    """One selected condition is stored verbatim, not wrapped -- see
    `subentry_flow._flatten_condition_list`'s own docstring for why: the
    merged body a saved condition's `data` becomes (everything but `id`) must
    already look like one condition node, matching the fixture
    `tests/ha/conftest.py`'s `CONFIG_TEXT` and `tests/test_config_store.py`
    both already use for a `condition` subentry.
    """
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    result = asyncio.run(
        flow.async_step_user(
            {
                "id": "slnko",
                "condition": [
                    {"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}
                ],
            }
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "slnko"
    assert result["data"] == {
        "id": "slnko",
        "condition": "state",
        "entity_id": "sun.sun",
        "state": "above_horizon",
    }


def test_condition_add_multiple_conditions_becomes_an_explicit_and(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    result = asyncio.run(
        flow.async_step_user(
            {
                "id": "obe",
                "condition": [
                    {"condition": "state", "entity_id": "input_boolean.a", "state": "on"},
                    {"condition": "state", "entity_id": "input_boolean.b", "state": "on"},
                ],
            }
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "id": "obe",
        "condition": "and",
        "conditions": [
            {"condition": "state", "entity_id": "input_boolean.a", "state": "on"},
            {"condition": "state", "entity_id": "input_boolean.b", "state": "on"},
        ],
    }


def test_condition_reconfigure_prefills_the_selector_from_saved_data(subentry_entry, subentry_hass):
    entry = subentry_entry()
    subentry_id = entry.add_subentry(
        CONDITION,
        {"id": "slnko", "condition": "state", "entity_id": "sun.sun", "state": "above_horizon"},
    )
    flow = _make_flow(
        ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION, subentry_id=subentry_id
    )

    shown = asyncio.run(flow.async_step_reconfigure(None))

    suggested = {
        key.schema: key.description["suggested_value"]
        for key in shown["data_schema"].schema
        if isinstance(key, vol.Marker) and key.description
    }
    assert suggested == {
        "id": "slnko",
        "condition": [{"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}],
        "numeric_state_default": None,
    }


def test_condition_add_rejects_a_duplicate_id(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(
        CONDITION,
        {"id": "slnko", "condition": "state", "entity_id": "sun.sun", "state": "above_horizon"},
    )
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    result = asyncio.run(
        flow.async_step_user(
            {"id": "slnko", "condition": [{"condition": "state", "entity_id": "x", "state": "off"}]}
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "already configured" in result["description_placeholders"]["error_detail"]


def test_condition_add_blocked_by_ref_to_unknown_condition(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    result = asyncio.run(
        flow.async_step_user({"id": "b", "condition": [{"condition": "ref", "name": "neexistuje"}]})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "unknown_condition_ref" in result["description_placeholders"]["error_detail"]


def test_condition_add_blocked_by_bad_condition_shape(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    # A `state` condition missing its required `state` key -- hand-built
    # rather than run through the real `ConditionSelector`, which would
    # never let this particular shape through; the point here is that
    # `_blocking_errors` still catches it if it somehow arrived, not that the
    # selector is bypassable.
    result = asyncio.run(
        flow.async_step_user({"id": "b", "condition": [{"condition": "state", "entity_id": "x"}]})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "bad_condition_shape" in result["description_placeholders"]["error_detail"]


def test_condition_add_blocked_by_a_circular_ref(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(CONDITION, {"id": "a", "condition": "ref", "name": "b"})
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    result = asyncio.run(
        flow.async_step_user({"id": "b", "condition": [{"condition": "ref", "name": "a"}]})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "circular_condition_ref" in result["description_placeholders"]["error_detail"]


# ---------------------------------------------------------------------------
# condition: the native selector's own entity_id-list coercion (finding not
# in the task brief -- discovered while building this flow, see the task
# report's own section on it). `ConditionSelector` normalises a `state`/
# `numeric_state` condition's `entity_id` to a list via
# `cv.entity_ids_or_uuids`, even for one entity picked; `world.state`/
# `world.attribute` key their snapshot by a bare string and would raise
# `TypeError: unhashable type: 'list'` on a list, a crash `validate()` cannot
# see coming (it checks required keys, never a value's type). These two
# drive the *real* `ConditionSelector`, not a hand-built dict -- a hand-built
# one would hide the coercion this guards against entirely.
# ---------------------------------------------------------------------------


def test_condition_single_entity_via_real_selector_unwraps_the_entity_id_list(
    subentry_entry, subentry_hass
):
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    shown = asyncio.run(flow.async_step_user(None))
    coerced = shown["data_schema"](
        {
            "id": "dnu",
            "condition": [{"condition": "state", "entity_id": "input_boolean.a", "state": "on"}],
        }
    )
    result = asyncio.run(flow.async_step_user(coerced))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Not `["input_boolean.a"]` -- see the section docstring above.
    assert result["data"]["entity_id"] == "input_boolean.a"
    assert "match" not in result["data"]

    body = {k: v for k, v in result["data"].items() if k != _ID_KEY}
    assert evaluate_condition(body, World(states={"input_boolean.a": "on"})) is True
    assert evaluate_condition(body, World(states={"input_boolean.a": "off"})) is False


def test_condition_multi_entity_via_real_selector_expands_to_an_and(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    shown = asyncio.run(flow.async_step_user(None))
    coerced = shown["data_schema"](
        {
            "id": "obe",
            "condition": [
                {
                    "condition": "state",
                    "entity_id": ["input_boolean.a", "input_boolean.b"],
                    "state": "on",
                }
            ],
        }
    )
    result = asyncio.run(flow.async_step_user(coerced))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "id": "obe",
        "condition": "and",
        "conditions": [
            {"condition": "state", "entity_id": "input_boolean.a", "state": "on"},
            {"condition": "state", "entity_id": "input_boolean.b", "state": "on"},
        ],
    }

    body = {k: v for k, v in result["data"].items() if k != _ID_KEY}
    assert (
        evaluate_condition(body, World(states={"input_boolean.a": "on", "input_boolean.b": "on"}))
        is True
    )
    assert (
        evaluate_condition(body, World(states={"input_boolean.a": "on", "input_boolean.b": "off"}))
        is False
    )


# ---------------------------------------------------------------------------
# condition: `numeric_state`'s `default` (this fix pass's finding 2). HA's
# own `numeric_state` schema has no `default` key and rejects one as an
# unrecognised extra (unlike the `ALLOW_EXTRA` path this project's own three
# custom kinds get -- `numeric_state` is a kind HA already knows, so its
# schema is closed), so the field lives outside the selector --
# `_NUMERIC_STATE_DEFAULT_FIELD`, a plain `NumberSelector` -- and `_to_data`
# merges it into the flattened body's `default` key afterwards. These drive
# the *real* selector for the condition itself, the same way the
# entity-id-list tests above do, so "HA rejects `default` inside the
# selector" is exercised for real, not just asserted in a comment.
# ---------------------------------------------------------------------------


def test_condition_numeric_state_via_real_selector_needs_the_separate_default_field(
    subentry_entry, subentry_hass
):
    """The live house's own shape: one single-node `numeric_state` condition,
    `below` a threshold, with a fallback for when the sensor is unavailable.
    Previously undoable through the UI at all (task 3's own report) -- now
    buildable with the selector plus the new field, and the saved body
    evaluates exactly like a hand-written YAML `numeric_state` would.
    """
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    shown = asyncio.run(flow.async_step_user(None))
    coerced = shown["data_schema"](
        {
            "id": "vietor",
            "condition": [
                {"condition": "numeric_state", "entity_id": "sensor.vietor", "below": 40}
            ],
            "numeric_state_default": 999,
        }
    )
    result = asyncio.run(flow.async_step_user(coerced))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "id": "vietor",
        "condition": "numeric_state",
        "entity_id": "sensor.vietor",
        "below": 40.0,
        "default": 999.0,
    }

    body = {k: v for k, v in result["data"].items() if k != _ID_KEY}
    # Below the threshold: True. Sensor missing entirely: falls back to
    # `default`, which is *not* below 40 -- "a dead sensor must fall on the
    # safe side" (docs/rationale.md, "Why `numeric_state` requires an
    # explicit `default`"), exercised here through the UI-built body, not
    # just a hand-written YAML fixture.
    assert evaluate_condition(body, World(states={"sensor.vietor": "10"})) is True
    assert evaluate_condition(body, World(states={})) is False


def test_condition_numeric_state_without_the_default_field_is_blocked(
    subentry_entry, subentry_hass
):
    """No fallback supplied -- `_to_data` must not invent one. The save is
    blocked by the pre-existing `bad_condition_shape` (`numeric_state`
    missing `default`), the same loud failure a hand-written YAML config
    without `default` already gets from `validate()` -- the gap this field
    closes is "no way to supply it", not "silently permitted without it".
    """
    entry = subentry_entry()
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    shown = asyncio.run(flow.async_step_user(None))
    coerced = shown["data_schema"](
        {
            "id": "vietor",
            "condition": [
                {"condition": "numeric_state", "entity_id": "sensor.vietor", "below": 40}
            ],
        }
    )
    result = asyncio.run(flow.async_step_user(coerced))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "bad_condition_shape" in result["description_placeholders"]["error_detail"]
    assert "default" in result["description_placeholders"]["error_detail"]


def test_condition_numeric_state_reconfigure_prefills_default_separately_from_the_selector(
    subentry_entry, subentry_hass
):
    """The inverse of `_to_data`: a saved `numeric_state`'s `default` is
    pulled back out into its own field, not left in the body handed to the
    selector -- HA's schema would reject a prefill that included it.
    """
    entry = subentry_entry()
    subentry_id = entry.add_subentry(
        CONDITION,
        {
            "id": "vietor",
            "condition": "numeric_state",
            "entity_id": "sensor.vietor",
            "below": 40,
            "default": 999,
        },
    )
    flow = _make_flow(
        ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION, subentry_id=subentry_id
    )

    shown = asyncio.run(flow.async_step_reconfigure(None))

    suggested = {
        key.schema: key.description["suggested_value"]
        for key in shown["data_schema"].schema
        if isinstance(key, vol.Marker) and key.description
    }
    assert suggested == {
        "id": "vietor",
        "condition": [{"condition": "numeric_state", "entity_id": "sensor.vietor", "below": 40}],
        "numeric_state_default": 999,
    }


# ---------------------------------------------------------------------------
# mode
# ---------------------------------------------------------------------------


def test_mode_add_shows_a_form_with_the_expected_fields(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(flow.async_step_user(None))

    assert result["type"] is FlowResultType.FORM
    assert _schema_keys(result) == {"id", "order", "condition_ref", "when"}


def test_mode_condition_ref_options_are_the_configured_conditions(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(
        CONDITION,
        {"id": "slnko", "condition": "state", "entity_id": "sun.sun", "state": "above_horizon"},
    )
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(flow.async_step_user(None))

    ref_selector = next(
        val for key, val in result["data_schema"].schema.items() if key.schema == "condition_ref"
    )
    assert ref_selector.config["options"] == ["slnko"]


def test_mode_fallback_add_creates_a_subentry_with_no_when_key(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(flow.async_step_user({"id": "bezny", "order": 0}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"id": "bezny", "order": 0}


def test_mode_ref_add_creates_a_subentry_with_the_parsed_ref_shape(subentry_entry, subentry_hass):
    """`when` becomes `{"condition": "ref", "name": ...}`, not this project's own
    `{"ref": ...}` subentry marker -- see `ModeSubentryFlowHandler._to_data`'s
    own docstring for why the marker (which `config_store._to_reftag` turns
    into an eagerly-checked `RefTag`) is deliberately avoided here.

    A fallback mode is seeded first: with zero modes existing, a lone
    conditioned mode would itself be blocked by `no_fallback_mode` before
    this test ever got to check the `when` shape -- see
    `test_mode_add_first_mode_with_a_condition_is_blocked_until_a_fallback_exists`.
    """
    entry = subentry_entry()
    entry.add_subentry(
        CONDITION,
        {"id": "slnko", "condition": "state", "entity_id": "sun.sun", "state": "above_horizon"},
    )
    entry.add_subentry(MODE, {"id": "bezny", "order": 100})
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(
        flow.async_step_user({"id": "slnecno", "order": 0, "condition_ref": "slnko"})
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "id": "slnecno",
        "order": 0,
        "when": {"condition": "ref", "name": "slnko"},
    }


def test_mode_inline_add_creates_a_subentry_with_a_bare_list(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(MODE, {"id": "bezny", "order": 100})
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(
        flow.async_step_user(
            {
                "id": "slnecno",
                "order": 0,
                "when": [{"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}],
            }
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "id": "slnecno",
        "order": 0,
        "when": [{"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}],
    }


def test_mode_inline_via_real_selector_also_unwraps_the_entity_id_list(
    subentry_entry, subentry_hass
):
    """Same fix as `condition`'s, exercised through `mode`'s own inline field --
    `_normalize_condition_tree` is shared, not reimplemented, but nothing
    stops the two call sites from drifting if one forgets to call it.
    """
    entry = subentry_entry()
    entry.add_subentry(MODE, {"id": "bezny", "order": 100})
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    shown = asyncio.run(flow.async_step_user(None))
    coerced = shown["data_schema"](
        {
            "id": "slnecno",
            "order": 0,
            "when": [{"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}],
        }
    )
    result = asyncio.run(flow.async_step_user(coerced))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["when"] == [
        {"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}
    ]


def test_mode_rejects_both_a_named_and_an_inline_condition(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(
        CONDITION,
        {"id": "slnko", "condition": "state", "entity_id": "sun.sun", "state": "above_horizon"},
    )
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(
        flow.async_step_user(
            {
                "id": "m",
                "order": 0,
                "condition_ref": "slnko",
                "when": [{"condition": "state", "entity_id": "input_boolean.a", "state": "on"}],
            }
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "not both" in result["description_placeholders"]["error_detail"]


def test_mode_reconfigure_prefills_ref_inline_and_fallback_shapes(subentry_entry, subentry_hass):
    entry = subentry_entry()
    hass = subentry_hass(entry)
    entry.add_subentry(
        CONDITION,
        {"id": "slnko", "condition": "state", "entity_id": "sun.sun", "state": "above_horizon"},
    )
    ref_id = entry.add_subentry(
        MODE, {"id": "a", "order": 0, "when": {"condition": "ref", "name": "slnko"}}
    )
    inline_id = entry.add_subentry(
        MODE,
        {
            "id": "b",
            "order": 1,
            "when": {"condition": "state", "entity_id": "input_boolean.x", "state": "on"},
        },
    )
    fallback_id = entry.add_subentry(MODE, {"id": "c", "order": 2})

    def suggested_of(subentry_id):
        shown = asyncio.run(
            _make_flow(
                ModeSubentryFlowHandler, hass, MODE, subentry_id=subentry_id
            ).async_step_reconfigure(None)
        )
        return {
            key.schema: key.description["suggested_value"]
            for key in shown["data_schema"].schema
            if isinstance(key, vol.Marker) and key.description
        }

    ref_suggested = suggested_of(ref_id)
    assert ref_suggested["condition_ref"] == "slnko"
    assert ref_suggested["when"] == []

    inline_suggested = suggested_of(inline_id)
    assert inline_suggested["condition_ref"] is None
    assert inline_suggested["when"] == [
        {"condition": "state", "entity_id": "input_boolean.x", "state": "on"}
    ]

    fallback_suggested = suggested_of(fallback_id)
    assert fallback_suggested["condition_ref"] is None
    assert fallback_suggested["when"] == []


def test_mode_add_first_mode_with_a_condition_is_blocked_until_a_fallback_exists(
    subentry_entry, subentry_hass
):
    """The very first `mode` a user saves must be the fallback: with zero
    modes yet, one carrying a condition leaves `no_fallback_mode` unresolved.
    Unlike the blind/zone deadlock the previous task's fix pass exists for,
    there is always a way out -- add the fallback (no condition) first, or
    at any point give it the highest `order` -- so this is a deliberate
    ordering constraint `validate()` already enforces, not a defect. See the
    task report's own note on this.
    """
    entry = subentry_entry()
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(
        flow.async_step_user(
            {
                "id": "slnecno",
                "order": 0,
                "when": [{"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}],
            }
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert "no_fallback_mode" in result["description_placeholders"]["error_detail"]


def test_mode_add_blocked_by_ordering_that_displaces_the_fallback(subentry_entry, subentry_hass):
    entry = subentry_entry()
    entry.add_subentry(MODE, {"id": "bezny", "order": 0})  # the only mode, so currently last
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(
        flow.async_step_user(
            {
                "id": "slnecno",
                "order": 1,  # higher than the fallback's -- would sort after it
                "when": [{"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}],
            }
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "fallback_mode_not_last" in result["description_placeholders"]["error_detail"]


def test_mode_add_blocked_by_ref_to_unknown_condition(subentry_entry, subentry_hass):
    entry = subentry_entry()
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(
        flow.async_step_user({"id": "slnecno", "order": 0, "condition_ref": "neexistuje"})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "unknown_condition_ref" in result["description_placeholders"]["error_detail"]


def test_unknown_condition_ref_never_blocks_an_unrelated_blind_add(subentry_entry, subentry_hass):
    """A dangling ref left behind by, e.g., deleting the `condition` subentry
    it named (see the task report's "dangling ref" discussion -- Home
    Assistant's own subentry removal has no hook this project can veto it
    with) must not stop an unrelated blind add: a blind form has no
    condition field of any kind and cannot possibly be how a user fixes it.
    """
    entry = subentry_entry()
    entry.add_subentry(
        MODE, {"id": "m", "order": 0, "when": {"condition": "ref", "name": "neexistuje"}}
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

    assert result["type"] is FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Attribution (this fix pass's finding 1): `unknown_condition_ref`/
# `bad_condition_shape`/`circular_condition_ref` used to map to the whole
# *set* of types `{condition, mode, rule}` (`_CODE_OWNERS`), so a dangling
# ref anywhere among them blocked every save of any of those three types --
# not just the one that could fix it. `_blocks_on` now checks `Problem.
# owners`, the specific `(subentry_type, id)` `validate()` already names in
# its own message (`"mode 'm1' refers to unknown condition 'c1'"`), instead
# of the type alone. `test_unknown_condition_ref_never_blocks_an_unrelated_
# blind_add` above already proves the blind case (never blocked, before or
# after this fix, since `blind` was never in the coarse set to begin with);
# these three are the regression coverage for the same-type case the coarse
# set actually got wrong.
# ---------------------------------------------------------------------------


def test_unknown_condition_ref_in_one_mode_never_blocks_saving_an_unrelated_condition(
    subentry_entry, subentry_hass
):
    """`m1`'s dangling ref to `c1` must not block adding an unrelated,
    unconnected `condition` -- a new `condition` subentry has nothing to do
    with `m1`'s own `when` and cannot possibly be how a user fixes it.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(ZONE, {"id": "z", "members": ["cover.a"], "occupants": []})
    entry.add_subentry(MODE, {"id": "m1", "order": 0, "when": {"condition": "ref", "name": "c1"}})
    entry.add_subentry(MODE, {"id": "fallback", "order": 10})
    flow = _make_flow(ConditionSubentryFlowHandler, subentry_hass(entry), CONDITION)

    result = asyncio.run(
        flow.async_step_user(
            {
                "id": "c2",
                "condition": [
                    {"condition": "state", "entity_id": "input_boolean.x", "state": "on"}
                ],
            }
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


def test_unknown_condition_ref_in_one_mode_never_blocks_saving_an_unrelated_mode(
    subentry_entry, subentry_hass
):
    """Same scenario, saving an unrelated `mode` instead of a `condition`:
    `m1`'s own dangling ref is `m1`'s problem, fixable only from `m1`'s own
    reconfigure form -- not something a brand new `m3`, with no condition of
    its own yet, could have caused or could resolve. Before this fix,
    `_CODE_OWNERS`'s coarse `{condition, mode, rule}` set blocked this save
    too, purely because `m3` is also a `mode`.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(ZONE, {"id": "z", "members": ["cover.a"], "occupants": []})
    entry.add_subentry(MODE, {"id": "m1", "order": 0, "when": {"condition": "ref", "name": "c1"}})
    entry.add_subentry(MODE, {"id": "fallback", "order": 10})
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    # `m3` needs its own condition -- not the point under test here, but a
    # fallback-less `m3` sitting before `order: 10`'s `fallback` would itself
    # trigger the unrelated `fallback_mode_not_last` (correctly, since a
    # mode's own `order` really is what that check is about) and muddy what
    # this test is isolating.
    result = asyncio.run(
        flow.async_step_user(
            {
                "id": "m3",
                "order": 5,
                "when": [{"condition": "state", "entity_id": "input_boolean.y", "state": "on"}],
            }
        )
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


def test_a_modes_own_dangling_ref_still_blocks_that_same_mode_alongside_an_unrelated_one(
    subentry_entry, subentry_hass
):
    """The other half: attribution must not overcorrect into "a dangling ref
    never blocks a mode again". `m3`'s own `when` refs a nonexistent
    condition -- its own form's field caused this, and is exactly where a
    user would fix it -- so it must still block, even while the unrelated
    `m1` has a dangling ref of its own sitting right next to it. The error
    detail must name `m3`, confirming the block is attributed to the save
    actually being made, not merely "some `unknown_condition_ref` exists
    somewhere" the way the coarse type check would have let through.
    """
    entry = subentry_entry()
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(ZONE, {"id": "z", "members": ["cover.a"], "occupants": []})
    entry.add_subentry(MODE, {"id": "m1", "order": 0, "when": {"condition": "ref", "name": "c1"}})
    entry.add_subentry(MODE, {"id": "fallback", "order": 10})
    flow = _make_flow(ModeSubentryFlowHandler, subentry_hass(entry), MODE)

    result = asyncio.run(flow.async_step_user({"id": "m3", "order": 5, "condition_ref": "c2"}))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert (
        "mode 'm3' refers to unknown condition 'c2'"
        in (result["description_placeholders"]["error_detail"])
    )


# ---------------------------------------------------------------------------
# the build-up sequence a human performs, extended to `condition`/`mode` --
# same rationale as `test_full_build_up_sequence_a_human_would_perform`
# above (which stops at blind/zone/value, the previous task's scope): every
# step here is asserted to succeed on its own, in the order a person would
# actually click through it, not just checked as one config assembled in a
# single shot.
# ---------------------------------------------------------------------------


def test_full_build_up_sequence_condition_and_mode(subentry_entry, subentry_hass):
    entry = subentry_entry()
    hass = subentry_hass(entry)

    # 1. A blind and a zone -- the minimum a mode/condition pair needs to
    # decide anything at all.
    blind = asyncio.run(
        _make_flow(BlindSubentryFlowHandler, hass, BLIND).async_step_user(
            {
                "entity": "cover.a",
                "tolerance": 45,
                "travel_time": 60,
                "has_tilt": True,
                "tilt_after_arrival": True,
            }
        )
    )
    assert blind["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(BLIND, blind["data"], title=blind["title"])

    zone = asyncio.run(
        _make_flow(ZoneSubentryFlowHandler, hass, ZONE).async_step_user(
            {"id": "terasa", "members": ["cover.a"], "occupants": []}
        )
    )
    assert zone["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(ZONE, zone["data"], title=zone["title"])

    # 2. A named condition.
    cond_a = asyncio.run(
        _make_flow(ConditionSubentryFlowHandler, hass, CONDITION).async_step_user(
            {
                "id": "slnko",
                "condition": [
                    {"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}
                ],
            }
        )
    )
    assert cond_a["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(CONDITION, cond_a["data"], title=cond_a["title"])

    # 3. A second condition that refs the first. No dedicated "reference a
    # condition" field exists on this form the way `mode` has one (see
    # `ConditionSubentryFlowHandler`'s own docstring) -- but the native
    # selector accepts `condition: ref` directly wherever any other
    # condition dict is accepted (finding 1: HA validates an unrecognised
    # `condition:` value with `extra=ALLOW_EXTRA`, not a closed enum), so
    # composing one named condition out of another is already possible with
    # no extra code, the same way a user would type it in the selector's own
    # YAML-editing mode.
    cond_b = asyncio.run(
        _make_flow(ConditionSubentryFlowHandler, hass, CONDITION).async_step_user(
            {
                "id": "slnko_a_doma",
                "condition": [
                    {
                        "condition": "and",
                        "conditions": [
                            {"condition": "ref", "name": "slnko"},
                            {
                                "condition": "state",
                                "entity_id": "input_boolean.doma",
                                "state": "on",
                            },
                        ],
                    }
                ],
            }
        )
    )
    assert cond_b["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(CONDITION, cond_b["data"], title=cond_b["title"])

    # 4. The fallback mode, added first with the highest `order` so nothing
    # added below it can ever displace it from last place.
    fallback = asyncio.run(
        _make_flow(ModeSubentryFlowHandler, hass, MODE).async_step_user(
            {"id": "bezny", "order": 100}
        )
    )
    assert fallback["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(MODE, fallback["data"], title=fallback["title"])

    # 5. A second mode, referencing the second (composed) condition by name,
    # at a lower `order` so it is tried first.
    conditional = asyncio.run(
        _make_flow(ModeSubentryFlowHandler, hass, MODE).async_step_user(
            {"id": "slnecno", "order": 0, "condition_ref": "slnko_a_doma"}
        )
    )
    assert conditional["type"] is FlowResultType.CREATE_ENTRY
    assert conditional["data"]["when"] == {"condition": "ref", "name": "slnko_a_doma"}
    entry.add_subentry(MODE, conditional["data"], title=conditional["title"])

    # The finished entry loads cleanly: no ERROR-severity problem, modes in
    # `order` order with the fallback last.
    built = config_from_subentries(entry)
    assert [m.id for m in built.modes] == ["slnecno", "bezny"]
    assert built.modes[0].when == {"condition": "ref", "name": "slnko_a_doma"}
    assert built.modes[1].when is None
    problems = validate(built) + duplicate_rule_order_problems(entry)
    assert [p for p in problems if p.severity == ERROR] == []


# ---------------------------------------------------------------------------
# rule
#
# The one type where order is meaning: `engine._apply_rules` takes the first
# rule whose events and condition match, so a rule's `order` relative to its
# neighbours in the same `(mode, zone)` pair *is* what the house does. Adding
# is two steps -- pick the pair, then fill in the rule with `order` already
# defaulted to "append here" -- so these tests drive both, in sequence,
# through `_add_rule` below rather than calling the second step directly.
# ---------------------------------------------------------------------------


def _rule_scaffold(entry, *, values=(), conditions=()):
    """Seed the minimum a rule needs to exist: a blind, a zone, a fallback mode."""
    entry.add_subentry(BLIND, {"entity": "cover.a", "tolerance": 45, "travel_time": 60})
    entry.add_subentry(ZONE, {"id": "terasa", "members": ["cover.a"], "occupants": []})
    entry.add_subentry(MODE, {"id": "bezny", "order": 0})
    for value_id in values:
        entry.add_subentry(
            VALUE, {"id": value_id, "entity": f"input_number.{value_id}", "default": 34}
        )
    for condition_id in conditions:
        entry.add_subentry(
            CONDITION,
            {
                "id": condition_id,
                "condition": "state",
                "entity_id": "sun.sun",
                "state": "above_horizon",
            },
        )
    return entry


def _start_rule(hass, entry, mode, zone):
    """Run step one of the add flow and return `(flow, the step-two form)`."""
    flow = _make_flow(RuleSubentryFlowHandler, hass, RULE)
    shown = asyncio.run(flow.async_step_user({"mode": mode, "zone": zone}))
    assert shown["type"] is FlowResultType.FORM
    assert shown["step_id"] == "rule"
    return flow, shown


def _add_rule(hass, entry, submission, *, save=True):
    """Drive both add steps and, unless told otherwise, attach the result to `entry`."""
    flow, _shown = _start_rule(hass, entry, submission["mode"], submission["zone"])
    result = asyncio.run(flow.async_step_rule(submission))
    if save and result["type"] is FlowResultType.CREATE_ENTRY:
        entry.add_subentry(RULE, result["data"], title=result["title"])
    return result


def _suggested(shown):
    """The values a shown form is pre-filled with."""
    return {
        key.schema: key.description["suggested_value"]
        for key in shown["data_schema"].schema
        if isinstance(key, vol.Marker) and key.description
    }


def test_rule_step_one_offers_only_configured_modes_and_zones(subentry_entry, subentry_hass):
    """Never free text: a rule filed under a pair that does not exist is
    `unknown_rule_key`, and there is no reason to let the UI produce one.
    `zone` also always offers `const.RULE_DEFAULT_ZONE` ("*") alongside the
    real, configured zones -- picking it is how a rule becomes a mode-wide
    default (phase 6 task 2) rather than one zone's own.
    """
    entry = _rule_scaffold(subentry_entry())
    entry.add_subentry(MODE, {"id": "noc", "order": 10})
    entry.add_subentry(ZONE, {"id": "spalna", "members": [], "occupants": []})
    flow = _make_flow(RuleSubentryFlowHandler, subentry_hass(entry), RULE)

    shown = asyncio.run(flow.async_step_user(None))

    assert shown["type"] is FlowResultType.FORM
    assert shown["step_id"] == "user"
    assert _schema_keys(shown) == {"mode", "zone"}
    options = {
        key.schema: val.config["options"] for key, val in shown["data_schema"].schema.items()
    }
    assert options == {"mode": ["bezny", "noc"], "zone": ["spalna", "terasa", "*"]}


def test_rule_step_two_shows_every_field(subentry_entry, subentry_hass):
    entry = _rule_scaffold(subentry_entry())
    _flow, shown = _start_rule(subentry_hass(entry), entry, "bezny", "terasa")

    assert _schema_keys(shown) == {
        "mode",
        "zone",
        "order",
        "if_ref",
        "if",
        "position",
        "tilt",
        "events",
        "name",
    }


def test_rule_build_schema_offers_the_wildcard_zone_too(subentry_entry, subentry_hass):
    """Step one's own picker (`_rule_pick_schema`) offering `RULE_DEFAULT_ZONE`
    is pinned above (`test_rule_step_one_offers_only_configured_modes_and_
    zones`), but `RuleSubentryFlowHandler._build_schema` -- step two's own
    form, reused unchanged for both `async_step_rule` and a reconfigure --
    builds its `zone` field separately, from the same `_zone_options` helper.
    Nothing asserted that it did too: a revert of that one call site (back to
    only `_configured_ids(entry, ZONE)`) would make it impossible to turn an
    existing rule into a mode default through the edit form, and the suite
    would still pass.
    """
    entry = _rule_scaffold(subentry_entry())
    handler = RuleSubentryFlowHandler()

    schema = handler._build_schema(entry)  # noqa: SLF001

    zone_field = next(key for key in schema.schema if key.schema == "zone")
    assert RULE_DEFAULT_ZONE in schema.schema[zone_field].config["options"]


def test_rule_order_defaults_to_the_highest_in_that_pair_plus_ten(subentry_entry, subentry_hass):
    """The brief's "appending needs no thought". Also the reason adding is two
    steps at all: the default cannot be computed until the pair is known, and
    the pair is what step one asks for.
    """
    entry = _rule_scaffold(subentry_entry())
    entry.add_subentry(MODE, {"id": "noc", "order": 10})
    hass = subentry_hass(entry)

    # Nothing in this pair yet -- start at 0, not at 10.
    _flow, first = _start_rule(hass, entry, "bezny", "terasa")
    assert _suggested(first)["order"] == 0

    entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 0, "then": {"position": "keep"}}
    )
    entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 30, "then": {"position": "keep"}}
    )
    _flow, second = _start_rule(hass, entry, "bezny", "terasa")
    assert _suggested(second)["order"] == 40

    # Highest *in that pair*, not in the entry: a different mode starts over.
    _flow, other = _start_rule(hass, entry, "noc", "terasa")
    assert _suggested(other)["order"] == 0


def test_rule_step_two_prefills_both_axes_with_keep(subentry_entry, subentry_hass):
    """`keep` is the do-nothing action, so it is the only safe default for a
    form that decides where a physical blind goes.
    """
    entry = _rule_scaffold(subentry_entry())
    _flow, shown = _start_rule(subentry_hass(entry), entry, "bezny", "terasa")

    suggested = _suggested(shown)
    assert suggested["position"] == "keep"
    assert suggested["tilt"] == "keep"


def test_rule_writes_only_keys_config_store_reads(subentry_entry, subentry_hass):
    """The drift guard the other types have, in the shape a rule needs it.

    A rule's form fields are deliberately *not* its data keys (`if_ref`
    folds into `if`, `position`/`tilt` fold into `then`), so the blind/zone
    check -- "the form's field set equals the reader's key set" -- cannot be
    reused. What must hold instead is that every key `_to_data` emits is one
    `config_store` actually reads: `mode`/`zone`/`order` for the grouping,
    and nothing outside `_RULE_KEYS` for the body, since `_rule_body` drops
    anything else on the floor without a word.
    """
    entry = _rule_scaffold(subentry_entry(), values=["poz"], conditions=["slnko"])

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 0,
            "if_ref": "slnko",
            "position": "poz",
            "tilt": "100",
            "events": ["arrival"],
            "name": "tienenie",
        },
        save=False,
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    body = set(result["data"]) - {"mode", "zone", "order"}
    assert body <= _RULE_KEYS
    # Nothing written is silently dropped on the way to `_parse_rule`.
    assert set(_rule_body(result["data"])) == body


def test_rule_axes_accept_a_number_keep_and_a_value_ref(subentry_entry, subentry_hass):
    """The brief's hard requirement: numbers only would make a large share of
    the live configuration unexpressible (`fixtures/dom_peter.yaml` uses all
    three forms, often within one rule). Asserted against the loaded `Config`,
    not just the saved dict, so "it wrote something" is not mistaken for "it
    means the right thing".
    """
    entry = _rule_scaffold(subentry_entry(), values=["kvety_poz"])
    hass = subentry_hass(entry)

    ref_and_number = _add_rule(
        hass,
        entry,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 0,
            "position": "kvety_poz",
            "tilt": "100",
        },
    )
    assert ref_and_number["type"] is FlowResultType.CREATE_ENTRY
    assert ref_and_number["data"]["then"] == {"position": {"ref": "kvety_poz"}, "tilt": 100}

    keeps = _add_rule(
        hass,
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 10, "position": "keep", "tilt": "keep"},
    )
    assert keeps["type"] is FlowResultType.CREATE_ENTRY
    assert keeps["data"]["then"] == {"position": "keep", "tilt": "keep"}

    rules = config_from_subentries(entry).rules["bezny.terasa"]
    assert rules[0].then.position == Ref(entity="input_number.kvety_poz", default=34)
    assert rules[0].then.tilt == 100
    assert rules[1].then.position is KEEP
    assert rules[1].then.tilt is KEEP


def test_rule_axis_that_names_nothing_is_blocked_not_guessed(subentry_entry, subentry_hass):
    """A typed axis that is neither `keep`, a configured value, nor a number
    must fail loudly. `_axis_to_data` passes it through untouched precisely so
    `config_schema._parse_axis` raises the same `ConfigError` hand-written
    YAML would get -- the alternative, quietly falling back to `keep`, would
    mean a blind silently not moving with nothing to look at.
    """
    entry = _rule_scaffold(subentry_entry())

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 0, "position": "kvety_poz", "tilt": "keep"},
        save=False,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "action axis" in result["description_placeholders"]["error_detail"]


def test_rule_out_of_range_axis_is_blocked(subentry_entry, subentry_hass):
    entry = _rule_scaffold(subentry_entry())

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 0, "position": "150", "tilt": "keep"},
        save=False,
    )

    assert result["type"] is FlowResultType.FORM
    assert "0..100" in result["description_placeholders"]["error_detail"]


def test_rule_with_no_events_omits_the_key_rather_than_writing_an_empty_list(
    subentry_entry, subentry_hass
):
    """Not cosmetic. `Rule.events = None` means "any event"; an empty
    `frozenset` makes `engine._apply_rules`'s `world.event.kind not in
    rule.events` true for every event, so the rule can never fire. Writing
    `events: []` for "the user picked nothing" would turn every rule added
    through the UI into a dead one.
    """
    entry = _rule_scaffold(subentry_entry())

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 0,
            "position": "keep",
            "tilt": "keep",
            "events": [],
            "name": "",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "events" not in result["data"]
    assert "name" not in result["data"]
    assert config_from_subentries(entry).rules["bezny.terasa"][0].events is None


def test_rule_with_events_scopes_the_rule_to_them(subentry_entry, subentry_hass):
    entry = _rule_scaffold(subentry_entry())

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 0,
            "position": "keep",
            "tilt": "keep",
            "events": ["arrival"],
        },
    )

    assert result["data"]["events"] == ["arrival"]
    assert config_from_subentries(entry).rules["bezny.terasa"][0].events == frozenset({"arrival"})


def test_rule_title_shows_order_pair_and_action(subentry_entry, subentry_hass):
    """The brief: the subentry list must read without opening rows. `order`
    leads because Home Assistant lists subentries by title, so a title-sorted
    list is very nearly the order the engine tries the rules in.
    """
    entry = _rule_scaffold(subentry_entry(), values=["kvety_poz"])
    hass = subentry_hass(entry)

    plain = _add_rule(
        hass,
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 0, "position": "40", "tilt": "keep"},
    )
    assert plain["title"] == "0 bezny.terasa -> 40/keep"

    named = _add_rule(
        hass,
        entry,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 10,
            "position": "kvety_poz",
            "tilt": "100",
            "name": "tienenie",
        },
    )
    assert named["title"] == "10 bezny.terasa tienenie -> kvety_poz/100"


def test_rule_if_ref_writes_the_parsed_ref_shape(subentry_entry, subentry_hass):
    """Same reasoning as `mode`'s: `{"ref": ...}` would become a `RefTag` whose
    target `_parse_condition` checks eagerly, so deleting the named condition
    would raise `ConfigError` and block every save of every type instead of
    producing one attributable `unknown_condition_ref`.
    """
    entry = _rule_scaffold(subentry_entry(), conditions=["slnko"])

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 0,
            "if_ref": "slnko",
            "position": "keep",
            "tilt": "0",
        },
    )

    assert result["data"]["if"] == {"condition": "ref", "name": "slnko"}
    assert config_from_subentries(entry).rules["bezny.terasa"][0].when == {
        "condition": "ref",
        "name": "slnko",
    }


def test_rule_rejects_both_a_named_and_an_inline_condition(subentry_entry, subentry_hass):
    entry = _rule_scaffold(subentry_entry(), conditions=["slnko"])

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 0,
            "if_ref": "slnko",
            "if": [{"condition": "state", "entity_id": "input_boolean.a", "state": "on"}],
            "position": "keep",
            "tilt": "keep",
        },
        save=False,
    )

    assert result["type"] is FlowResultType.FORM
    assert "not both" in result["description_placeholders"]["error_detail"]


def test_rule_inline_condition_via_the_real_selector_unwraps_the_entity_id_list(
    subentry_entry, subentry_hass
):
    """`_normalize_condition_tree` is shared with `condition`/`mode`, but a
    third call site is a third chance to forget to call it -- and forgetting
    is not caught by `validate()`, it is a `TypeError: unhashable type:
    'list'` the first time the engine runs the rule. Drives the *real*
    `ConditionSelector`, since a hand-built dict never shows the coercion.
    """
    entry = _rule_scaffold(subentry_entry())
    hass = subentry_hass(entry)

    flow, shown = _start_rule(hass, entry, "bezny", "terasa")
    coerced = shown["data_schema"](
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 0,
            "if": [{"condition": "state", "entity_id": "input_boolean.a", "state": "on"}],
            "position": "keep",
            "tilt": "0",
        }
    )
    result = asyncio.run(flow.async_step_rule(coerced))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["if"] == [
        {"condition": "state", "entity_id": "input_boolean.a", "state": "on"}
    ]
    entry.add_subentry(RULE, result["data"], title=result["title"])
    rule = config_from_subentries(entry).rules["bezny.terasa"][0]
    assert evaluate_condition(rule.when, World(states={"input_boolean.a": "on"})) is True
    assert evaluate_condition(rule.when, World(states={"input_boolean.a": "off"})) is False


def test_rule_reconfigure_prefills_every_field_and_can_move_the_rule(subentry_entry, subentry_hass):
    entry = _rule_scaffold(subentry_entry(), values=["kvety_poz"], conditions=["slnko"])
    entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 0, "then": {"position": "keep"}}
    )
    subentry_id = entry.add_subentry(
        RULE,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 10,
            "if": {"condition": "ref", "name": "slnko"},
            "then": {"position": {"ref": "kvety_poz"}, "tilt": 100},
            "events": ["arrival"],
            "name": "tienenie",
        },
    )
    flow = _make_flow(RuleSubentryFlowHandler, subentry_hass(entry), RULE, subentry_id=subentry_id)

    shown = asyncio.run(flow.async_step_reconfigure(None))
    assert shown["step_id"] == "reconfigure"
    assert _suggested(shown) == {
        "mode": "bezny",
        "zone": "terasa",
        "order": 10,
        "if_ref": "slnko",
        "if": [],
        "position": "kvety_poz",
        "tilt": "100",
        "events": ["arrival"],
        "name": "tienenie",
    }

    # Move it ahead of the other rule -- the edit that only means anything
    # because order is semantics.
    result = asyncio.run(
        flow.async_step_reconfigure(
            {
                "mode": "bezny",
                "zone": "terasa",
                "order": -10,
                "if_ref": "slnko",
                "position": "kvety_poz",
                "tilt": "100",
                "events": ["arrival"],
                "name": "tienenie",
            }
        )
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert [r.name for r in config_from_subentries(entry).rules["bezny.terasa"]] == [
        "tienenie",
        "",
    ]


def test_rule_reconfigure_round_trips_an_inline_condition(subentry_entry, subentry_hass):
    """The prefill must be a real inverse of what was saved: opening a rule's
    form and pressing submit without touching anything must write the same
    bytes back. A lossy round-trip here would quietly rewrite a rule's
    condition, action or event scope every time it was merely looked at.

    Resubmits the form's own *suggested values* -- what the browser would
    send back untouched -- rather than calling `_to_form_values` directly, so
    the prefill path being exercised is the one a user actually goes through.
    """
    entry = _rule_scaffold(subentry_entry())
    hass = subentry_hass(entry)
    added = _add_rule(
        hass,
        entry,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 0,
            "if": [{"condition": "state", "entity_id": "input_boolean.a", "state": "on"}],
            "position": "40",
            "tilt": "keep",
            "events": ["arrival"],
            "name": "n",
        },
    )
    subentry_id = next(sid for sid, sub in entry.subentries.items() if sub.subentry_type == RULE)
    flow = _make_flow(RuleSubentryFlowHandler, hass, RULE, subentry_id=subentry_id)

    shown = asyncio.run(flow.async_step_reconfigure(None))
    untouched = {key: value for key, value in _suggested(shown).items() if value is not None}
    resubmitted = asyncio.run(flow.async_step_reconfigure(untouched))

    assert resubmitted["type"] is FlowResultType.ABORT
    assert entry.subentries[subentry_id].data == added["data"]


# ---------------------------------------------------------------------------
# rule: attribution. `duplicate_rule_order` and `unknown_rule_key` are the two
# ERROR codes a rule owns, and both name one `(mode, zone)` pair. Before this
# task they were mapped to the whole `rule` *type* (`_CODE_OWNERS`), so either
# one, left anywhere in the entry, blocked every rule save -- the same defect
# class that made adding a second blind impossible in task 2 and blocked
# unrelated condition saves in task 3. `Problem.owners` now names the specific
# rules, matched by the `"<mode>.<zone>#<index>"` identity
# `config_store.rule_owner_ids` maps a real subentry id onto.
# ---------------------------------------------------------------------------


def test_rule_duplicate_order_in_the_same_pair_is_blocked(subentry_entry, subentry_hass):
    """Attribution must not overcorrect into "a tie never blocks". Two rules in
    one pair claiming one `order` is genuinely ambiguous -- and unrecoverable
    once sorted into a tuple -- so the save that creates it must fail.
    """
    entry = _rule_scaffold(subentry_entry())
    entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 10, "then": {"position": "keep"}}
    )

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 10, "position": "0", "tilt": "keep"},
        save=False,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert "duplicate_rule_order" in result["description_placeholders"]["error_detail"]


def test_a_tie_in_one_pair_does_not_block_adding_a_rule_to_another(subentry_entry, subentry_hass):
    """The defect class. A tie can arrive without this flow -- a YAML import,
    a hand-edited `.storage` -- and the form for a *different* pair has no
    field that could resolve it. Blocking it there leaves the user staring at
    an error about rules they are not editing.
    """
    entry = _rule_scaffold(subentry_entry())
    entry.add_subentry(MODE, {"id": "noc", "order": 10})
    entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 10, "then": {"position": "keep"}}
    )
    entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 10, "then": {"position": 0}}
    )

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {"mode": "noc", "zone": "terasa", "order": 0, "position": "0", "tilt": "0"},
        save=False,
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


def test_a_tie_elsewhere_does_not_block_fixing_one_of_the_tied_rules(subentry_entry, subentry_hass):
    """The other half of the escape: the rules that *are* tied must still be
    editable, or attribution would have replaced one dead end with another.
    """
    entry = _rule_scaffold(subentry_entry())
    entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 10, "then": {"position": "keep"}}
    )
    tied_id = entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 10, "then": {"position": 0}}
    )
    flow = _make_flow(RuleSubentryFlowHandler, subentry_hass(entry), RULE, subentry_id=tied_id)

    result = asyncio.run(
        flow.async_step_reconfigure(
            {"mode": "bezny", "zone": "terasa", "order": 20, "position": "0", "tilt": "keep"}
        )
    )

    assert result["type"] is FlowResultType.ABORT
    assert duplicate_rule_order_problems(entry) == []


def test_a_rule_stranded_by_a_deleted_mode_does_not_block_an_unrelated_rule_add(
    subentry_entry, subentry_hass
):
    """`unknown_rule_key`, same defect class. Home Assistant offers no veto on
    subentry removal, so deleting a mode strands every rule filed under it --
    and before attribution that stranded rule blocked adding any rule at all,
    including to the healthy pairs the user still has.
    """
    entry = _rule_scaffold(subentry_entry())
    entry.add_subentry(
        RULE, {"mode": "zmazany", "zone": "terasa", "order": 0, "then": {"position": "keep"}}
    )

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 0, "position": "keep", "tilt": "keep"},
        save=False,
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


def test_a_stranded_rule_still_blocks_its_own_save(subentry_entry, subentry_hass):
    """And the stranded rule itself is still refused, so attribution has not
    turned `unknown_rule_key` into a check that never fires.
    """
    entry = _rule_scaffold(subentry_entry())
    stranded_id = entry.add_subentry(
        RULE, {"mode": "zmazany", "zone": "terasa", "order": 0, "then": {"position": "keep"}}
    )
    flow = _make_flow(RuleSubentryFlowHandler, subentry_hass(entry), RULE, subentry_id=stranded_id)

    # Re-submitting it unchanged, as a user would after reopening the form
    # without fixing the mode. The mode select offers only `bezny`, so this
    # shape is only reachable by leaving the stale value in place -- which is
    # exactly what a reconfigure form prefilled from broken data does.
    result = asyncio.run(
        flow.async_step_reconfigure(
            {"mode": "zmazany", "zone": "terasa", "order": 0, "position": "keep", "tilt": "keep"}
        )
    )

    assert result["type"] is FlowResultType.FORM
    assert "unknown_rule_key" in result["description_placeholders"]["error_detail"]


def test_a_rules_own_dangling_condition_ref_blocks_only_that_rule(subentry_entry, subentry_hass):
    """The `_condition_sites` rule-id assumption, exercised end to end.

    `validation._condition_sites` attributes a condition-body problem inside
    a rule's `if` to `("rule", "<mode>.<zone>#<index>")`. Nothing before this
    task ever compared that string against a real subentry, so it was a
    guess. Here `r1`'s dangling ref must block `r1` and nothing else: if the
    identity did not line up, the first assertion would fail (the problem
    would match nobody) or the second would (it would match everybody).
    """
    entry = _rule_scaffold(subentry_entry())
    innocent_id = entry.add_subentry(
        RULE, {"mode": "bezny", "zone": "terasa", "order": 0, "then": {"position": "keep"}}
    )
    guilty_id = entry.add_subentry(
        RULE,
        {
            "mode": "bezny",
            "zone": "terasa",
            "order": 10,
            "if": {"condition": "ref", "name": "neexistuje"},
            "then": {"position": 0},
        },
    )
    hass = subentry_hass(entry)

    # The guilty rule's own form is blocked, and the message names it by the
    # very index `rule_owner_ids` assigns it.
    assert rule_owner_ids(entry)[guilty_id] == "bezny.terasa#1"
    guilty = asyncio.run(
        _make_flow(
            RuleSubentryFlowHandler, hass, RULE, subentry_id=guilty_id
        ).async_step_reconfigure(
            {
                "mode": "bezny",
                "zone": "terasa",
                "order": 10,
                "if": [{"condition": "ref", "name": "neexistuje"}],
                "position": "0",
                "tilt": "keep",
            }
        )
    )
    assert guilty["type"] is FlowResultType.FORM
    assert (
        "rule bezny.terasa#1 refers to unknown condition 'neexistuje'"
        in guilty["description_placeholders"]["error_detail"]
    )

    # The innocent neighbour, in the same pair and the same type, is not.
    innocent = asyncio.run(
        _make_flow(
            RuleSubentryFlowHandler, hass, RULE, subentry_id=innocent_id
        ).async_step_reconfigure(
            {"mode": "bezny", "zone": "terasa", "order": 0, "position": "100", "tilt": "keep"}
        )
    )
    assert innocent["type"] is FlowResultType.ABORT

    # Neither is an unrelated blind.
    blind = asyncio.run(
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
    assert blind["type"] is FlowResultType.CREATE_ENTRY


def test_deleting_a_value_a_rule_refs_does_not_lock_every_other_form(subentry_entry, subentry_hass):
    """A value ref is the one ref this project cannot defer: `_parse_axis`
    resolves it eagerly and raises `ConfigError` if it is missing, so an
    entry whose rule points at a deleted `value` does not merely fail
    `validate()` -- it fails to parse at all, before there is anything to
    attribute. Home Assistant has no veto on subentry removal, so this state
    is reachable by clicking delete on a value.

    Blocking every form on it would be the worst version of this project's
    recurring defect: not one form refusing a fix, but all of them. The
    entry is already refusing to load, so the way out has to stay open --
    both by editing the offending rule and by carrying on elsewhere.
    """
    entry = _rule_scaffold(subentry_entry(), values=["kvety_poz"])
    hass = subentry_hass(entry)
    rule = _add_rule(
        hass,
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 0, "position": "kvety_poz", "tilt": "keep"},
    )
    assert rule["type"] is FlowResultType.CREATE_ENTRY
    rule_id = next(sid for sid, sub in entry.subentries.items() if sub.subentry_type == RULE)

    # The user deletes the value, as HA's built-in subentry UI lets them.
    value_id = next(sid for sid, sub in entry.subentries.items() if sub.subentry_type == VALUE)
    del entry.subentries[value_id]

    # An unrelated blind add still works.
    blind = asyncio.run(
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
    assert blind["type"] is FlowResultType.CREATE_ENTRY

    # And the actual repair -- pointing the rule's axis somewhere real --
    # goes through, leaving an entry that parses again.
    repair = asyncio.run(
        _make_flow(RuleSubentryFlowHandler, hass, RULE, subentry_id=rule_id).async_step_reconfigure(
            {"mode": "bezny", "zone": "terasa", "order": 0, "position": "60", "tilt": "keep"}
        )
    )
    assert repair["type"] is FlowResultType.ABORT
    assert config_from_subentries(entry).rules["bezny.terasa"][0].then.position == 60


def test_a_save_that_itself_breaks_a_healthy_entry_is_still_blocked(subentry_entry, subentry_hass):
    """The exemption above must be exactly "the entry was already broken", not
    "parse failures never block". A healthy entry plus one bad save is the
    case the check exists for.
    """
    entry = _rule_scaffold(subentry_entry())

    result = _add_rule(
        subentry_hass(entry),
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 0, "position": "nic_take", "tilt": "keep"},
        save=False,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}


# ---------------------------------------------------------------------------
# The ordered build-up a human actually performs, now all the way to rules.
#
# Every earlier task's defect survived review because each test drove one step
# in isolation; the sequence is what catches an ordering trap. This one goes
# blinds -> zone -> value -> conditions -> modes -> several rules in one pair
# at different orders -> a rule using `keep` -> a rule whose position is a
# value ref, asserting each save succeeds, and finishes by loading the whole
# entry and checking the rules come out in `order` order.
# ---------------------------------------------------------------------------


def test_full_build_up_sequence_through_rules(subentry_entry, subentry_hass):
    entry = subentry_entry()
    hass = subentry_hass(entry)

    def blind(entity):
        result = asyncio.run(
            _make_flow(BlindSubentryFlowHandler, hass, BLIND).async_step_user(
                {
                    "entity": entity,
                    "tolerance": 45,
                    "travel_time": 60,
                    "has_tilt": True,
                    "tilt_after_arrival": True,
                }
            )
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY, entity
        entry.add_subentry(BLIND, result["data"], title=result["title"])

    # 1. Two blinds, then the zone that owns them -- the order task 2's fix
    # pass exists for.
    blind("cover.a")
    blind("cover.b")

    zone = asyncio.run(
        _make_flow(ZoneSubentryFlowHandler, hass, ZONE).async_step_user(
            {"id": "terasa", "members": ["cover.a", "cover.b"], "occupants": ["peter"]}
        )
    )
    assert zone["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(ZONE, zone["data"], title=zone["title"])

    # 2. A value, so a rule can point an axis at a helper rather than a literal.
    value = asyncio.run(
        _make_flow(ValueSubentryFlowHandler, hass, VALUE).async_step_user(
            {"id": "kvety_poz", "entity": "input_number.kvety_pozicia_zaluzie", "default": 34}
        )
    )
    assert value["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(VALUE, value["data"], title=value["title"])

    # 3. A named condition.
    cond = asyncio.run(
        _make_flow(ConditionSubentryFlowHandler, hass, CONDITION).async_step_user(
            {
                "id": "slnko",
                "condition": [
                    {"condition": "state", "entity_id": "sun.sun", "state": "above_horizon"}
                ],
            }
        )
    )
    assert cond["type"] is FlowResultType.CREATE_ENTRY
    entry.add_subentry(CONDITION, cond["data"], title=cond["title"])

    # 4. The fallback mode first, at the highest order, then the conditional
    # one below it -- the sequence `fallback_mode_not_last` forces.
    for submission in (
        {"id": "bezny", "order": 100},
        {"id": "slnecno", "order": 0, "condition_ref": "slnko"},
    ):
        mode = asyncio.run(
            _make_flow(ModeSubentryFlowHandler, hass, MODE).async_step_user(submission)
        )
        assert mode["type"] is FlowResultType.CREATE_ENTRY, submission["id"]
        entry.add_subentry(MODE, mode["data"], title=mode["title"])

    # 5. Three rules in the same pair, added in the order a person would think
    # of them -- and each one taking the `order` the form suggested, never a
    # number worked out by hand.
    for submission in (
        {"if_ref": "slnko", "position": "kvety_poz", "tilt": "0", "name": "tienenie"},
        {"position": "keep", "tilt": "keep", "events": ["arrival"], "name": "prichod"},
        {"position": "100", "tilt": "100", "name": "inak"},
    ):
        flow, shown = _start_rule(hass, entry, "slnecno", "terasa")
        suggested_order = _suggested(shown)["order"]
        result = asyncio.run(
            flow.async_step_rule(
                {"mode": "slnecno", "zone": "terasa", "order": suggested_order, **submission}
            )
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY, submission["name"]
        entry.add_subentry(RULE, result["data"], title=result["title"])

    assert [
        sub.data["order"] for sub in entry.subentries.values() if sub.subentry_type == RULE
    ] == [0, 10, 20]

    # 6. A catch-all for the other mode, so no (mode, zone) pair is empty.
    catch_all = _add_rule(
        hass,
        entry,
        {"mode": "bezny", "zone": "terasa", "order": 0, "position": "keep", "tilt": "keep"},
    )
    assert catch_all["type"] is FlowResultType.CREATE_ENTRY

    # 7. A rule slipped *between* two existing ones -- the whole reason the
    # suggested order steps by ten rather than by one.
    between = _add_rule(
        hass,
        entry,
        {
            "mode": "slnecno",
            "zone": "terasa",
            "order": 5,
            "position": "0",
            "tilt": "keep",
            "name": "medzi",
        },
    )
    assert between["type"] is FlowResultType.CREATE_ENTRY

    # The finished entry loads, is free of ERRORs, and the rules come out in
    # `order` order -- including the one inserted between two neighbours.
    built = config_from_subentries(entry)
    assert [rule.name for rule in built.rules["slnecno.terasa"]] == [
        "tienenie",
        "medzi",
        "prichod",
        "inak",
    ]
    assert built.rules["slnecno.terasa"][0].then.position == Ref(
        entity="input_number.kvety_pozicia_zaluzie", default=34
    )
    assert built.rules["slnecno.terasa"][2].then.position is KEEP
    assert built.rules["slnecno.terasa"][2].events == frozenset({"arrival"})
    problems = validate(built) + duplicate_rule_order_problems(entry)
    assert [p for p in problems if p.severity == ERROR] == []


# ---------------------------------------------------------------------------
# Translations, checked against the flows rather than by reading the diff.
#
# `tests/test_translations.py` proves the three JSON files agree with each
# other. What it cannot know is what the *flows* actually render: a step or a
# field with no key at all is consistent across all three files and still
# shows the user a raw identifier. These two close that half.
# ---------------------------------------------------------------------------

_STRINGS = json.loads(
    (Path(cover_logic.__file__).parent / "strings.json").read_text(encoding="utf-8")
)


def _step_names(handler):
    """Every step id `handler` can dispatch, the way Home Assistant finds them."""
    return {
        name.removeprefix("async_step_") for name in dir(handler) if name.startswith("async_step_")
    }


@pytest.mark.parametrize("subentry_type", sorted(SUBENTRY_FLOW_HANDLERS))
def test_every_subentry_type_has_its_own_strings(subentry_type):
    section = _STRINGS["config_subentries"][subentry_type]

    assert section["entry_type"]
    assert section["initiate_flow"]["user"]
    # Every flow surfaces failures the same way (`errors["base"]`) and ends a
    # reconfigure with the same abort reason, so both keys are needed by all.
    assert section["error"]["invalid_config"]
    assert section["abort"]["reconfigure_successful"]


@pytest.mark.parametrize("subentry_type", sorted(SUBENTRY_FLOW_HANDLERS))
def test_every_step_a_flow_can_dispatch_has_a_title(subentry_type):
    """A step id with no strings entry renders with no title and no
    description -- the failure `rule`'s two-step add made newly possible,
    since every other flow has only ever shown `user`/`reconfigure`.
    """
    handler = SUBENTRY_FLOW_HANDLERS[subentry_type]
    steps = _STRINGS["config_subentries"][subentry_type]["step"]

    for step_name in _step_names(handler):
        assert step_name in steps, f"{subentry_type}: step {step_name!r} has no strings"
        assert steps[step_name]["title"]


def test_every_field_of_every_rendered_form_has_a_label(subentry_entry, subentry_hass):
    """Renders each add form for real and checks each field it declares, rather
    than trusting a hand-kept list of field names to have stayed in step with
    the schemas.
    """
    entry = _rule_scaffold(subentry_entry(), values=["kvety_poz"], conditions=["slnko"])
    hass = subentry_hass(entry)

    rendered = []
    for subentry_type, handler in sorted(SUBENTRY_FLOW_HANDLERS.items()):
        rendered.append(
            (
                subentry_type,
                asyncio.run(_make_flow(handler, hass, subentry_type).async_step_user(None)),
            )
        )
    # `rule`'s second step is only reachable after step one, so it is rendered
    # separately -- and it is the step with the most fields of any in the
    # integration, so leaving it out would gut this test.
    rendered.append((RULE, _start_rule(hass, entry, "bezny", "terasa")[1]))

    # `.get` rather than `[...]`: a step missing entirely is a real failure
    # mode too, and it should be reported as one more missing label rather
    # than as a `KeyError` traceback that buries which step it was.
    missing = [
        f"{subentry_type}::{shown['step_id']}::{field}"
        for subentry_type, shown in rendered
        for field in _schema_keys(shown)
        if field
        not in _STRINGS["config_subentries"][subentry_type]["step"]
        .get(shown["step_id"], {})
        .get("data", {})
    ]
    assert missing == []
