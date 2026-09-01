"""Fixtures shared by the `tests/ha/` suite.

Everything here imports only `cover_logic` (pure) and `pytest` -- no
`homeassistant` import -- so it is harmless to collect under system Python
3.12, which has no `homeassistant` installed at all. Each *test module* under
`tests/ha/` guards its own Home Assistant imports with a module-level
`pytest.importorskip("homeassistant")`, which is what actually keeps the
system-Python run green; this file just must not defeat that by importing
`homeassistant` itself.
"""

import asyncio

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


# The window a test waits out when what it is testing is not the window. The
# real `EVAL_SETTLE_SECONDS` is 8 s (it must outlast the house's longest
# trigger `for:`, see `const.py`), and a module that waits it out a dozen times
# spends two minutes proving something unrelated to its length.
SHORT_SETTLE_SECONDS = 0.5


@pytest.fixture
def short_settle_window(monkeypatch):
    """Shrink the coordinator's settle window; opted into per module, never global.

    Deliberately not autouse: `tests/ha/test_settle.py` reproduces two measured
    mornings against the *real* constant, and a window shrunk from a shared
    conftest would delete that evidence without anyone editing that file. The
    string target imports `cover_logic.coordinator` at call time, which keeps
    this module free of a `homeassistant` import under system Python.
    """
    monkeypatch.setattr("cover_logic.coordinator.EVAL_SETTLE_SECONDS", SHORT_SETTLE_SECONDS)
    return SHORT_SETTLE_SECONDS


class FakeRuntimeEntry:
    """The one attribute the *runtime* side of a config entry is read for.

    `CoverLogicCoordinator` and `CoverRunner` read `entry.options` and nothing
    else off the entry -- the `dry_run` switch lives there so it can be flipped
    without a reload (`const.OPT_DRY_RUN`). A plain dict rather than the real
    `MappingProxyType` so a test can flip it mid-run the way
    `async_update_entry` does in production, which is the whole point of the
    option living in `options` at all.

    Separate from `FakeConfigEntry` above because that one stands in for the
    *setup* side (`data`, `subentries`, `version`, `runtime_data`); nothing
    that only needs the switch should have to construct a config file path to
    get it.
    """

    def __init__(self, options=None, entry_id="entry1"):
        """Store `options` (defaulting to none set) and an entry id."""
        self.options = dict(options or {})
        self.entry_id = entry_id


@pytest.fixture
def runtime_entry():
    """Factory: `runtime_entry({"dry_run": False})` -> a `FakeRuntimeEntry`."""
    return FakeRuntimeEntry


class FakeStateMachine:
    """Stands in for `hass.states`: only the `.get()` read path `build_world` uses."""

    def __init__(self, states):
        """Store the entity_id -> State mapping this fake serves."""
        self._states = states

    def get(self, entity_id):
        """Return the `State` for `entity_id`, or `None` if it was never set."""
        return self._states.get(entity_id)


class FakeLocation:
    """The three fields `homeassistant.helpers.sun` reads off `hass.config`.

    Bratislava, so a sun time computed in a test is a plausible one for this
    house rather than Home Assistant's own default of San Diego.
    """

    latitude = 48.1486
    longitude = 17.1077
    elevation = 150
    time_zone = "Europe/Bratislava"


