"""Cover Logic: a universal rule-based cover (blind) controller.

Phase 1 is the decision core (`config_schema`, `model`, `world`,
`conditions`, `engine`, `validation`) -- pure Python, no Home Assistant
imports, tested standalone. Phase 2 adds the Home Assistant layer; this
module is the config entry's entry point (`async_setup_entry`,
`async_unload_entry`, and -- since this task -- `async_migrate_entry`, which
moves an entry created before phase 4 (a config file path, no subentries)
onto subentries, the shape `subentry_flow.py`'s subentry flows and
`services.py`'s `import_config`/`export_config` already assume).

Every Home Assistant name this file needs is either deferred behind
`TYPE_CHECKING` (for annotations, quoted as strings since this project does
not use `from __future__ import annotations` -- see `model.py`'s `Keep`
docstring for the established precedent) or imported inside the function
that needs it at runtime -- never at module level. This is not stylistic:
`custom_components/cover_logic/__init__.py` is executed, as any package's
`__init__.py` is, merely by importing one of the pure submodules
(`from cover_logic.config_schema import ...`) -- including from the
system-Python test run, which has no `homeassistant` installed at all (see
`tests/ha/conftest.py`'s own docstring on the same constraint). An
unconditional `import homeassistant` here would break every one of those
imports, not just this module's own tests.
"""

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .config_schema import ConfigError, load_config_file
from .config_store import config_from_subentries
from .conformance import diff_configs, repo_fixture_path
from .const import CONF_CONFIG_PATH, CONFIG_ENTRY_VERSION, DOMAIN
from .model import Config
from .readiness import name_list
from .validation import ERROR, validate

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import CoverLogicCoordinator

_LOGGER = logging.getLogger(__name__)

# `issue_registry.async_create_issue`/`async_delete_issue` key this entry's
# own repair issue; see `_check_fixture_conformance`.
_FIXTURE_DRIFT_ISSUE = "fixture_drift"

# The same mechanism for the one question a fresh install needs answered;
# see `_check_referenced_entities`.
_UNKNOWN_ENTITIES_ISSUE = "unknown_entities"

# Plain strings, not `homeassistant.const.Platform.SENSOR` -- an enum import
# would be one more Home Assistant name at module level, exactly what this
# file's own docstring rules out. Home Assistant's platform-forwarding calls
# accept plain strings just as well.
PLATFORMS: list[str] = ["sensor"]


@dataclass
class CoverLogicData:
    """What `entry.runtime_data` carries: the parsed config and its coordinator."""

    config: Config
    coordinator: "CoverLogicCoordinator"


if TYPE_CHECKING:
    type CoverLogicConfigEntry = ConfigEntry[CoverLogicData]


