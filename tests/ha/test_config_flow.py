"""Tests for `config_flow.CoverLogicConfigFlow`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv -- see `test_ha_world.py`'s own note.

Drives the flow's own step method directly rather than through
`hass.config_entries.flow.async_init()`: a working flow manager needs a real,
running `HomeAssistant` wired up with `config_entries` storage, the loader,
and the flow manager itself (which needs the integration loadable from a
`custom_components/` directory on disk) -- disproportionate for exercising
one step's logic. `tests/ha/conftest.py`'s `FakeConfigEntries` stubs exactly
the two calls the base `ConfigFlow` methods our step uses
(`async_set_unique_id`, `_abort_if_unique_id_configured`) touch on
`self.hass`, not the whole manager -- the same tradeoff `FakeConfigEntry`
makes in `test_init.py` and `FakeHass` makes in `test_ha_world.py`.

One consequence: `_abort_if_unique_id_configured` raises `AbortFlow` directly
(that really is how `homeassistant.data_entry_flow` signals it) rather than
returning the `FlowResultType.ABORT` dict a real flow manager's
`_async_handle_step` would translate that exception into -- this harness
does not reimplement the manager. The "second instance" test below asserts
the raised `AbortFlow` and its `.reason`, not a result dict.
"""

import asyncio

import pytest

pytest.importorskip("homeassistant")

from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from cover_logic.config_flow import CoverLogicConfigFlow
from cover_logic.const import CONF_CONFIG_PATH, DEFAULT_CONFIG_PATH, DOMAIN

# Zero problems of any severity -- see test_init.py for the same config text
# and why it is clean. Kept as a separate copy here rather than imported from
# test_init.py: each tests/ha/ module is meant to stand alone (see
# test_ha_world.py's own self-contained CONFIG_TEXT).
VALID_CONFIG = """
blinds:
  - entity: cover.a
zones:
  z:
    members: [cover.a]
modes:
  - {id: any}
rules:
  any.z:
    - {then: {position: keep, tilt: keep}}
"""

# cover.a belongs to no zone -- validate()'s `blind_without_zone`, ERROR severity.
ERROR_CONFIG = """
blinds:
  - entity: cover.a
zones:
  z:
    members: []
modes:
  - {id: any}
rules:
  any.z:
    - {then: {position: keep, tilt: keep}}
"""


def _make_flow(hass):
    flow = CoverLogicConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "test-flow-id"
    flow.context = {"source": "user"}
    return flow


def test_flow_shows_a_form_with_the_default_path(flow_hass):
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_user(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    schema_keys = {key.schema: key.default() for key in result["data_schema"].schema}
    assert schema_keys[CONF_CONFIG_PATH] == DEFAULT_CONFIG_PATH


def test_flow_rejects_a_missing_file_and_shows_an_error(flow_hass, tmp_path):
    flow = _make_flow(flow_hass())
    bad_path = str(tmp_path / "does_not_exist.yaml")

    result = asyncio.run(flow.async_step_user({CONF_CONFIG_PATH: bad_path}))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}


def test_flow_rejects_a_config_with_an_error_problem(flow_hass, tmp_path):
    flow = _make_flow(flow_hass())
    path = tmp_path / "cover_logic.yaml"
    path.write_text(ERROR_CONFIG, encoding="utf-8")

    result = asyncio.run(flow.async_step_user({CONF_CONFIG_PATH: str(path)}))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}


def test_flow_creates_entry_for_a_good_path(flow_hass, tmp_path):
    flow = _make_flow(flow_hass())
    path = tmp_path / "cover_logic.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    result = asyncio.run(flow.async_step_user({CONF_CONFIG_PATH: str(path)}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_CONFIG_PATH: str(path)}


def test_second_instance_is_aborted(flow_hass):
    flow = _make_flow(flow_hass(existing=True))

    with pytest.raises(AbortFlow) as excinfo:
        asyncio.run(flow.async_step_user(None))

    assert excinfo.value.reason == "already_configured"