class FakeHass:
    """A minimal stand-in for `HomeAssistant`.

    `build_world` reads `hass.states.get(entity_id)` and -- since sun times
    became part of the snapshot -- `hass.config`'s latitude/longitude/
    elevation, by way of `get_astral_event_date`. Constructing a real
    `HomeAssistant` needs a running event loop and on-disk config directory --
    disproportionate for exercising one read path. This fake carries real
    `homeassistant.core.State` objects (constructed standalone, no event loop
    required), so attribute types and `.attributes` semantics are the genuine
    Home Assistant behaviour, not a reimplementation of it.
    """

    def __init__(self, states):
        """Wrap `states` (entity_id -> `State`) behind a `.states.get()` façade."""
        self.states = FakeStateMachine(states)
        self.config = FakeLocation()


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

    `subentries` defaults to `{}` (empty) -- harmless for every pre-existing
    test here, since they all pass `CONF_CONFIG_PATH` in `data`, and it is
    the presence of that key, not `entry.subentries` truthiness, that sends
    `async_setup_entry` down the legacy, path-based branch -- see that
    function's own docstring for why (phase 5 changed this: a subentry-backed
    entry can legitimately have zero subentries too, e.g. straight after
    "start empty"). `version` defaults to `1`, the original shape's version,
    matching every entry these fixtures built before `async_migrate_entry`
    existed.

    `options` defaults to `{}` -- an entry that has never had its options
    touched, which is what every entry created before the `dry_run` switch
    existed looks like. `CoverRunner`/`CoverLogicCoordinator` read the switch
    through `entry.options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN)`, so an empty
    mapping is the "dry run on" default and not a missing attribute.
    """

    def __init__(self, data, *, subentries=None, version=1, options=None):
        """Store `data`/`subentries`/`version`/`options`; `runtime_data` is left unset."""
        self.data = data
        self.subentries = subentries or {}
        self.version = version
        self.options = dict(options or {})


@pytest.fixture
def make_entry():
    """Factory: `make_entry({"config_path": "..."})` -> a `FakeConfigEntry`."""
    return FakeConfigEntry


class FakeEntryConfigEntries:
    """Stands in for `hass.config_entries` as seen from `async_setup_entry`/`async_unload_entry`
    and, since this task, `async_migrate_entry`.

    Once `sensor.py` exists, `async_setup_entry`/`async_unload_entry` touch
    exactly `.async_forward_entry_setups(entry, platforms)` and
    `.async_unload_platforms(entry, platforms)` -- this fake covers precisely
    that surface, records every call so a test can assert what got forwarded
    or unloaded, and returns `unload_result` (default `True`) the way a real
    unload does. `.async_add_subentry`/`.async_update_entry` mirror
    `FakeServiceConfigEntries`'s identically-named methods below, for
    `async_migrate_entry`'s own writes -- kept as a separate class rather
    than reused, since this one, unlike that one, is not also handed a
    fixed list of pre-wrapped entries to serve back from `.async_entries`.
    """

    def __init__(self, *, unload_result=True):
        """`unload_result` lets a test simulate a platform refusing to unload."""
        self.forwarded = []
        self.unloaded = []
        self.unload_result = unload_result

    async def async_forward_entry_setups(self, entry, platforms):
        """Record the call; a real platform loader would import and set up each platform."""
        self.forwarded.append((entry, list(platforms)))

    def async_add_subentry(self, entry, subentry):
        """Insert `subentry` keyed by its own `subentry_id`, like the real manager does."""
        entry.subentries[subentry.subentry_id] = subentry
        return True

    def async_update_entry(self, entry, *, data=None, version=None, **_ignored):
        """Replace `entry.data`/`entry.version` in place, like the real manager does."""
        if data is not None:
            entry.data = dict(data)
        if version is not None:
            entry.version = version
        return True

    async def async_unload_platforms(self, entry, platforms):
        """Record the call and return `unload_result`."""
        self.unloaded.append((entry, list(platforms)))
        return self.unload_result


class FakeServiceRegistry:
    """Stands in for `hass.services`, as seen from `services.async_register_services`/
    `async_unregister_services` and by `test_services.py` driving a registered handler
    directly.

    Covers exactly `.has_service`, `.async_register` and `.async_remove` --
    the only three calls either function makes. Registered handlers are kept
    in a plain dict rather than dispatched through anything resembling real
    HA service-call machinery (schema coercion, `SupportsResponse`
    enforcement, ...), the same tradeoff every other fake in this file makes:
    `tests/ha/test_services.py` calls `services._async_import_config`/
    `_async_export_config` directly for its actual assertions and uses this
    registry only to prove *that* registration happened and is idempotent.
    """

    def __init__(self):
        """Start with nothing registered."""
        self._services: dict[tuple[str, str], object] = {}

    def has_service(self, domain, service):
        """Whether `(domain, service)` was registered and not yet removed."""
        return (domain, service) in self._services

    def async_register(self, domain, service, handler, **_kwargs):
        """Record the handler, ignoring `schema`/`supports_response` (not exercised here)."""
        self._services[(domain, service)] = handler

    def async_remove(self, domain, service):
        """Drop `(domain, service)` if present; a real registry ignores a missing one too."""
        self._services.pop((domain, service), None)

    def registered_services(self):
        """Every `(domain, service)` currently registered -- a public read, not `._services`."""
        return frozenset(self._services)


class FakeSetupHass:
    """Stands in for `hass` as seen by `async_setup_entry`/`async_unload_entry`.

    `.config_entries` and (since both functions now also (un)register the
    `import_config`/`export_config` services) `.services` are the two
    attributes those two functions touch directly.

    `.states` is here for the coordinator they build. `test_init.py`'s
    fixtures (`VALID_CONFIG` and friends) reference no entity at all, so
    `build_world` still never reads anything -- but since task 4 the
    coordinator also builds a `positions` map for `guards.review`, one entry
    per *configured blind*, and those configs do have a blind. An empty state
    machine answers `None` for it, which is exactly "this cover reports
    nothing" and is a state the real house can be in too.
    """

    def __init__(self, *, unload_result=True):
        """Wrap a fresh `FakeEntryConfigEntries`, `FakeServiceRegistry` and empty states."""
        self.config_entries = FakeEntryConfigEntries(unload_result=unload_result)
        self.services = FakeServiceRegistry()
        self.states = FakeStateMachine({})

    @property
    def loop(self):
        """The running loop -- `async_track_point_in_utc_time` calls `loop.call_at` on it.

        Needed since `CoverLogicCoordinator.async_setup` stopped evaluating
        inline and started arming the settle window like any other evaluation
        (see that method's docstring). A property rather than an attribute
        because these tests build the fake outside `asyncio.run(...)` and there
        is no loop to capture yet at that point.
        """
        return asyncio.get_running_loop()

    def async_run_hass_job(self, job, *args):
        """Run a `HassJob`'s target -- the one thing a fired timer asks of `hass`.

        Deliberately not a reimplementation of `HomeAssistant.async_run_hass_job`'s
        job-type dispatch: the coordinator's only timer callbacks are coroutine
        functions, so the coroutine is turned into a task and nothing else is
        guessed at. Anything richer belongs on `hass_factory`'s real
        `HomeAssistant`, which is what `test_coordinator.py` and friends use
        precisely because they care how timers behave.
        """
        result = job.target(*args)
        if asyncio.iscoroutine(result):
            return asyncio.get_running_loop().create_task(result)
        return None

    async def async_add_executor_job(self, target, *args):
        """Genuinely run `target` off the current thread, like the real one does.

        Not a same-thread stub -- `test_init.py`'s blocking-I/O test asserts
        the target ran on a different thread than the caller, so this must
        actually hop through an executor (`loop.run_in_executor`, the same
        primitive `HomeAssistant.async_add_executor_job` itself calls) rather
        than just awaiting the call in place.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, target, *args)