async def async_setup_entry(hass: "HomeAssistant", entry: "CoverLogicConfigEntry") -> bool:
    """Set up cover_logic from a config entry.

    Reads the `Config` from the YAML file named by
    `entry.data[CONF_CONFIG_PATH]` if that key is present, otherwise from
    `entry.subentries` -- which source is authoritative is decided by which
    key `entry.data` carries, not by whether `entry.subentries` happens to be
    non-empty. That distinction matters since phase 5: a brand-new entry
    created by `config_flow.py`'s "start empty" or "set up blinds now" setup
    steps carries *no* `CONF_CONFIG_PATH` and legitimately has zero
    subentries (a fresh install with nothing configured yet, or one where
    every selected cover was later removed) -- checking `entry.subentries`
    truthiness the way this used to would send that entry down the YAML
    branch and crash on the now-absent `CONF_CONFIG_PATH` key. Checking for
    the key directly instead means "no subentries yet" and "no subentries
    because the source is a file" are told apart by the one fact that
    actually distinguishes them.

    `validate()` runs over the result either way. An `ERROR`-severity
    problem refuses the start -- `ConfigEntryNotReady`, naming every such
    problem -- rather than running with rules nobody checked; a
    `WARNING`-severity one is only logged, not fatal. A brand-new,
    still-incomplete configuration (no blinds, or blinds with no zone yet)
    hits this same `ConfigEntryNotReady` path, not a crash: the entry still
    exists, and its subentries -- and, since phase 5, the options-flow menu
    over them -- stay reachable regardless of setup state, which is how a
    user actually finishes configuring it. A missing or unparsable file (the
    YAML branch only -- reading subentries does no I/O and cannot raise
    `OSError`) fails the same clean way, not with a raw traceback: both
    `ConfigError` (bad YAML/schema/subentries) and `OSError` become
    `ConfigEntryNotReady`.

    Both sources are re-read on every call, including a config entry reload
    -- nothing about the parsed `Config` is cached across calls or stashed at
    module level. That is deliberate: editing the YAML (or a subentry) and
    reloading the entry is the whole debugging loop for phases 2-4.
    """
    # Deferred: see the module docstring for why this cannot be a top-level
    # import.
    from homeassistant.exceptions import ConfigEntryNotReady  # noqa: PLC0415

    if CONF_CONFIG_PATH in entry.data:
        path = entry.data[CONF_CONFIG_PATH]
        try:
            config = await hass.async_add_executor_job(load_config_file, path)
        except ConfigError as err:
            msg = f"cover_logic: config at {path!r} is invalid: {err}"
            raise ConfigEntryNotReady(msg) from err
        except OSError as err:
            msg = f"cover_logic: config at {path!r} could not be read: {err}"
            raise ConfigEntryNotReady(msg) from err
        source = f"file {path!r}"
    else:
        try:
            config = config_from_subentries(entry)
        except ConfigError as err:
            msg = f"cover_logic: this entry's subentries could not be read: {err}"
            raise ConfigEntryNotReady(msg) from err
        source = "subentries"
        # Only meaningful once subentries are the source of truth -- see
        # that function's own docstring for why a still-legacy, path-based
        # entry (the `if` branch above) has nothing of this kind to check.
        await _check_fixture_conformance(hass, config)

    problems = validate(config)
    errors = [problem for problem in problems if problem.severity == ERROR]
    if errors:
        summary = "; ".join(f"{problem.code}: {problem.message}" for problem in errors)
        msg = f"cover_logic: config from {source} has errors: {summary}"
        raise ConfigEntryNotReady(msg)

    for problem in problems:
        _LOGGER.warning(
            "cover_logic: config from %s: %s: %s", source, problem.code, problem.message
        )

    # Deferred: see the module docstring for why this cannot be a top-level
    # import.
    from .coordinator import CoverLogicCoordinator  # noqa: PLC0415

    coordinator = CoverLogicCoordinator(hass, config, entry)
    await coordinator.async_setup()

    entry.runtime_data = CoverLogicData(config=config, coordinator=coordinator)

    # Deferred, and armed rather than called: see `_check_referenced_entities`
    # for why this one question can only be asked once Home Assistant has
    # finished starting.
    from homeassistant.helpers.start import async_at_started  # noqa: PLC0415

    async def _at_started(_hass: "HomeAssistant") -> None:
        await _check_referenced_entities(hass, config)

    entry.async_on_unload(async_at_started(hass, _at_started))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Deferred: see the module docstring for why this cannot be a top-level
    # import. `import_config`/`export_config` are entry-independent in
    # behaviour (there is only ever the one entry -- see
    # `services._get_entry`'s docstring) but registered here, alongside it,
    # rather than from an `async_setup` this integration has no other reason
    # to define.
    from .services import async_register_services  # noqa: PLC0415

    async_register_services(hass)

    return True


def _read_repo_fixture() -> tuple[Path | None, Config | None]:
    """Resolve and parse this checkout's own fixture. Blocking: executor only.

    Both halves touch the filesystem -- `repo_fixture_path` stats the path,
    `load_config_file` reads and parses it -- so they travel to the executor
    thread together, in one hop, rather than as two `async_add_executor_job`
    calls around the same file. A non-`None` path with a `None` config means
    the fixture is there but unreadable or unparsable; see
    `_check_fixture_conformance` for why that is treated exactly like "no
    fixture here at all".
    """
    fixture = repo_fixture_path()
    if fixture is None:
        return None, None
    try:
        return fixture, load_config_file(fixture)
    except (ConfigError, OSError):
        return fixture, None


