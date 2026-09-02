r"""The `import_config`/`export_config` services: the bridge between YAML and subentries.

`config_schema.py` reads/writes a `Config` as YAML text. `config_store.py`
reads/writes the same `Config` as Home Assistant config-entry subentries.
Between phase 4's subentry flows (click-driven editing) and the YAML file
`async_setup_entry` still runs off, nothing ever moved a configuration from
one representation to the other -- this module is that move, exposed as two
services rather than a config-flow step, since neither direction fits "fill
in a form": an import may replace dozens of subentries at once, and an
export is a plain file write with no user choice to make once the path is
given.

Imports `homeassistant` unconditionally: this is the Home Assistant layer,
never imported by `cover_logic/__init__.py` at module level (see that
module's own docstring), only called from inside `async_setup_entry` at
runtime -- the same reasoning `coordinator.py`/`sensor.py`/`config_flow.py`
already give for their own unconditional imports.

**Replace, never merge.** `import_config` either replaces every subentry
this integration owns, or refuses outright if the entry already has any and
`overwrite` was not passed -- see `_async_import_config`'s own docstring for
why a partial merge is not on offer: it would leave a state matching neither
the file just imported nor whatever was configured before.

**Validate before writing.** Both services build and validate a complete
`Config` -- `import_config` via `load_config_file` + `validate()`, plus its
own `subentries_from_config` round-trip self-check (see `config_store.py`)
-- entirely before touching `entry.subentries`. `dry_run` stops exactly
there: every check above still runs (so a dry run reports the same refusal
a real run would hit, not a false "this would succeed"), only the final
mutation is skipped.

**Path safety.** `export_config` refuses to write through a symlink. On this
project's own host, `/config/cover_logic.yaml` (`const.DEFAULT_CONFIG_PATH`)
*is* a symlink, to `fixtures/dom_peter.yaml` -- the migration gate's fixture
(see `MODELS.md` Sec. 5) -- so writing through it without this check would
silently change what the gate measures the next time it runs. The guard is
general (any symlink target, not that specific path) rather than a
hard-coded exception for one file: this integration has no way to know
every path a symlink might shadow on some other installation. Writing
directly to a *non-symlink* path -- including `fixtures/dom_peter.yaml`
itself, named directly rather than through the symlink -- is deliberately
still allowed: re-exporting the house's own real configuration after editing
it through the UI is a legitimate use this task's brief does not ask to
block, and `MODELS.md`'s "do not touch `fixtures/dom_peter.yaml`" rule is
about not editing it for unrelated (documentation/example) reasons, not
about this service.

**Comment preservation.** `dump_config` (`config_schema.py`) writes from the
parsed `Config`, which carries no comments at all -- exporting onto an
existing file that has any would silently discard them, with no way back.
`fixtures/dom_peter.yaml` itself has 49 comment lines today, explaining the
transcription from the live house's Jinja matrix; losing them would not
break anything the migration gate checks (`Config` equality does not see
comments either), only the reviewability the project's own `MODELS.md`
relies on. This is exactly the class of danger the operator's own `CLAUDE.md`
documents for `safe_load` + `dump` round-trips of `automations.yaml`/
`scripts.yaml`. `_check_export_target_has_no_comments` refuses outright,
before the write happens, whenever the target already exists and contains
at least one whole-line comment (`^\\s*#`, the same check that `CLAUDE.md`
itself prescribes for auditing comment loss around the HA config API) --
see that function's own docstring for why this is an unconditional refusal
rather than an "I mean it" override field. A future task adding real
comment preservation (e.g. switching `config_schema.py` to a round-trip-
preserving YAML library) would remove this guard from the write side, not
add an escape hatch to it.
"""

from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_schema import ConfigError, dump_config_file, load_config_file
from .config_store import (
    BLIND,
    CONDITION,
    GUARD,
    MODE,
    RULE,
    VALUE,
    ZONE,
    config_from_subentries,
    subentries_from_config,
)
from .const import DOMAIN
from .validation import ERROR, Problem, validate

