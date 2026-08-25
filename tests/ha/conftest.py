"""Fixtures shared by the `tests/ha/` suite.

Everything here imports only `cover_logic` (pure) and `pytest` -- no
`homeassistant` import -- so it is harmless to collect under system Python
3.12, which has no `homeassistant` installed at all. Each *test module* under
`tests/ha/` guards its own Home Assistant imports with a module-level
`pytest.importorskip("homeassistant")`, which is what actually keeps the
system-Python run green; this file just must not defeat that by importing
`homeassistant` itself.
"""

import pytest

from cover_logic.config_schema import load_config

# A minimal config that exercises all three shapes `referenced_entities`
# produces: a plain state read (`input_boolean.a`), an attribute read
# (`cover.a`, `current_position`), and a `values:` helper entity
# (`input_number.kvety_pozicia_zaluzie`).
CONFIG_TEXT = """
blinds:
  - entity: cover.a
    facade_azimuth: 270
zones:
  terasa:
    members: [cover.a]
modes:
  - {id: bezny_den}
conditions:
  position_high:
    condition: state
    entity_id: cover.a
    attribute: current_position
    state: 100
  boolean_on:
    condition: state
    entity_id: input_boolean.a
    state: "on"
values:
  kvety_poz:
    entity: input_number.kvety_pozicia_zaluzie
    default: 34
rules:
  bezny_den.terasa:
    - {then: {position: keep, tilt: keep}}
"""


@pytest.fixture
def config():
    """A `Config` whose `referenced_entities()` covers state, attribute and value reads."""
    return load_config(CONFIG_TEXT)


class FakeStateMachine:
    """Stands in for `hass.states`: only the `.get()` read path `build_world` uses."""

    def __init__(self, states):
        """Store the entity_id -> State mapping this fake serves."""
        self._states = states

    def get(self, entity_id):
        """Return the `State` for `entity_id`, or `None` if it was never set."""
        return self._states.get(entity_id)


class FakeHass:
    """A minimal stand-in for `HomeAssistant`.

    `build_world` only ever reads `hass.states.get(entity_id)`. Constructing a
    real `HomeAssistant` needs a running event loop and on-disk config
    directory -- disproportionate for exercising one read path. This fake
    carries real `homeassistant.core.State` objects (constructed standalone,
    no event loop required), so attribute types and `.attributes` semantics
    are the genuine Home Assistant behaviour, not a reimplementation of it.
    """

    def __init__(self, states):
        """Wrap `states` (entity_id -> `State`) behind a `.states.get()` façade."""
        self.states = FakeStateMachine(states)


@pytest.fixture
def fake_hass():
    """Factory: `fake_hass({"cover.a": State(...)})` -> a `FakeHass`."""
    return FakeHass


class FakeConfigEntry:
    """Duck-typed stand-in for `homeassistant.config_entries.ConfigEntry`.

    `async_setup_entry`/`async_unload_entry` (see `test_init.py`) only ever
    read `entry.data` and read/write `entry.runtime_data`. Building a real
    `ConfigEntry` needs a live `HomeAssistant` with a working
    `config_entries` manager (on-disk storage, the event bus, the flow
    manager, ...) -- disproportionate for exercising two attribute reads and
    one attribute write, the same tradeoff `FakeHass` above makes for
    `build_world`. Real `ConfigEntry` has no `__slots__`, so `runtime_data`
    is a plain assignable attribute there too; this fake matches that shape
    by simply not setting it until `async_setup_entry` does.
    """

    def __init__(self, data):
        """Store `data`; `runtime_data` is left unset, matching a fresh entry."""
        self.data = data


@pytest.fixture
def make_entry():
    """Factory: `make_entry({"config_path": "..."})` -> a `FakeConfigEntry`."""
    return FakeConfigEntry


class FakeFlowManager:
    """Stands in for `hass.config_entries.flow`: only what base `ConfigFlow` methods touch."""

    def async_progress_by_handler(
        self, handler, *, include_uninitialized=False, match_context=None
    ):
        """No other flow is ever in progress in these tests."""
        return []