async def _check_fixture_conformance(hass: "HomeAssistant", config: Config) -> None:
    """Raise or clear the `fixture_drift` repair issue for this checkout's own fixture.

    A no-op everywhere `conformance.repo_fixture_path()` returns `None` -- see
    that function's docstring for why that has to include every installation
    of this integration except this project's own development host.
    Deliberately a `Config`-equality comparison (`conformance.diff_configs`),
    never a re-read of the fixture as text: a difference in comments, key
    order or quoting is not drift, only a difference in meaning is (see the
    task report's "meaning, not text" section).

    Called from `async_setup_entry` on every setup of a subentry-backed
    entry, not just once at migration time -- a subentry can be added,
    edited or removed at any point after that through the flows in
    `config_flow.py`, and each of those is exactly the moment this project's
    own `MODELS.md` (Sec. 5) warns the gate could start measuring something
    other than what the house runs. A repair issue is Home Assistant's own
    mechanism for "something needs attention and must stay visible until it
    doesn't" (Settings -> System -> Repairs), which is what "must not be able
    to pass unnoticed" (this task's own requirement) means for a live house,
    as opposed to `tests/parity/test_subentry_conformance.py`'s dev-time
    version of the same check -- see that module's docstring for why both
    exist instead of just one.

    Async because reading the fixture is blocking I/O and this runs on the
    event loop: `_read_repo_fixture` goes through `hass.async_add_executor_job`,
    the same reasoning as this module's `async_setup_entry` and
    `config_flow._describe_problems` -- `Path.read_text` and the `open` under
    it are both on Home Assistant's own `block_async_io` list, so a direct
    call here logged two "Detected blocking call" warnings on every start.
    """
    # Deferred: see the module docstring for why this cannot be a top-level
    # import.
    from homeassistant.helpers import issue_registry as ir  # noqa: PLC0415

    fixture, reference = await hass.async_add_executor_job(_read_repo_fixture)
    if reference is None:
        # No fixture here at all, or one that no longer parses -- the
        # fixture's own health is `tests/test_fixture_dom_peter.py`'s job, and
        # neither case is evidence the *live* configuration drifted, so both
        # clear rather than raise the issue.
        ir.async_delete_issue(hass, DOMAIN, _FIXTURE_DRIFT_ISSUE)
        return

    diff = diff_configs(config, reference)
    if not diff:
        ir.async_delete_issue(hass, DOMAIN, _FIXTURE_DRIFT_ISSUE)
        return

    _LOGGER.warning("cover_logic: live subentries no longer match %s: %s", fixture, ", ".join(diff))
    ir.async_create_issue(
        hass,
        DOMAIN,
        _FIXTURE_DRIFT_ISSUE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_FIXTURE_DRIFT_ISSUE,
        translation_placeholders={"fields": ", ".join(sorted(diff))},
    )