SERVICE_IMPORT_CONFIG = "import_config"
SERVICE_EXPORT_CONFIG = "export_config"

ATTR_PATH = "path"
ATTR_DRY_RUN = "dry_run"
ATTR_OVERWRITE = "overwrite"

IMPORT_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PATH): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
    }
)

EXPORT_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PATH): cv.string,
    }
)


def _title_for(subentry_type: str, data: dict[str, Any]) -> str:
    """A human-readable title for a subentry `import_config` creates.

    Cosmetic only -- `config_from_subentries` never reads `.title` -- so
    this stays a small, local mirror of `subentry_flow.py`'s own per-type
    `_title()` methods rather than a shared function: those methods work off
    a submitted form's `user_input`, this off already-built subentry `data`,
    and reconciling the two shapes is not worth it for a display string with
    no behavioural weight.
    """
    if subentry_type == BLIND:
        return str(data.get("entity", ""))
    if subentry_type in (ZONE, VALUE, CONDITION, MODE):
        return str(data.get("id", ""))
    if subentry_type == RULE:
        return f"{data.get('mode')}.{data.get('zone')} #{data.get('order')}"
    if subentry_type == GUARD:
        # A guard's `name` is optional and often empty, so fall back to the
        # policy -- "what this guard does" is the next most useful label.
        return str(data.get("name") or "").strip() or f"{data.get('policy')} guard"
    return subentry_type


def _summary(problems: list[Problem]) -> list[str]:
    """Render `validate()`'s output the same way `__init__.async_setup_entry` logs it."""
    return [f"{problem.code}: {problem.message}" for problem in problems]


def _get_entry(hass: HomeAssistant) -> Any:
    """Return this integration's one config entry, or refuse if it does not exist yet.

    Single-instance by construction (`CoverLogicConfigFlow.async_step_user`
    aborts a second `user` step), so "the" entry is unambiguous once one
    exists.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_config_entry",
        )
    return entries[0]


def _raise_invalid_config(problems: list[Problem]) -> None:
    errors = [problem for problem in problems if problem.severity == ERROR]
    if errors:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_config",
            translation_placeholders={"error_detail": "; ".join(_summary(errors))},
        )


async def _async_load_and_validate(hass: HomeAssistant, path: str) -> tuple[Any, list[Problem]]:
    """Read and parse `path` off the event loop, then run `validate()` over it.

    Raises `ServiceValidationError` for a bad path or a `Config` with any
    `ERROR`-severity problem -- nothing about the target entry is touched
    before this returns.
    """
    try:
        config = await hass.async_add_executor_job(load_config_file, path)
    except ConfigError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cannot_parse_file",
            translation_placeholders={"path": path, "error_detail": str(err)},
        ) from err
    except OSError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cannot_read_file",
            translation_placeholders={"path": path, "error_detail": str(err)},
        ) from err

    problems = validate(config)
    _raise_invalid_config(problems)
    return config, problems


async def _async_import_config(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Import a YAML file into this entry's subentries, replacing them entirely.

    See the module docstring's "Replace, never merge" / "Validate before
    writing" sections for the two decisions this function's ordering exists
    to enforce: every check below -- parse, `validate()`, the existing-
    config refusal, `subentries_from_config`'s own round-trip self-check --
    runs before the first subentry is touched, and still runs under
    `dry_run` so a dry run reports the same outcome a real run would reach.
    """
    path = call.data[ATTR_PATH]
    dry_run = call.data[ATTR_DRY_RUN]
    overwrite = call.data[ATTR_OVERWRITE]

    entry = _get_entry(hass)
    config, problems = await _async_load_and_validate(hass, path)

    if entry.subentries and not overwrite:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="existing_config",
            translation_placeholders={"count": str(len(entry.subentries))},
        )

    # Raises `ConfigError` -> surfaced as `HomeAssistantError` below if the
    # subentries this would write do not read back to an equal `Config` --
    # see `config_store.subentries_from_config`'s own docstring for why that
    # is this function's bug, not the imported file's.
    try:
        items = subentries_from_config(config)
    except ConfigError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="import_conversion_failed",
            translation_placeholders={"error_detail": str(err)},
        ) from err

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "path": path,
        "blinds": len(config.blinds),
        "zones": len(config.zones),
        "modes": len(config.modes),
        "conditions": len(config.conditions),
        "values": len(config.values),
        "rules": sum(len(rules) for rules in config.rules.values()),
        "replaced_subentries": len(entry.subentries),
        "warnings": _summary(problems),
    }
    if dry_run:
        return summary

    # `async_add_subentry`/`async_remove_subentry`/`async_update_entry` are
    # `@callback` (event-loop only, not blocking I/O) -- run directly, never
    # through `hass.async_add_executor_job` (see the module docstring's
    # "Do not write files on the event loop" counterpart: that rule is about
    # actual file I/O, which this step has none of).
    for subentry_id in list(entry.subentries):
        hass.config_entries.async_remove_subentry(entry, subentry_id)
    for subentry_type, data in items:
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=data,
                subentry_type=subentry_type,
                title=_title_for(subentry_type, data),
                unique_id=None,
            ),
        )
    # No separate guards write: since guards became the seventh subentry type
    # they arrive in `items` above like every other piece of the config, and
    # `entry.data` is no longer a config source (see
    # `config_store._ordered_guards`). Writing the old key here would give
    # `config_from_subentries` nothing extra and leave a stale copy behind.
    return summary


