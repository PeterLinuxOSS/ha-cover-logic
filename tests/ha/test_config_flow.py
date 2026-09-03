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
from cover_logic.config_schema import ConfigError
from cover_logic.config_store import (
    BLIND,
    GUARD,
    MODE,
    RULE,
    ZONE,
    config_from_subentries,
    entry_from_subentry_items,
)
from cover_logic.const import CONF_CONFIG_PATH, DEFAULT_CONFIG_PATH, DOMAIN
from cover_logic.model import Blind, Config
from cover_logic.starter_config import _OPEN_POSITION, _SHADE_POSITION, _SHADE_TILT
from cover_logic.validation import ERROR, validate

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

# `cover.a` is claimed by two zones -- `validate()`'s `blind_in_two_zones`,
# ERROR severity. It was `blind_without_zone` until 2026-09-03, when that
# became a WARNING because refusing the whole entry over one incomplete blind
# stopped the house deciding about all the others (docs/rationale.md, "Why an
# orphan blind is skipped rather than fatal"). Two owners stayed fatal -- whose
# rules apply is unanswerable -- so it is the shape this fixture needs now.
ERROR_CONFIG = """
blinds:
  - entity: cover.a
zones:
  z:
    members: [cover.a]
  z2:
    members: [cover.a]
modes:
  - {id: any}
rules:
  any.z:
    - {then: {position: keep, tilt: keep}}
  any.z2:
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


def test_blinds_now_moves_on_to_the_facing_question(flow_hass):
    """Picking entities no longer creates the entry directly (see
    `config_flow.py`'s own module docstring for why "blinds with nothing
    deciding them" was the bug this task closes) -- it asks about the first
    blind's facing instead.
    """
    flow = _make_flow(flow_hass())

    result = asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a", "cover.b"]}))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "blinds_now_facing"
    assert result["description_placeholders"] == {"blind": "cover.a"}


def test_blinds_now_facing_asks_once_per_blind_in_order(flow_hass):
    """One screen per blind (see `async_step_blinds_now_facing`'s own
    docstring for why not one field per blind on a single screen): answering
    for the first blind shows the second, not a repeat or a skip.
    """
    flow = _make_flow(flow_hass())
    asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a", "cover.b"]}))

    second = asyncio.run(flow.async_step_blinds_now_facing({"facing": "north"}))

    assert second["type"] is FlowResultType.FORM
    assert second["step_id"] == "blinds_now_facing"
    assert second["description_placeholders"] == {"blind": "cover.b"}


def test_blinds_now_facing_options_are_translatable(flow_hass):
    """The compass dropdown is the one closed, fixed-option `SelectSelector`
    in this flow (see `_FACING_SCHEMA`'s own comment): every other
    `SelectSelector` here lists entity ids or a user-chosen id, which cannot
    be translated, so this is the one spot a missing `translation_key` would
    leave a Slovak user reading raw English-ish internal ids ("north",
    "southeast", ...) under a translated title. Home Assistant resolves each
    option's label from `strings.json`/`translations/*.json`'s
    `selector.facing.options` only when this is set.
    """
    flow = _make_flow(flow_hass())
    asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a"]}))

    result = asyncio.run(flow.async_step_blinds_now_facing(None))

    (validator,) = [
        value for key, value in result["data_schema"].schema.items() if key.schema == "facing"
    ]
    assert validator.config.get("translation_key") == "facing"


def test_blinds_now_facing_moves_to_the_summary_once_every_blind_is_answered(flow_hass):
    flow = _make_flow(flow_hass())
    asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a", "cover.b"]}))
    asyncio.run(flow.async_step_blinds_now_facing({"facing": "north"}))

    result = asyncio.run(flow.async_step_blinds_now_facing({"facing": "east"}))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "blinds_now_summary"
    placeholders = result["description_placeholders"]
    blinds = placeholders["blinds"]
    assert "- cover.a" in blinds
    assert "- cover.b" in blinds
    # No raw internal compass id repeated here -- this plain, synchronous
    # helper has no `hass`/language to resolve `_FACING_TRANSLATION_KEY`'s
    # localized label with (see `_describe_starter_config`'s own docstring),
    # so it does not print the untranslated one either. The facing question
    # was already asked, and answered, one translated screen ago.
    assert "facing" not in blinds.lower()
    # Plain language: getting through this flow must never require these
    # three words (see the phase 6 task 4 plan's own "concepts are not
    # shown until needed").
    for forbidden in ("condition", "mode", "rule"):
        assert forbidden not in blinds.lower()
    # The prose describing what the starter configuration actually does
    # lives in `strings.json`/`translations/*.json` now (see `config_flow.
    # _describe_starter_config`'s own docstring for why), filled in from
    # these three placeholders rather than baked into a hard-coded English
    # summary string.
    assert placeholders["shade_position"] == str(_SHADE_POSITION)
    assert placeholders["shade_tilt"] == str(_SHADE_TILT)
    assert placeholders["open_position"] == str(_OPEN_POSITION)


def test_blinds_now_summary_creates_a_configuration_that_decides_something(flow_hass):
    """The whole point of this task: what `blinds_now` creates must actually
    decide every blind, not merely exist -- checked here by parsing the
    exact subentries this step hands to `async_create_entry` back through
    `config_from_subentries` and running the real `validate()` over them,
    not just over `_build_starter_config`'s own in-memory `Config` (which
    `subentries_from_config`'s own round-trip self-check already covers on
    every call, so a second identical check here would prove nothing new).
    """
    flow = _make_flow(flow_hass())
    asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a", "cover.b"]}))
    asyncio.run(flow.async_step_blinds_now_facing({"facing": "north"}))
    asyncio.run(flow.async_step_blinds_now_facing({"facing": "east"}))

    result = asyncio.run(flow.async_step_blinds_now_summary({}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry_types = [s["subentry_type"] for s in result["subentries"]]
    assert subentry_types.count(BLIND) == 2
    assert subentry_types.count(ZONE) == 1
    assert subentry_types.count(MODE) == 2
    assert subentry_types.count(RULE) == 3

    entry = entry_from_subentry_items(
        [(s["subentry_type"], s["data"]) for s in result["subentries"]]
    )
    config = config_from_subentries(entry)
    assert [p for p in validate(config) if p.severity == ERROR] == []


def test_blinds_now_summary_shows_the_form_first_without_creating_anything(flow_hass):
    """`user_input is None` (the initial render) must only describe what would
    be created, never create it -- otherwise the confirmation screen this
    task adds ("show the user what was created ... before or immediately
    after it is saved") would not be a confirmation at all.
    """
    flow = _make_flow(flow_hass())
    asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a"]}))
    asyncio.run(flow.async_step_blinds_now_facing({"facing": "south"}))

    result = asyncio.run(flow.async_step_blinds_now_summary(None))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "blinds_now_summary"


def test_blinds_now_summary_refuses_to_create_an_invalid_generated_configuration(
    flow_hass, monkeypatch
):
    """Load-bearing proof that `validate()` actually gates this step, not just
    decorates it: force `_build_starter_config` to hand back a config
    `validate()` rejects (a blind in no zone) and confirm the flow aborts
    instead of silently creating a broken entry. This can only happen if
    this module's own generator has a bug -- see `async_step_blinds_now_
    summary`'s own docstring -- so a real user's answers never reach this
    branch; the monkeypatch is what stands in for that bug here.
    """
    flow = _make_flow(flow_hass())
    asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a"]}))
    asyncio.run(flow.async_step_blinds_now_facing({"facing": "north"}))
    broken = Config(blinds={"cover.a": Blind(entity="cover.a")}, zones={}, modes=(), rules={})
    monkeypatch.setattr("cover_logic.config_flow._build_starter_config", lambda *a, **k: broken)

    result = asyncio.run(flow.async_step_blinds_now_summary(None))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "starter_config_invalid"


def test_blinds_now_summary_aborts_instead_of_leaking_a_config_error_on_submit(
    flow_hass, monkeypatch
):
    """The same "this module's own bug" outcome, one step later: force
    `subentries_from_config` (already `validate()`-clean `config` in hand) to
    raise `ConfigError`, the same way its own round-trip self-check
    (`config_store.py`) would if it and `config_from_subentries` ever
    disagreed with each other -- and confirm the submit branch aborts with
    `starter_config_invalid` instead of letting that exception escape the
    flow as Home Assistant's generic "Unknown error occurred". Before this
    guard existed, this is exactly what happened: nothing in
    `async_step_blinds_now_summary`'s submit branch caught it.
    """
    flow = _make_flow(flow_hass())
    asyncio.run(flow.async_step_blinds_now({"entities": ["cover.a"]}))
    asyncio.run(flow.async_step_blinds_now_facing({"facing": "north"}))

    def _raise(config):
        msg = "subentries_from_config produced subentries that do not round-trip"
        raise ConfigError(msg)

    monkeypatch.setattr("cover_logic.config_flow.subentries_from_config", _raise)

    result = asyncio.run(flow.async_step_blinds_now_summary({}))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "starter_config_invalid"


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
    # `guards` is carried through in `entry.data`, not as a subentry -- see
    # Since config entry version 3 guards are subentries, so `entry.data`
    # carries nothing at all -- and the example config's four guards have to
    # show up in the subentry list instead, in order. A frozen `Guard`
    # dataclass would not survive `.storage`, so each one must be a plain
    # mapping by the time it gets here.
    assert set(result["data"]) == set()
    guards = [entry["data"] for entry in result["subentries"] if entry["subentry_type"] == GUARD]
    assert all(isinstance(guard, dict) for guard in guards)
    assert [guard["policy"] for guard in guards] == ["force", "skip", "defer", "skip"]
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

    The resubmission goes to `async_step_example_not_available`, not back to
    `async_step_from_example` -- exactly what Home Assistant's own flow
    manager does with a form's `step_id` (`_async_handle_step(flow,
    cur_step["step_id"], user_input)`), and the reason `shown["step_id"]`
    must name a method that actually exists: a form whose `step_id` has no
    matching `async_step_<id>` fails the manager's own `_raise_if_step_
    does_not_exist` the moment it is rendered for real, something driving
    methods directly (as every test in this module does) cannot catch on its
    own -- asserted below via `hasattr`.
    """
    flow = _make_flow(flow_hass())

    with mock.patch("cover_logic.config_flow.repo_example_config_path", return_value=None):
        shown = asyncio.run(flow.async_step_from_example(None))
        assert shown["type"] is FlowResultType.FORM
        assert shown["step_id"] == "example_not_available"
        assert hasattr(flow, f"async_step_{shown['step_id']}")

        back = asyncio.run(flow.async_step_example_not_available({}))

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