class FakeConfigEntries:
    """Stands in for `hass.config_entries`, as seen from inside a config flow step.

    `ConfigFlow.async_set_unique_id`/`_abort_if_unique_id_configured` are the
    only base-class methods `CoverLogicConfigFlow` calls (see
    `test_config_flow.py`'s module docstring for why a real `ConfigEntries`
    manager -- storage, the loader, the full flow manager -- is not built
    here instead). Between them they touch exactly
    `.flow.async_progress_by_handler(...)` and
    `.async_entry_for_domain_unique_id(...)`; this fake covers precisely
    that surface.
    """

    def __init__(self, existing_entry=None):
        """`existing_entry`, if given, is what `async_entry_for_domain_unique_id` returns."""
        self.flow = FakeFlowManager()
        self._existing_entry = existing_entry

    def async_entry_for_domain_unique_id(self, domain, unique_id):
        """Return the pre-seeded entry, standing in for 'a second instance already exists'."""
        return self._existing_entry


class FakeExistingEntry:
    """The one attribute `_abort_if_unique_id_configured` reads off a found entry."""

    source = "user"


class FakeFlowHass:
    """Stands in for `hass` as seen by a `ConfigFlow` step: only `.config_entries`."""

    def __init__(self, existing_entry=None):
        """Wrap a `FakeConfigEntries` seeded with `existing_entry` (or none)."""
        self.config_entries = FakeConfigEntries(existing_entry)


@pytest.fixture
def flow_hass():
    """Factory: `flow_hass()` for a fresh flow, `flow_hass(existing=True)` for a second instance."""

    def _make(*, existing=False):
        return FakeFlowHass(FakeExistingEntry() if existing else None)

    return _make


@pytest.fixture
def hass_factory(tmp_path):
    """Factory for a real, minimal `homeassistant.core.HomeAssistant`.

    `test_coordinator.py` needs `async_track_state_change_event` and
    `homeassistant.helpers.debounce.Debouncer` to genuinely dispatch events
    and schedule timers -- both reach into `hass.bus`, `hass.data` and
    `hass.loop` deeply enough (keyed listener bookkeeping, `HassJob`
    dispatch, `loop.call_later`) that a `FakeHass` covering just `.states`,
    the way `test_ha_world.py`'s does, would mean reimplementing that
    plumbing by hand -- exactly the risk of subtle incorrectness a fake is
    supposed to avoid, not invite. A real `HomeAssistant` sidesteps that
    without needing `pytest-homeassistant-custom-component` (pinned to
    `homeassistant==2025.1.4`, see the project's own constraint): its
    constructor does not start integrations, load config, or spin up
    storage -- only `hass.bus`, `hass.states`, `hass.data` and `hass.loop`
    exist, which is exactly the surface these two helpers need and no more.

    `HomeAssistant.__init__` calls `asyncio.get_running_loop()`, so it can
    only be constructed while a loop is already running -- this fixture
    therefore returns a plain callable rather than a ready-made instance,
    for a test to invoke from inside its own `asyncio.run(...)` body (the
    project's established async-test shape, e.g. `test_init.py`). The loop
    that ends up bound to `hass.loop` must be the same loop used for the
    rest of that test -- a `HomeAssistant` built in one `asyncio.run(...)`
    call and used from a different one fails opaquely (the Debouncer's
    `loop.call_later` binds the now-closed original loop) -- so a caller
    must construct, use and call `await hass.async_stop(force=True)` on the
    instance all inside one `asyncio.run(...)` body. `force=True` is needed
    because `hass.state` never leaves `CoreState.not_running` here (nothing
    calls `async_start`); without it, `async_stop` is a no-op and the
    constructor's background `import_executor` thread is never shut down.
    """

    def _make():
        # Deferred: see `tests/ha/conftest.py`'s own module docstring --
        # this file must stay importable under system Python 3.12, which has
        # no `homeassistant` installed, so the import lives inside the
        # factory function, only ever called from a module that has already
        # done `pytest.importorskip("homeassistant")`.
        from homeassistant.core import HomeAssistant  # noqa: PLC0415

        return HomeAssistant(str(tmp_path))

    return _make
