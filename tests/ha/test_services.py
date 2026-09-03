"""Tests for the `import_config`/`export_config` services (`services.py`).

Imports Home Assistant, so this module only collects under the Python 3.14
venv -- see `test_init.py`'s own note. Drives `services.py`'s
`_async_import_config`/`_async_export_config` directly, the same tradeoff
`test_config_flow.py`'s own module docstring explains for the top-level
config flow: registering through a real `hass.services` and dispatching a
genuine `ServiceCall` needs a running `ServiceRegistry` this project does
not otherwise need to stand up; `tests/ha/conftest.py`'s
`FakeServiceHass`/`FakeServiceEntry`/`FakeServiceCall` stub exactly the
surface those two functions touch.

One test near the bottom of this file, `test_registered_service_handlers_
are_awaited_as_coroutines`, deliberately does not make that tradeoff. It
exists because a real bug shipped past every other test here: both services
were registered as `lambda call: _async_import_config(hass, call)`, which
Home Assistant's job-type detection does not recognise as a coroutine
function (a lambda wrapping an `async def` is not itself one), so it ran the
handler through an executor and returned the unawaited coroutine object as
the response -- `HomeAssistantError: ... expected a dictionary, but got
<class 'coroutine'>` against the live house, for every caller going through
the service bus (the UI, an automation, Developer Tools). `FakeServiceRegistry`
never asks what job type a handler is, so no test driving it could ever have
caught this; only Home Assistant's own `ServiceRegistry`, on a real
`HomeAssistant` (`tests/ha/conftest.py`'s `hass_factory`), actually exercises
that check. See that test's own docstring for how it tells "awaited
correctly" apart from "not awaited at all" without needing a fully wired
config entry.
"""

import asyncio
from pathlib import Path
import threading
from unittest import mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import ServiceValidationError

from cover_logic.config_schema import load_config, load_config_file
from cover_logic.config_store import BLIND, config_from_subentries, subentries_from_config
from cover_logic.const import DOMAIN
from cover_logic.services import (
    SERVICE_EXPORT_CONFIG,
    SERVICE_IMPORT_CONFIG,
    _async_export_config,
    _async_import_config,
    async_register_services,
    async_unregister_services,
)
from tests.ha.conftest import FakeServiceConfigEntries, FakeServiceEntry

# Zero problems of any severity: one blind, one zone that owns it, one
# fallback mode, and a rule list for that (mode, zone) pair ending in an
# unconditional catch-all rule. Small on purpose -- these tests are about the
# service layer's own decisions (replace-or-refuse, dry-run, path safety),
# not about exercising every corner of `config_schema`/`config_store`
# (already covered by `tests/test_config_schema.py`/`tests/test_config_store.py`).
VALID_TEXT = """
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
ERROR_TEXT = """
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