def _check_export_target_has_no_comments(path: Path) -> None:
    r"""Refuse to overwrite an existing file that contains whole-line comments.

    Runs inside `hass.async_add_executor_job`, as `_prepare_export_path`
    before it does: that one only stats `path` and its parent, this reads file
    *content*, and neither belongs on the event loop.

    Does nothing if `path` does not exist yet (a first export has nothing to
    lose) or is a directory/symlink (`_prepare_export_path` already refused
    those before this ever runs).

    Counts a comment the same way this project's own operator already
    audits comment loss elsewhere -- `grep -c '^\\s*#'`, per this repo's
    `CLAUDE.md` on the HA config API silently dumping `automations.yaml` --
    rather than writing a YAML-comment-aware parser: `dump_config` cannot
    reproduce a comment regardless of where mid-line it sits, so counting
    only whole-line comments already catches the exact fixture this guard
    exists for (`fixtures/dom_peter.yaml`, 49 such lines) without pretending
    to attempt inline-comment preservation nobody is building.

    Refuses outright rather than accepting an "I mean it, overwrite anyway"
    field. An override field would have to be passed on *every* legitimate
    re-export of a file that will always have comments -- nothing here ever
    regenerates them -- so it would fossilise into "always pass
    `overwrite_comments: true`", the same rubber-stamping this project's own
    `CLAUDE.md` already warns about for the config-subentry flow's
    `reconfigure_successful` swallowing a real problem. Refusing forces a
    deliberate, manual step instead -- move the commented file aside, or
    hand-merge the comments back afterwards -- rather than a flag that stops
    meaning anything after its first use.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cannot_read_file",
            translation_placeholders={"path": str(path), "error_detail": str(err)},
        ) from err
    comment_lines = sum(1 for line in text.splitlines() if line.strip().startswith("#"))
    if comment_lines:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="export_target_has_comments",
            translation_placeholders={"path": str(path), "count": str(comment_lines)},
        )


def _prepare_export_path(path_str: str) -> Path:
    """Validate `path_str` as an `export_config` destination.

    Only ever stats `path_str` itself and its parent directory -- never
    reads or writes file content. See the module docstring's "Path safety"
    section for why a symlink is refused unconditionally, rather than only
    for one known-dangerous path.

    Runs inside `hass.async_add_executor_job` all the same: a stat is still a
    filesystem call, and the event loop is not where any of them belong.
    """
    path = Path(path_str)
    if path.is_symlink():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="export_path_is_symlink",
            translation_placeholders={"path": str(path)},
        )
    if path.is_dir():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="export_path_is_directory",
            translation_placeholders={"path": str(path)},
        )
    parent = path.parent
    if not parent.is_dir():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="export_parent_missing",
            translation_placeholders={"path": str(parent)},
        )
    return path


async def _async_export_config(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Write this entry's subentries out as a YAML file `config_schema` can read back.

    Structural validity (does this collapse into a `Config` at all) is the
    only gate -- unlike `import_config`, an `ERROR`-severity `validate()`
    problem does not block an export. Blocking here would refuse to let a
    still-being-edited configuration be inspected or backed up as YAML,
    which is a legitimate use `import_config`'s "validate before writing"
    rule does not need to extend to: nothing runs off an exported file the
    way `async_setup_entry` runs off an imported one.

    An existing target that already has whole-line comments still blocks --
    see `_check_export_target_has_no_comments`'s own docstring -- run after
    `_prepare_export_path`'s symlink/directory/missing-parent checks and
    before the write itself, so `dump_config_file` never runs against a
    target this function is about to refuse.
    """
    path_str = call.data[ATTR_PATH]

    entry = _get_entry(hass)
    if not entry.subentries:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="nothing_to_export",
        )

    try:
        config = config_from_subentries(entry)
    except ConfigError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cannot_parse_subentries",
            translation_placeholders={"error_detail": str(err)},
        ) from err

    path = await hass.async_add_executor_job(_prepare_export_path, path_str)
    await hass.async_add_executor_job(_check_export_target_has_no_comments, path)

    try:
        await hass.async_add_executor_job(dump_config_file, path, config)
    except OSError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cannot_write_file",
            translation_placeholders={"path": str(path), "error_detail": str(err)},
        ) from err

    return {
        "path": str(path),
        "blinds": len(config.blinds),
        "zones": len(config.zones),
        "modes": len(config.modes),
        "conditions": len(config.conditions),
        "values": len(config.values),
        "rules": sum(len(rules) for rules in config.rules.values()),
    }