@pytest.fixture
def setup_hass():
    """Factory: normal unload by default, or `setup_hass(unload_result=False)` for a refused one."""

    def _make(*, unload_result=True):
        return FakeSetupHass(unload_result=unload_result)

    return _make


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
    """Stands in for `hass` as seen by a `ConfigFlow` step: `.config_entries` and
    `.async_add_executor_job`.
    """

    def __init__(self, existing_entry=None):
        """Wrap a `FakeConfigEntries` seeded with `existing_entry` (or none)."""
        self.config_entries = FakeConfigEntries(existing_entry)

    async def async_add_executor_job(self, target, *args):
        """Genuinely run `target` off the current thread -- see `FakeSetupHass`'s
        identical method for why this cannot be a same-thread stub.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, target, *args)


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
    `async_track_point_in_utc_time` to genuinely dispatch events
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
    call and used from a different one fails opaquely (a scheduled
    timer's `loop.call_at` binds the now-closed original loop) -- so a caller
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


class FakeSubentry:
    """Duck-typed stand-in for `homeassistant.config_entries.ConfigSubentry`.

    Mutable, unlike the real frozen dataclass: `FakeSubentryConfigEntries.
    async_update_subentry` (below) needs somewhere to write the new
    `data`/`title` a reconfigure step produces, standing in for what the real
    `hass.config_entries.async_update_subentry` does to the real, frozen one
    via `object.__setattr__`.
    """

    def __init__(self, subentry_id, subentry_type, data, title=""):
        """Store the attributes `config_store.config_from_subentries` and
        `ConfigSubentryFlow` itself read.
        """
        self.subentry_id = subentry_id
        self.subentry_type = subentry_type
        self.data = dict(data)
        self.title = title


class FakeSubentryEntry:
    """Duck-typed stand-in for `homeassistant.config_entries.ConfigEntry`, as seen
    from inside a subentry flow.

    `config_store.config_from_subentries` and `ConfigSubentryFlow._get_entry`/
    `_get_reconfigure_subentry` only ever read `.data` and `.subentries` off
    the entry -- see `config_store.py`'s own "Duck-typed on purpose"
    docstring section. `.subentries` is a plain, mutable `dict` here, unlike
    the real `MappingProxyType`: a test attaches the subentry a `CREATE_ENTRY`
    result would have produced by calling `add_subentry` below, mirroring
    what the real `ConfigSubentryFlowManager` does once a flow finishes --
    the same tradeoff `test_config_flow.py`'s own module docstring explains
    for why that suite drives flow steps directly instead of the full
    manager.
    """

    def __init__(self, entry_id="entry1", data=None, options=None):
        """Start with no subentries; `data`/`options` default to `{}`."""
        self.entry_id = entry_id
        self.data = data or {}
        # A plain dict, unlike the real `MappingProxyType`. Only ever read
        # through `.get(...)` and replaced wholesale by `async_update_entry`
        # below, which is exactly what `options_flow.async_step_execution`
        # (the one screen that writes options at all) does to the real one.
        self.options = dict(options or {})
        self.subentries: dict[str, FakeSubentry] = {}

    def add_subentry(self, subentry_type, data, *, title=""):
        """Attach a new subentry, as a finished add flow would. Returns its id."""
        subentry_id = f"sub{len(self.subentries)}"
        self.subentries[subentry_id] = FakeSubentry(subentry_id, subentry_type, data, title)
        return subentry_id


@pytest.fixture
def subentry_entry():
    """Factory: `subentry_entry()` -> a fresh, empty `FakeSubentryEntry`."""
    return FakeSubentryEntry


class FakeSubentryConfigEntries:
    """Stands in for `hass.config_entries`, as seen from inside a subentry flow step.

    Covers exactly the two calls `ConfigSubentryFlow`'s own base-class
    methods make on it: `.async_get_known_entry(...)` (used by `_get_entry`/
    `_get_reconfigure_subentry`) and `.async_update_subentry(...)` (used by
    `async_update_and_abort`). `unique_id` is accepted and ignored -- none of
    this project's subentry flows set one.
    """

    def __init__(self, entry):
        """Wrap the one `FakeSubentryEntry` this fake ever returns."""
        self._entry = entry

    def async_get_known_entry(self, entry_id):
        """Return the wrapped entry regardless of `entry_id` -- there is only ever one."""
        return self._entry

    def async_update_subentry(self, entry, subentry, *, data=None, title=None, unique_id=None):
        """Write `data`/`title` onto `subentry` in place, like the real update does."""
        if data is not None:
            subentry.data = dict(data)
        if title is not None:
            subentry.title = title
        return True


class FakeSubentryHass:
    """Stands in for `hass` as seen from inside a `ConfigSubentryFlow` step.

    Only `.config_entries` is touched: none of this project's subentry-flow
    steps do blocking I/O, so unlike `FakeFlowHass` this fake needs no
    `async_add_executor_job`.
    """

    def __init__(self, entry):
        """Wrap `entry` behind a `FakeSubentryConfigEntries`."""
        self.config_entries = FakeSubentryConfigEntries(entry)


@pytest.fixture
def subentry_hass():
    """Factory: `subentry_hass(entry)` -> hass as seen from inside a subentry flow."""

    def _make(entry):
        return FakeSubentryHass(entry)

    return _make


class FakeServiceConfigEntries:
    """Stands in for `hass.config_entries`, as seen from `services.py`.

    Covers exactly the four calls `services._async_import_config`/
    `_async_export_config` make: `.async_entries`, `.async_add_subentry`,
    `.async_remove_subentry`, `.async_update_entry`. Mutates the wrapped
    `FakeServiceEntry` directly and in place -- the same tradeoff
    `FakeSubentryEntry`/`FakeSubentryConfigEntries` make for the subentry
    flows, a plain mutable `dict`/attribute standing in for what the real
    manager does to a frozen, `MappingProxyType`-backed `ConfigEntry` via
    `object.__setattr__`.
    """

    def __init__(self, entries):
        """Wrap the entries `async_entries` should return -- zero or one in every
        test here, this integration being single-instance (see
        `services._get_entry`'s docstring).
        """
        self._entries = list(entries)

    def async_entries(self, domain):
        """Return every wrapped entry; `domain` is ignored -- there is only one domain here."""
        return list(self._entries)

    def async_add_subentry(self, entry, subentry):
        """Insert `subentry` keyed by its own `subentry_id`, like the real manager does."""
        entry.subentries[subentry.subentry_id] = subentry
        return True

    def async_remove_subentry(self, entry, subentry_id):
        """Drop `subentry_id`, like the real manager does."""
        del entry.subentries[subentry_id]
        return True

    def async_update_entry(self, entry, *, data=None, **_ignored):
        """Replace `entry.data`; every other keyword the real signature accepts is unused here."""
        if data is not None:
            entry.data = dict(data)
        return True


class FakeServiceEntry:
    """Duck-typed stand-in for `homeassistant.config_entries.ConfigEntry`, as seen
    from `services.py`.

    `.subentries` values are real `homeassistant.config_entries.ConfigSubentry`
    instances, not a third duck-typed stand-in -- cheap to construct without a
    running event loop, and exactly what `hass.config_entries.async_add_subentry`
    builds in production (`services._async_import_config`), so a test pre-seeding
    "already configured" starts from the identical shape.
    """

    def __init__(self, entry_id="entry1", data=None, subentries=None):
        """Start with `subentries` (default: none) and `data` (default: `{}`)."""
        self.entry_id = entry_id
        self.data = data or {}
        self.subentries = dict(subentries or {})


@pytest.fixture
def service_entry():
    """Factory: `service_entry(subentries={...}, data={...})` -> a fresh `FakeServiceEntry`."""
    return FakeServiceEntry


class FakeServiceHass:
    """Stands in for `hass` as seen from inside `services._async_import_config`/
    `_async_export_config`/`async_register_services`/`async_unregister_services`:
    `.config_entries`, `.services` and `.async_add_executor_job`.
    """

    def __init__(self, entries):
        """Wrap `entries`; start with a fresh `FakeServiceConfigEntries`/`FakeServiceRegistry`."""
        self.config_entries = FakeServiceConfigEntries(entries)
        self.services = FakeServiceRegistry()

    async def async_add_executor_job(self, target, *args):
        """Genuinely run `target` off the current thread -- see `FakeSetupHass`'s
        identical method for why this cannot be a same-thread stub.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, target, *args)