def _write(tmp_path, text, name="cfg.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _one_existing_subentry():
    """A single pre-existing `blind` subentry, standing in for "already configured"."""
    return ConfigSubentry(
        data={"entity": "cover.existing"},
        subentry_type=BLIND,
        title="cover.existing",
        unique_id=None,
    )


# --- import_config -----------------------------------------------------------


def test_import_into_a_fresh_entry_populates_subentries_matching_the_file(
    tmp_path, service_entry, service_hass, service_call
):
    path = _write(tmp_path, VALID_TEXT)
    entry = service_entry()
    hass = service_hass([entry])

    async def run():
        return await _async_import_config(
            hass, service_call({"path": path, "dry_run": False, "overwrite": False})
        )

    summary = asyncio.run(run())

    assert entry.subentries  # something was actually written
    rebuilt = config_from_subentries(entry)
    assert rebuilt == load_config(VALID_TEXT)
    assert summary["blinds"] == 1
    assert summary["dry_run"] is False


def test_import_refuses_an_existing_config_without_overwrite(
    tmp_path, service_entry, service_hass, service_call
):
    path = _write(tmp_path, VALID_TEXT)
    existing = _one_existing_subentry()
    entry = service_entry(subentries={existing.subentry_id: existing})
    hass = service_hass([entry])

    async def run():
        return await _async_import_config(
            hass, service_call({"path": path, "dry_run": False, "overwrite": False})
        )

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "existing_config"
    # Refused before anything was touched -- the pre-existing subentry is
    # still exactly what it was.
    assert entry.subentries == {existing.subentry_id: existing}


def test_import_with_overwrite_replaces_the_existing_config_entirely(
    tmp_path, service_entry, service_hass, service_call
):
    path = _write(tmp_path, VALID_TEXT)
    existing = _one_existing_subentry()
    entry = service_entry(subentries={existing.subentry_id: existing})
    hass = service_hass([entry])

    async def run():
        return await _async_import_config(
            hass, service_call({"path": path, "dry_run": False, "overwrite": True})
        )

    asyncio.run(run())

    assert existing.subentry_id not in entry.subentries
    assert config_from_subentries(entry) == load_config(VALID_TEXT)


def test_import_dry_run_changes_nothing_but_reports_what_it_would_do(
    tmp_path, service_entry, service_hass, service_call
):
    path = _write(tmp_path, VALID_TEXT)
    entry = service_entry()
    hass = service_hass([entry])

    async def run():
        return await _async_import_config(
            hass, service_call({"path": path, "dry_run": True, "overwrite": False})
        )

    summary = asyncio.run(run())

    assert entry.subentries == {}
    assert summary["dry_run"] is True
    assert summary["blinds"] == 1


def test_import_dry_run_still_refuses_an_existing_config_without_overwrite(
    tmp_path, service_entry, service_hass, service_call
):
    """A dry run previews the *same* outcome a real run would reach -- including a
    refusal -- rather than always reporting success. See `_async_import_config`'s
    own docstring.
    """
    path = _write(tmp_path, VALID_TEXT)
    existing = _one_existing_subentry()
    entry = service_entry(subentries={existing.subentry_id: existing})
    hass = service_hass([entry])

    async def run():
        return await _async_import_config(
            hass, service_call({"path": path, "dry_run": True, "overwrite": False})
        )

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "existing_config"
    assert entry.subentries == {existing.subentry_id: existing}


def test_import_rejects_a_config_with_an_error_severity_problem(
    tmp_path, service_entry, service_hass, service_call
):
    path = _write(tmp_path, ERROR_TEXT)
    entry = service_entry()
    hass = service_hass([entry])

    async def run():
        return await _async_import_config(
            hass, service_call({"path": path, "dry_run": False, "overwrite": False})
        )

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "invalid_config"
    assert entry.subentries == {}


def test_import_of_a_missing_file_raises_cleanly(service_entry, service_hass, service_call):
    entry = service_entry()
    hass = service_hass([entry])

    async def run():
        return await _async_import_config(
            hass,
            service_call({"path": "/no/such/file.yaml", "dry_run": False, "overwrite": False}),
        )

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "cannot_read_file"


def test_import_without_a_configured_entry_raises_cleanly(tmp_path, service_hass, service_call):
    path = _write(tmp_path, VALID_TEXT)
    hass = service_hass([])  # nothing set up yet

    async def run():
        return await _async_import_config(
            hass, service_call({"path": path, "dry_run": False, "overwrite": False})
        )

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "no_config_entry"


def test_import_reads_the_file_off_the_event_loop(
    tmp_path, service_entry, service_hass, service_call
):
    """Same guard as `test_init.py`'s `test_setup_reads_the_config_file_off_the_event_loop`,
    for the second `load_config_file` call site this task adds.
    """
    path = _write(tmp_path, VALID_TEXT)
    entry = service_entry()
    hass = service_hass([entry])
    read_thread_idents = []
    original_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):
        read_thread_idents.append(threading.get_ident())
        return original_read_text(self, *args, **kwargs)

    async def run():
        with mock.patch.object(Path, "read_text", spy_read_text):
            await _async_import_config(
                hass, service_call({"path": path, "dry_run": False, "overwrite": False})
            )

    caller_thread_ident = threading.get_ident()
    asyncio.run(run())

    assert read_thread_idents, "load_config_file never read the file at all"
    assert read_thread_idents[0] != caller_thread_ident


# --- export_config -------------------------------------------------------------


def _entry_with(config_text):
    """A `FakeServiceEntry` whose subentries reproduce `load_config(config_text)`."""
    config = load_config(config_text)
    items = subentries_from_config(config)
    subentries = {}
    for subentry_type, data in items:
        sub = ConfigSubentry(data=data, subentry_type=subentry_type, title="x", unique_id=None)
        subentries[sub.subentry_id] = sub
    return FakeServiceEntry(data={"guards": list(config.guards)}, subentries=subentries)


def test_export_writes_a_file_load_config_file_reads_back_to_an_equal_config(
    tmp_path, service_hass, service_call
):
    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])
    out = tmp_path / "out.yaml"

    async def run():
        return await _async_export_config(hass, service_call({"path": str(out)}))

    summary = asyncio.run(run())

    assert load_config_file(out) == config_from_subentries(entry)
    assert summary["blinds"] == 1


