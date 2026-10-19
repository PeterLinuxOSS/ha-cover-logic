"""The conformance check's dev-time twin: does the real config entry on this
host still match `fixtures/dom_peter.yaml`, once it has been migrated?

Reaches outside this repository into `/config/.storage/core.config_entries`
-- the real Home Assistant host's own storage, read-only -- the same
reasoning `tests/parity/jinja_bridge.py` gives for reaching into
`/config/tests/matica.py`: this repository happens to sit inside that host's
`/config`, so on this checkout specifically this test can measure the real
thing instead of a stand-in for it. Skipped on every other machine (that
storage file simply is not there), exactly like `tests/parity`'s other
module already is (see `MODELS.md` Sec. 5) -- and skipped, not failed, when
the real entry exists but has not been migrated to subentries yet: see
`test_live_subentries_match_the_fixture`'s own docstring for why "not
migrated" is not the same claim as "matches" or "does not match".

This is the loud, dev-time half of this task's conformance requirement;
`__init__._check_fixture_conformance` is the loud, runtime half (a repair
issue, raised on every setup of a subentry-backed entry) -- see that
function's own docstring for why both exist rather than just one. Both call
the same `conformance.diff_configs`, so "matches" can never mean something
different between the two.

Read-only, on purpose: this project's own constraints forbid writing
anything under `/config` outside this repository (and, separately,
triggering an actual reload of the live `cover_logic` entry, which is what
running its real migration would require) -- a conformance *check* has no
legitimate reason to write anywhere at all.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from cover_logic.config_schema import ConfigError, load_config_file
from cover_logic.config_store import config_from_subentries
from cover_logic.conformance import diff_configs

_STORAGE = Path("/config/.storage/core.config_entries")


def available() -> bool:
    """Whether this host's own config-entry storage can be read at all."""
    return _STORAGE.is_file()


pytestmark = pytest.mark.skipif(
    not available(), reason="needs the real Home Assistant host's own .storage/core.config_entries"
)


class _StubSubentry:
    """Duck-typed stand-in `config_from_subentries` reads -- see that
    function's module docstring ("Duck-typed on purpose") for why this needs
    nothing beyond `.subentry_type`/`.data`.
    """

    __slots__ = ("data", "subentry_type")

    def __init__(self, subentry_type: str, data: dict[str, Any]) -> None:
        self.subentry_type = subentry_type
        self.data = data


class _StubEntry:
    """Duck-typed stand-in for the one config entry `config_from_subentries` reads."""

    __slots__ = ("data", "subentries")

    def __init__(self, data: dict[str, Any], subentries: dict[str, _StubSubentry]) -> None:
        self.data = data
        self.subentries = subentries


def _load_live_entry() -> _StubEntry | None:
    """Read the real `cover_logic` config entry straight off this host's own storage.

    Parses the exact on-disk shape Home Assistant's own `ConfigEntries`
    manager writes (`entry["subentries"]` is a list of
    `{"subentry_id", "subentry_type", "data", ...}` dicts, not yet the
    `{id: subentry}` mapping `config_from_subentries` wants) rather than
    spinning up any part of Home Assistant itself to read it back -- the same
    tradeoff `jinja_bridge.py` makes by importing `matica.py` directly rather
    than running the real Jinja template through a live `hass`.
    """
    raw = json.loads(_STORAGE.read_text(encoding="utf-8"))
    for entry in raw["data"]["entries"]:
        if entry.get("domain") != "cover_logic":
            continue
        subentries = {
            sub["subentry_id"]: _StubSubentry(sub["subentry_type"], sub["data"])
            for sub in entry.get("subentries", [])
        }
        return _StubEntry(entry.get("data") or {}, subentries)
    return None


def test_live_subentries_match_the_fixture(fixtures_dir):
    """THE conformance gate for a subentry-backed installation of this integration.

    Skips (does not fail) in two cases that are not "matches" or "does not
    match" at all: no `cover_logic` entry on this host, or one that exists
    but has not been migrated to subentries yet (`entry.subentries` empty --
    the shape this project's real entry is still in as of this task, since
    triggering the real `async_migrate_entry` would mean reloading the live
    entry, exactly the "touch /config outside this repo" this task's own
    constraints forbid). Once a real reload has migrated it, this starts
    actually comparing instead of skipping -- see the task report for the
    load-bearing proof this test's *logic* got instead, via
    `tests/ha/test_init.py`'s fixture-conformance tests against a
    hand-built, migrated-shape entry.
    """
    entry = _load_live_entry()
    if entry is None:
        pytest.skip("no cover_logic config entry on this host")
    if not entry.subentries:
        pytest.skip("the live cover_logic entry has not been migrated to subentries yet")

    try:
        live = config_from_subentries(entry)
    except ConfigError as err:
        pytest.fail(f"the live entry's subentries do not even parse: {err}")
        return

    fixture = load_config_file(fixtures_dir / "dom_peter.yaml")
    diff = diff_configs(live, fixture)
    assert diff == [], f"live cover_logic subentries differ from fixtures/dom_peter.yaml in: {diff}"