def async_register_services(hass: HomeAssistant) -> None:
    """Register `import_config`/`export_config`, once, regardless of how many times called.

    Called from `async_setup_entry` (there is at most one entry -- see
    `_get_entry`'s docstring -- so there is no "second entry" case to guard
    against the way a multi-instance integration would need to); guarded by
    `has_service` so a config entry reload does not try to register the same
    service twice.
    """
    # Home Assistant decides how to dispatch a registered handler by asking
    # `inspect.iscoroutinefunction` (see `HassJob`/`get_hassjob_callable_job_type`
    # in `homeassistant/core.py`), not by trying it and seeing what comes
    # back. A `lambda call: _async_import_config(hass, call)` fails that
    # check -- a lambda is never a coroutine function, even though calling
    # it produces one -- so the registry classified it as a plain blocking
    # callable and ran it through `hass.async_add_executor_job`, which
    # returns the *coroutine object* the lambda handed back without ever
    # awaiting it. With `supports_response=OPTIONAL` that unawaited coroutine
    # is then handed to the caller as the response payload, which is exactly
    # the `expected a dictionary, but got <class 'coroutine'>` error this
    # fixes: nothing was wrong with `_async_import_config`/`_async_export_config`
    # themselves, only with how the handler passed to `async_register` failed
    # to *look like* a coroutine function to that check. An inner `async def`
    # that awaits the real function is a coroutine function in its own
    # right, so `inspect.iscoroutinefunction` says yes and the registry
    # awaits it like every other async service handler in Home Assistant.
    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_CONFIG):

        async def _import_config(call: ServiceCall) -> ServiceResponse:
            return await _async_import_config(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_CONFIG,
            _import_config,
            schema=IMPORT_CONFIG_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_EXPORT_CONFIG):

        async def _export_config(call: ServiceCall) -> ServiceResponse:
            return await _async_export_config(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_EXPORT_CONFIG,
            _export_config,
            schema=EXPORT_CONFIG_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove both services. Called from `async_unload_entry` -- see that function's docstring."""
    hass.services.async_remove(DOMAIN, SERVICE_IMPORT_CONFIG)
    hass.services.async_remove(DOMAIN, SERVICE_EXPORT_CONFIG)