def test_export_refuses_to_write_through_a_symlink(tmp_path, service_hass, service_call):
    """The specific danger the task brief names: `/config/cover_logic.yaml` is a
    symlink to the migration gate's fixture on the real host. This test uses an
    unrelated symlink/target pair (no dependency on that specific path) to prove
    the guard is general.
    """
    real_target = tmp_path / "protected.yaml"
    real_target.write_text("untouched: true\n", encoding="utf-8")
    link = tmp_path / "cover_logic.yaml"
    link.symlink_to(real_target)

    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])

    async def run():
        return await _async_export_config(hass, service_call({"path": str(link)}))

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "export_path_is_symlink"
    assert real_target.read_text(encoding="utf-8") == "untouched: true\n"


def test_export_refuses_an_existing_target_that_has_comments(tmp_path, service_hass, service_call):
    """The comment-destruction guard: `dump_config` carries no comments at all
    (see `services.py`'s module docstring and `fixtures/dom_peter.yaml`'s own
    49), so overwriting a file that has any must be refused rather than
    silently discarding them.
    """
    out = tmp_path / "out.yaml"
    out.write_text("# do not lose me\nblinds: []\n", encoding="utf-8")

    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])

    async def run():
        return await _async_export_config(hass, service_call({"path": str(out)}))

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "export_target_has_comments"
    # Refused before the write -- the original content must survive untouched.
    assert out.read_text(encoding="utf-8") == "# do not lose me\nblinds: []\n"


def test_export_refuses_a_target_with_an_indented_comment_line(
    tmp_path, service_hass, service_call
):
    """A comment need not start in column 0 -- `fixtures/dom_peter.yaml` has
    several indented under a `rules:`/`zones:` block. The guard must not miss
    those by anchoring to the start of the line instead of the first
    non-whitespace character.
    """
    out = tmp_path / "out.yaml"
    out.write_text(
        "zones:\n  z:\n    # members explained here\n    members: []\n", encoding="utf-8"
    )

    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])

    async def run():
        return await _async_export_config(hass, service_call({"path": str(out)}))

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "export_target_has_comments"


def test_export_allows_overwriting_an_existing_target_with_no_comments(
    tmp_path, service_hass, service_call
):
    """The guard is about comments, not about "the file already exists" in
    general -- re-exporting the house's own configuration after editing it
    through the UI (`services.py`'s own "Path safety" docstring section)
    must keep working for a plain, comment-free file.
    """
    out = tmp_path / "out.yaml"
    out.write_text("blinds: []\n", encoding="utf-8")

    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])

    async def run():
        return await _async_export_config(hass, service_call({"path": str(out)}))

    asyncio.run(run())

    assert load_config_file(out) == config_from_subentries(entry)


def test_export_does_not_read_a_target_that_does_not_exist_yet(
    tmp_path, service_hass, service_call
):
    """A first export has nothing to lose -- the guard must not require the
    target to pre-exist, and must not itself raise `cannot_read_file` for the
    ordinary "new file" case.
    """
    out = tmp_path / "brand_new.yaml"
    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])

    async def run():
        return await _async_export_config(hass, service_call({"path": str(out)}))

    asyncio.run(run())

    assert load_config_file(out) == config_from_subentries(entry)


def test_export_refuses_a_directory_path(tmp_path, service_hass, service_call):
    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])

    async def run():
        return await _async_export_config(hass, service_call({"path": str(tmp_path)}))

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "export_path_is_directory"


def test_export_refuses_a_path_whose_parent_does_not_exist(tmp_path, service_hass, service_call):
    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])
    missing = tmp_path / "no" / "such" / "dir" / "out.yaml"

    async def run():
        return await _async_export_config(hass, service_call({"path": str(missing)}))

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "export_parent_missing"
    assert not missing.exists()


def test_export_refuses_an_entry_with_nothing_configured(
    tmp_path, service_entry, service_hass, service_call
):
    entry = service_entry()  # no subentries at all
    hass = service_hass([entry])
    out = tmp_path / "out.yaml"

    async def run():
        return await _async_export_config(hass, service_call({"path": str(out)}))

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "nothing_to_export"
    assert not out.exists()


def test_export_does_not_gate_on_validate_severity_errors(tmp_path, service_hass, service_call):
    """Unlike `import_config`, an `ERROR`-severity `validate()` problem does not
    block an export -- see `_async_export_config`'s own docstring for why.
    """
    entry = _entry_with(ERROR_TEXT)
    hass = service_hass([entry])
    out = tmp_path / "out.yaml"

    async def run():
        return await _async_export_config(hass, service_call({"path": str(out)}))

    asyncio.run(run())

    assert load_config_file(out) == config_from_subentries(entry)


