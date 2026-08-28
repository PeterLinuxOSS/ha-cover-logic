"""Tests for `config_flow.CoverLogicConfigFlow`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv -- see `test_ha_world.py`'s own note.

Drives the flow's own step methods directly rather than through
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

Since phase 5's setup menu, `async_step_user` only ever shows a menu (see
`config_flow.py`'s own module docstring); a test that wants to exercise one
of its four branches calls that branch's own `async_step_<name>` method
directly, exactly as `FlowManager._async_configure` itself dispatches a menu
choice (see that function's own source, quoted in this task's report) --
never a `next_step_id` passed back into `async_step_user`, which is not how
a real menu selection is delivered.
"""

import asyncio
from pathlib import Path
import threading
from unittest import mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from cover_logic.config_flow import CoverLogicConfigFlow
from cover_logic.config_store import BLIND
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


# ---------------------------------------------------------------------------
# The `user` step: the setup menu, and the single-instance guard in front of it.
# ---------------------------------------------------------------------------


def test_user_step_shows_the_setup_menu(flow_hass):
    """The whole point of this task: a brand-new install sees four ways to
    start, not a blind text field.
    """
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_user(None))

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["blinds_now", "from_file", "from_example", "empty"]


def test_second_instance_is_aborted(flow_hass):
    """Checked before the menu is even shown -- every branch below ends in
    `async_create_entry`, so this is the one place single-instance is enforced.
    """
    flow = _make_flow(flow_hass(existing=True))

    with pytest.raises(AbortFlow) as excinfo:
        asyncio.run(flow.async_step_user(None))

    assert excinfo.value.reason == "already_configured"


# ---------------------------------------------------------------------------
# "Set up blinds now": a `blind` subentry per `cover` entity picked.
# ---------------------------------------------------------------------------


def test_blinds_now_shows_a_form_with_an_empty_default(flow_hass):
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_blinds_now(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "blinds_now"
    schema_keys = {key.schema: key.default() for key in result["data_schema"].schema}
    assert schema_keys["entities"] == []


def test_blinds_now_rejects_an_empty_selection(flow_hass):
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_blinds_now({"entities": []}))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_blinds_selected"}


def test_blinds_now_creates_one_blind_subentry_per_entity(flow_hass):
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a", "cover.b"]}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    assert list(result["subentries"]) == [
        {
            "data": {"entity": "cover.a"},
            "subentry_type": BLIND,
            "title": "cover.a",
            "unique_id": None,
        },
        {
            "data": {"entity": "cover.b"},
            "subentry_type": BLIND,
            "title": "cover.b",
            "unique_id": None,
        },
    ]


# ---------------------------------------------------------------------------
# "Load a configuration from a YAML file": today's original `user` step,
# renamed to `from_file`, behaviour unchanged.
# ---------------------------------------------------------------------------


def test_from_file_shows_a_form_with_the_default_path(flow_hass):
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_from_file(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "from_file"
    schema_keys = {key.schema: key.default() for key in result["data_schema"].schema}
    assert schema_keys[CONF_CONFIG_PATH] == DEFAULT_CONFIG_PATH


def test_from_file_rejects_a_missing_file_and_shows_an_error(flow_hass, tmp_path):
    flow = _make_flow(flow_hass())
    bad_path = str(tmp_path / "does_not_exist.yaml")

    result = asyncio.run(flow.async_step_from_file({CONF_CONFIG_PATH: bad_path}))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}


def test_from_file_rejects_a_config_with_an_error_problem(flow_hass, tmp_path):
    flow = _make_flow(flow_hass())
    path = tmp_path / "cover_logic.yaml"
    path.write_text(ERROR_CONFIG, encoding="utf-8")

    result = asyncio.run(flow.async_step_from_file({CONF_CONFIG_PATH: str(path)}))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}


def test_from_file_creates_entry_for_a_good_path(flow_hass, tmp_path):
    flow = _make_flow(flow_hass())
    path = tmp_path / "cover_logic.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    result = asyncio.run(flow.async_step_from_file({CONF_CONFIG_PATH: str(path)}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_CONFIG_PATH: str(path)}


def test_from_file_reads_the_config_file_off_the_event_loop(flow_hass, tmp_path):
    """`_describe_problems` does a blocking `Path.read_text` via `load_config_file`
    -- calling it directly from the awaited step blocks Home Assistant's
    event loop every time someone adds or reconfigures this integration
    through the UI. It must run inside `hass.async_add_executor_job`, on a
    thread other than the caller's.
    """
    flow = _make_flow(flow_hass())
    path = tmp_path / "cover_logic.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    read_thread_idents = []
    original_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):
        read_thread_idents.append(threading.get_ident())
        return original_read_text(self, *args, **kwargs)

    async def run():
        with mock.patch.object(Path, "read_text", spy_read_text):
            await flow.async_step_from_file({CONF_CONFIG_PATH: str(path)})

    caller_thread_ident = threading.get_ident()
    asyncio.run(run())

    assert read_thread_idents, "_describe_problems never read the file at all"
    assert read_thread_idents[0] != caller_thread_ident


# ---------------------------------------------------------------------------
# "Start from the example configuration": imports `docs/example-config.yaml`.
# ---------------------------------------------------------------------------


def test_from_example_shows_a_confirmation_form(flow_hass):
    """This checkout genuinely ships `docs/example-config.yaml` (see
    `conformance.repo_example_config_path`'s docstring) -- proof the happy
    path renders a plain confirmation, not the "not available" branch.
    """
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_from_example(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "from_example"


def test_from_example_creates_the_example_house_subentries(flow_hass):
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_from_example({}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    blind_entities = {
        entry["data"]["entity"] for entry in result["subentries"] if entry["subentry_type"] == BLIND
    }
    assert blind_entities == {
        "cover.living_room_balcony",
        "cover.bedroom_window",
        "cover.office_window",
        "cover.kitchen_window",
    }


def test_from_example_falls_back_to_the_menu_when_not_shipped(flow_hass):
    """A HACS install or a bare copy of `custom_components/cover_logic` has no
    sibling `docs/` directory -- `repo_example_config_path` returns `None`
    there (see its own docstring). This step must explain that and return to
    the main menu on the next submit, not abort the whole flow.
    """
    flow = _make_flow(flow_hass())

    with mock.patch("cover_logic.config_flow.repo_example_config_path", return_value=None):
        shown = asyncio.run(flow.async_step_from_example(None))
        assert shown["type"] is FlowResultType.FORM
        assert shown["step_id"] == "example_not_available"

        back = asyncio.run(flow.async_step_from_example({}))

    assert back["type"] is FlowResultType.MENU
    assert back["step_id"] == "user"


# ---------------------------------------------------------------------------
# "Start empty": a config entry with no subentries at all.
# ---------------------------------------------------------------------------


def test_empty_shows_a_confirmation_form(flow_hass):
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_empty(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "empty"


def test_empty_creates_an_entry_with_no_subentries(flow_hass):
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_empty({}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    assert list(result["subentries"]) == []
