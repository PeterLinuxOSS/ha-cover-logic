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

from cover_logic.config_flow import (
    SUBENTRY_FLOW_HANDLERS,
    BlindSubentryFlowHandler,
    CoverLogicConfigFlow,
    ValueSubentryFlowHandler,
    ZoneSubentryFlowHandler,
)
from cover_logic.config_schema import _BLIND_KEYS, _VALUE_KEYS, _ZONE_KEYS, load_config
from cover_logic.config_store import _ID_KEY, BLIND, VALUE, ZONE, config_from_subentries

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
    shown = asyncio.run(flow.async_step_reconfigure(None))
    assert shown["type"] is FlowResultType.FORM
    assert shown["step_id"] == "reconfigure"

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
    `no_fallback_mode` -- both are transient artefacts of building a config
    one subentry type at a time, not a problem with this blind, and must not
    block the save. See `config_flow._TRANSIENT_ERROR_CODES`.
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