def test_export_of_subentries_that_do_not_parse_raises_cleanly(
    service_entry, service_hass, service_call, tmp_path
):
    # A rule subentry missing `then` -- `config_from_subentries` raises `ConfigError`.
    broken = ConfigSubentry(
        data={"mode": "any", "zone": "z", "order": 0},
        subentry_type="rule",
        title="x",
        unique_id=None,
    )
    entry = service_entry(subentries={broken.subentry_id: broken})
    hass = service_hass([entry])
    out = tmp_path / "out.yaml"

    async def run():
        return await _async_export_config(hass, service_call({"path": str(out)}))

    with pytest.raises(ServiceValidationError) as excinfo:
        asyncio.run(run())

    assert excinfo.value.translation_key == "cannot_parse_subentries"
    assert not out.exists()


def test_export_writes_the_file_off_the_event_loop(tmp_path, service_hass, service_call):
    entry = _entry_with(VALID_TEXT)
    hass = service_hass([entry])
    out = tmp_path / "out.yaml"
    write_thread_idents = []
    original_write_text = Path.write_text

    def spy_write_text(self, *args, **kwargs):
        write_thread_idents.append(threading.get_ident())
        return original_write_text(self, *args, **kwargs)

    async def run():
        with mock.patch.object(Path, "write_text", spy_write_text):
            await _async_export_config(hass, service_call({"path": str(out)}))

    caller_thread_ident = threading.get_ident()
    asyncio.run(run())

    assert write_thread_idents, "dump_config_file never wrote the file at all"
    assert write_thread_idents[0] != caller_thread_ident


# --- registration --------------------------------------------------------------


def test_async_register_services_is_idempotent(service_hass):
    hass = service_hass([])
    async_register_services(hass)
    first = hass.services.registered_services()
    async_register_services(hass)  # a second config entry reload, say
    assert hass.services.registered_services() == first


def test_async_unregister_services_removes_both(service_hass):
    hass = service_hass([])
    async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_CONFIG)
    assert hass.services.has_service(DOMAIN, SERVICE_EXPORT_CONFIG)

    async_unregister_services(hass)

    assert not hass.services.has_service(DOMAIN, SERVICE_IMPORT_CONFIG)
    assert not hass.services.has_service(DOMAIN, SERVICE_EXPORT_CONFIG)


def test_registered_service_handlers_are_awaited_as_coroutines(hass_factory):
    """Regression test for the lambda-registration bug -- see this module's
    own docstring for the full story.

    Dispatches both services through a real `hass.services.async_call(...,
    blocking=True, return_response=True)`, the exact call shape that failed
    against the live house, on a real, minimal `HomeAssistant` (`hass_factory`)
    so Home Assistant's own `get_hassjob_callable_job_type` -- not a fake that
    never asks the question -- decides how each handler gets dispatched.

    `hass.config_entries` is swapped for `FakeServiceConfigEntries([])` (the
    same fake `_async_import_config`/`_async_export_config`'s "no entry yet"
    tests already use) purely so the awaited coroutine has something
    deterministic to fail on. That failure is the load-bearing assertion,
    not an afterthought:

    - Dispatched correctly (this fix), `_get_entry` runs *inside* the
      awaited coroutine and raises `ServiceValidationError(translation_key=
      "no_config_entry")` -- our own code, reached only because the handler
      was actually awaited.
    - Dispatched the broken way (a lambda), the handler is never awaited at
      all: `ServiceRegistry.async_call` gets the unawaited coroutine object
      back as "the response", and raises its own plain `HomeAssistantError`
      (`translation_key="service_reponse_invalid"`) one level up, before
      `_get_entry` ever runs. `HomeAssistantError` is `ServiceValidationError`'s
      *parent*, not a subclass of it, so `pytest.raises(ServiceValidationError)`
      does not swallow it -- the test errors out instead of merely failing an
      assertion, which is exactly what was seen reverting the fix locally
      (lambda back in -> this test fails; inner `async def` restored -> it
      passes again).
    """

    async def _run() -> None:
        hass = hass_factory()
        hass.config_entries = FakeServiceConfigEntries([])
        try:
            async_register_services(hass)
            for service in (SERVICE_IMPORT_CONFIG, SERVICE_EXPORT_CONFIG):
                with pytest.raises(ServiceValidationError) as excinfo:
                    await hass.services.async_call(
                        DOMAIN,
                        service,
                        {"path": "/nonexistent/does-not-matter.yaml"},
                        blocking=True,
                        return_response=True,
                    )
                assert excinfo.value.translation_key == "no_config_entry"
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())
