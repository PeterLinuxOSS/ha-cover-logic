"""Tests for `async_migrate_entry`: moving a version-1 (path, no subentries) entry
onto version-2 (subentries, no path).

Imports Home Assistant, so this module only collects under the Python 3.14
venv -- see `test_init.py`'s own note. Uses the same `make_entry`/`setup_hass`
fixtures `test_init.py` uses for `async_setup_entry`/`async_unload_entry`,
extended (see `tests/ha/conftest.py`) with `subentries`/`version` on
`FakeConfigEntry` and `async_add_subentry`/`async_update_entry` on
`FakeEntryConfigEntries` -- the two extra calls `async_migrate_entry` makes
that neither fake needed before this task.
"""

import asyncio
from unittest import mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import ConfigSubentry

from cover_logic import async_migrate_entry
from cover_logic.config_schema import load_config
from cover_logic.config_store import config_from_subentries, guards_to_data, subentries_from_config
from cover_logic.const import CONF_CONFIG_PATH, CONFIG_ENTRY_VERSION

# One blind, one zone, one fallback mode, an unconditional catch-all rule and
# one `guards` entry -- small, but touching every top-level `Config` field so
# a migration that dropped one of them (blinds imported but not guards, say)
# would be caught by the round-trip assertions below.
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
guards: [{policy: skip, applies_to: closing, targets: [cover.a]}]
"""


def _write(tmp_path, text):
    path = tmp_path / "cover_logic.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_migrate_imports_the_file_into_subentries(tmp_path, make_entry, setup_hass):
    path = _write(tmp_path, VALID_TEXT)
    entry = make_entry({CONF_CONFIG_PATH: path}, version=1)
    hass = setup_hass()

    assert asyncio.run(async_migrate_entry(hass, entry)) is True

    assert entry.version == CONFIG_ENTRY_VERSION
    assert CONF_CONFIG_PATH not in entry.data
    assert config_from_subentries(entry) == load_config(VALID_TEXT)


def test_migrate_never_touches_the_original_file(tmp_path, make_entry, setup_hass):
    """The file is the user's own backup -- migration only ever reads it."""
    path = _write(tmp_path, VALID_TEXT)
    entry = make_entry({CONF_CONFIG_PATH: path}, version=1)

    asyncio.run(async_migrate_entry(setup_hass(), entry))

    from pathlib import Path  # noqa: PLC0415

    assert Path(path).exists()
    assert Path(path).read_text(encoding="utf-8") == VALID_TEXT


def test_migrate_is_a_noop_when_already_at_the_current_version(tmp_path, make_entry, setup_hass):
    path = _write(tmp_path, VALID_TEXT)
    entry = make_entry({CONF_CONFIG_PATH: path}, version=CONFIG_ENTRY_VERSION)
    hass = setup_hass()

    assert asyncio.run(async_migrate_entry(hass, entry)) is True

    # Nothing imported -- the entry was already current, so `CONF_CONFIG_PATH`
    # (deliberately still present here, standing in for "whatever a version-2
    # entry happens to carry") is left completely alone.
    assert entry.subentries == {}
    assert entry.data == {CONF_CONFIG_PATH: path}


def test_migrate_twice_does_not_duplicate_subentries(tmp_path, make_entry, setup_hass):
    """Calling migration a second time -- e.g. Home Assistant re-running it after
    an unrelated retry -- must not double every blind, zone, mode and rule.
    """
    path = _write(tmp_path, VALID_TEXT)
    entry = make_entry({CONF_CONFIG_PATH: path}, version=1)
    hass = setup_hass()

    asyncio.run(async_migrate_entry(hass, entry))
    first_count = len(entry.subentries)

    assert asyncio.run(async_migrate_entry(hass, entry)) is True
    assert len(entry.subentries) == first_count
    assert config_from_subentries(entry) == load_config(VALID_TEXT)


def test_migrate_skips_import_when_subentries_already_exist_at_the_old_version(
    tmp_path, make_entry, setup_hass
):
    """A version-1 entry that already holds subentries -- a prior migration attempt
    interrupted after importing but before the version bump -- must not be
    imported into a second time; only the version catches up.
    """
    path = _write(tmp_path, VALID_TEXT)
    config = load_config(VALID_TEXT)
    existing = {
        f"sub{i}": ConfigSubentry(data=data, subentry_type=kind, title="", unique_id=None)
        for i, (kind, data) in enumerate(subentries_from_config(config))
    }
    # `guards` is set too, not just the subentries -- the realistic partial
    # state this guards against is a `import_config` run against this entry
    # (which writes both) before `async_migrate_entry` ever got to bump the
    # version, not a crash mid-way through this function's own single
    # `async_update_entry` call (which sets `guards` and `version` together;
    # see the module docstring).
    entry = make_entry(
        {CONF_CONFIG_PATH: path, "guards": guards_to_data(config)}, subentries=existing, version=1
    )
    hass = setup_hass()

    assert asyncio.run(async_migrate_entry(hass, entry)) is True

    assert entry.version == CONFIG_ENTRY_VERSION
    assert len(entry.subentries) == len(existing)
    assert config_from_subentries(entry) == config


def test_migrate_returns_false_and_leaves_version_untouched_on_a_missing_file(
    tmp_path, make_entry, setup_hass
):
    entry = make_entry({CONF_CONFIG_PATH: str(tmp_path / "does_not_exist.yaml")}, version=1)
    hass = setup_hass()

    assert asyncio.run(async_migrate_entry(hass, entry)) is False

    assert entry.version == 1
    assert entry.subentries == {}


def test_migrate_with_no_config_path_at_all_just_bumps_the_version(make_entry, setup_hass):
    """No plausible real entry, but nothing here assumes `CONF_CONFIG_PATH` exists --
    this must not crash, only bump the version with nothing to import.
    """
    entry = make_entry({}, version=1)
    hass = setup_hass()

    assert asyncio.run(async_migrate_entry(hass, entry)) is True

    assert entry.version == CONFIG_ENTRY_VERSION
    assert entry.subentries == {}


def test_migrate_reads_the_file_off_the_event_loop(tmp_path, make_entry, setup_hass):
    """Same reasoning as `test_init.test_setup_reads_the_config_file_off_the_event_loop`:
    `load_config_file` does a blocking read and must run through the executor.
    """
    from pathlib import Path  # noqa: PLC0415
    import threading  # noqa: PLC0415

    path = _write(tmp_path, VALID_TEXT)
    entry = make_entry({CONF_CONFIG_PATH: path}, version=1)
    hass = setup_hass()
    read_thread_idents = []
    original_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):
        read_thread_idents.append(threading.get_ident())
        return original_read_text(self, *args, **kwargs)

    async def run():
        with mock.patch.object(Path, "read_text", spy_read_text):
            await async_migrate_entry(hass, entry)

    caller_thread_ident = threading.get_ident()
    asyncio.run(run())

    assert read_thread_idents, "async_migrate_entry never read the file at all"
    assert read_thread_idents[0] != caller_thread_ident