async def _check_referenced_entities(hass: "HomeAssistant", config: Config) -> None:
    """Which entities this configuration names that this Home Assistant has never heard of.

    The one question a fresh install needs answered, and the one nothing here
    answered before. `validate` cannot: it is pure, and "does this entity
    exist" is not a fact about the configuration. `readiness.assess` looks the
    same but is not -- it answers "could I read this *now*", it runs on every
    evaluation, and it deliberately exempts every read a node gave a
    `default:`. Those exemptions are right for acting and wrong for
    installing: a configuration whose reads are all defaulted would reference
    nine entities that do not exist, report nothing at all, and quietly behave
    as though every input in the house were absent -- which is the silent
    version of the failure this project already has a repair issue for.

    So this asks a different question, once: is the entity *known here*, in
    either the state machine or the entity registry. Both, because neither
    alone is the house: a YAML template sensor without a `unique_id` is never
    in the registry, and an entity that is registered but whose integration
    has not set it up yet is not in the state machine. Absent from both is the
    only answer that means "you have not created this".

    Blinds are included unconditionally, unlike in `_entity_ids`, where
    subscribing to them is a behaviour with a cost. Here it is the most
    valuable name of all: a `cover.` that does not exist is a rule this house
    can never carry out.

    Runs from `async_at_started`, not from `async_setup_entry`. At setup time
    Home Assistant may not have restored state or finished setting up other
    integrations, so "absent" and "not yet" are indistinguishable -- and
    mistaking the second for the first is the exact reading error
    `readiness.py` exists for, one layer down.
    """
    # Deferred: see the module docstring for why this cannot be a top-level
    # import.
    from homeassistant.helpers import entity_registry as er, issue_registry as ir  # noqa: PLC0415

    from .coordinator import _entity_ids  # noqa: PLC0415

    registry = er.async_get(hass)
    referenced = _entity_ids(config) | set(config.blinds)
    unknown = tuple(
        sorted(
            entity
            for entity in referenced
            if hass.states.get(entity) is None and registry.async_get(entity) is None
        )
    )
    if not unknown:
        ir.async_delete_issue(hass, DOMAIN, _UNKNOWN_ENTITIES_ISSUE)
        return

    _LOGGER.warning(
        "cover_logic: configuration references %d entities this Home Assistant does not have: %s",
        len(unknown),
        name_list(unknown),
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        _UNKNOWN_ENTITIES_ISSUE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_UNKNOWN_ENTITIES_ISSUE,
        translation_placeholders={"count": str(len(unknown)), "entities": name_list(unknown)},
    )


async def async_migrate_entry(hass: "HomeAssistant", entry: "CoverLogicConfigEntry") -> bool:
    """Migrate an old-shape entry (a config file path, no subentries) onto subentries.

    Home Assistant's own migration mechanism: called automatically before
    `async_setup_entry` whenever `entry.version` is below this integration's
    current `config_flow.CoverLogicConfigFlow.VERSION`
    (`const.CONFIG_ENTRY_VERSION`), never invented as some separate one-off
    step. Version 1 is the original shape this integration shipped with --
    `entry.data[CONF_CONFIG_PATH]`, no subentries, `async_setup_entry` reading
    the YAML file on every start. Version 2 is subentries as the source of
    truth: the file's content is imported once, and `CONF_CONFIG_PATH` is
    dropped from `entry.data` so nothing about this entry depends on that
    path any longer.

    The file itself is left completely untouched -- it is the user's own
    backup (the operator's own house-config `CLAUDE.md` states this same
    rule for `automations.yaml`/`scripts.yaml`: never assume permission to
    remove a file just because its content has been read once), and this
    function never opens it for anything but reading.

    Idempotent, two ways at once:

    - If `entry.subentries` is already non-empty (a previous migration
      attempt got far enough to import before being interrupted, or the
      subentry flows / `import_config` populated the entry some other way
      before the version ever got bumped), the import step is skipped
      entirely and only the version bump below runs -- never a second import
      layered on top of a first, which would duplicate every blind, zone,
      mode, condition, value and rule.
    - If `entry.version` is already at or above `CONFIG_ENTRY_VERSION`, this
      returns `True` immediately, doing nothing at all -- the entry is
      already current, whether this was called because a caller (a test,
      most concretely) invoked it directly rather than through Home
      Assistant's own version check.

    Returns `False` (migration failed, entry left at its old version for
    another attempt on the next start) if the file cannot be read or parsed
    -- this is the one case with no safe default: importing nothing would
    silently discard the house's configuration, and there is no fallback
    file to read instead once this integration decides subentries are the
    source of truth.
    """
    if entry.version >= CONFIG_ENTRY_VERSION:
        return True

    if not entry.subentries:
        path = entry.data.get(CONF_CONFIG_PATH)
        if path is not None:
            # Deferred: see the module docstring for why this cannot be a
            # top-level import.
            from homeassistant.config_entries import ConfigSubentry  # noqa: PLC0415

            from .config_store import subentries_from_config  # noqa: PLC0415
            from .services import _title_for  # noqa: PLC0415  (reused, not reinvented)

            try:
                config = await hass.async_add_executor_job(load_config_file, path)
            except (ConfigError, OSError):
                _LOGGER.exception(
                    "cover_logic: migration could not read %s, leaving this entry at "
                    "version %s for the next attempt",
                    path,
                    entry.version,
                )
                return False

            for subentry_type, data in subentries_from_config(config):
                hass.config_entries.async_add_subentry(
                    entry,
                    ConfigSubentry(
                        data=data,
                        subentry_type=subentry_type,
                        title=_title_for(subentry_type, data),
                        unique_id=None,
                    ),
                )

            hass.config_entries.async_update_entry(
                entry, data=_migrated_data(entry), version=CONFIG_ENTRY_VERSION
            )
            return True

    # Version 2 -> 3: guards used to live in `entry.data["guards"]`, one
    # opaque list Home Assistant offers no UI for. An entry that already has
    # subentries (the v1 import above, or the subentry flows) still carries
    # them there, so they move across before the key is dropped below --
    # otherwise `config_from_subentries` would come back with no guards at
    # all, i.e. a house with its interlocks silently gone.
    _migrate_guards_to_subentries(hass, entry)

    # Either the import above already ran (subentries non-empty) or there was
    # never a path to import from -- either way, nothing left to import, only
    # the version to catch up so this function is not called again.
    hass.config_entries.async_update_entry(
        entry, data=_migrated_data(entry), version=CONFIG_ENTRY_VERSION
    )
    return True