@pytest.fixture
def service_hass():
    """Factory: `service_hass([entry])` (or `service_hass([])` for 'no entry configured yet')."""

    def _make(entries=()):
        return FakeServiceHass(entries)

    return _make


class FakeServiceCall:
    """Duck-typed stand-in for `homeassistant.core.ServiceCall`: only `.data` is read
    by `services._async_import_config`/`_async_export_config`.
    """

    def __init__(self, data):
        """Store the already-schema-coerced `data` a real service call would carry."""
        self.data = data


@pytest.fixture
def service_call():
    """Factory: `service_call({"path": ...})` -> a fake `ServiceCall`."""
    return FakeServiceCall


class FakeOptionsConfigEntries:
    """Stands in for `hass.config_entries`, as seen from inside the options flow.

    A superset of `FakeSubentryConfigEntries`'s surface: the options flow
    itself needs `.async_get_known_entry` (its own `config_entry` property)
    plus `.async_add_subentry`/`.async_update_subentry`/`.async_remove_
    subentry` (every add/edit/remove screen); `services._async_import_config`/
    `_async_export_config`, which the import/export screen calls directly
    (see `options_flow.async_step_import_export`'s own docstring for why not
    through the service-call bus), additionally need `.async_entries` and
    `.async_update_entry`. One fake covers both call sites because both are
    exercised from the same flow in `test_options_flow.py`.
    """

    def __init__(self, entry):
        """Wrap the one entry this fake ever returns."""
        self._entry = entry

    def async_get_known_entry(self, entry_id):
        """Return the wrapped entry regardless of `entry_id` -- there is only ever one."""
        return self._entry

    def async_entries(self, domain):
        """Return the wrapped entry as a one-element list, like the real manager does."""
        return [self._entry]

    def async_add_subentry(self, entry, subentry):
        """Insert `subentry` keyed by its own `subentry_id`, like the real manager does."""
        entry.subentries[subentry.subentry_id] = subentry
        return True

    def async_update_subentry(self, entry, subentry, *, data=None, title=None, unique_id=None):
        """Write `data`/`title` onto `subentry` in place, like the real update does."""
        if data is not None:
            subentry.data = dict(data)
        if title is not None:
            subentry.title = title
        return True

    def async_remove_subentry(self, entry, subentry_id):
        """Drop `subentry_id`, like the real manager does."""
        del entry.subentries[subentry_id]
        return True

    def async_update_entry(self, entry, *, data=None, options=None, **_ignored):
        """Replace `entry.data`/`entry.options`, like the real manager does.

        `options` is named explicitly rather than swallowed by `**_ignored`:
        `async_step_execution` writes the `dry_run` switch through exactly this
        keyword, and a fake that silently dropped it would let a test assert a
        toggle "worked" while nothing was ever stored.
        """
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)
        return True


class FakeOptionsHass:
    """Stands in for `hass` as seen from inside the options flow.

    `.config_entries` (above) and `.async_add_executor_job` (the "check
    against the old matrix" screen's blocking `load_config_file`, and
    `services._async_load_and_validate`'s equivalent read for import/export)
    -- no `.services`, unlike `FakeServiceHass`: the options flow deliberately
    never dispatches through `hass.services.async_call` (see `options_flow.
    async_step_import_export`'s own docstring), so nothing here ever reads
    that attribute.
    """

    def __init__(self, entry):
        """Wrap `entry` behind a `FakeOptionsConfigEntries`."""
        self.config_entries = FakeOptionsConfigEntries(entry)

    async def async_add_executor_job(self, target, *args):
        """Genuinely run `target` off the current thread -- see `FakeSetupHass`'s
        identical method for why this cannot be a same-thread stub.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, target, *args)


@pytest.fixture
def options_hass():
    """Factory: `options_hass(entry)` -> hass as seen from inside the options flow."""

    def _make(entry):
        return FakeOptionsHass(entry)

    return _make