def _migrated_data(entry: "CoverLogicConfigEntry") -> dict:
    """`entry.data` without the two keys migration retires.

    `CONF_CONFIG_PATH` stopped being read at version 2, `guards` at version 3
    (see `config_store._ordered_guards`). Both are dropped in one place so
    the three call sites above cannot disagree about which keys survive.
    """
    return {k: v for k, v in entry.data.items() if k not in (CONF_CONFIG_PATH, "guards")}


def _migrate_guards_to_subentries(hass: "HomeAssistant", entry: "CoverLogicConfigEntry") -> None:
    """Turn `entry.data["guards"]` into guard subentries, once.

    Idempotent the same way the version-1 import above is: if a guard
    subentry already exists, this returns without adding a second copy of
    anything -- the case where a previous attempt got as far as adding
    subentries but not as far as the version bump.

    Order comes from `config_store.guard_subentry_items`, the same function
    `subentries_from_config` uses, so a migrated house and an imported one
    end up with identical numbering.
    """
    # Deferred: see the module docstring for why this cannot be a top-level
    # import.
    from homeassistant.config_entries import ConfigSubentry  # noqa: PLC0415

    from .config_store import GUARD, guard_subentry_items  # noqa: PLC0415
    from .services import _title_for  # noqa: PLC0415  (reused, not reinvented)

    if any(sub.subentry_type == GUARD for sub in entry.subentries.values()):
        return

    for subentry_type, data in guard_subentry_items(list(entry.data.get("guards") or [])):
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=data,
                subentry_type=subentry_type,
                title=_title_for(subentry_type, data),
                unique_id=None,
            ),
        )


async def async_unload_entry(hass: "HomeAssistant", entry: "CoverLogicConfigEntry") -> bool:
    """Unload a config entry, leaving nothing behind.

    Platforms are unloaded first, before the coordinator itself is torn
    down -- `sensor.py`'s entity unsubscribes from
    `CoverLogicCoordinator.add_listener` in its own `async_will_remove_from_hass`,
    so the coordinator must still be alive and holding that listener list
    while `async_unload_platforms` runs. A failed platform unload aborts
    before touching the coordinator at all, matching Home Assistant's own
    convention that an unload returning `False` leaves the entry's state
    untouched for a retry. Only once platforms are confirmed gone are the
    coordinator's own subscription and any pending settle torn down via
    `CoverLogicCoordinator.async_unload`. This exists so a config entry reload
    -- unload, then set up again -- re-reads the file both times and starts a
    fresh coordinator each time; see `async_setup_entry`'s docstring for why
    the re-read matters.

    The two services are removed last, after the coordinator -- this
    integration supports exactly one config entry (see
    `services._get_entry`'s docstring), so once it is unloading there is no
    entry left for either service to act on.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    await entry.runtime_data.coordinator.async_unload()

    # Deferred: see the module docstring.
    from .services import async_unregister_services  # noqa: PLC0415

    async_unregister_services(hass)

    return True
