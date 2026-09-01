"""Compare a live `Config` against the fixture the migration gate measures, by meaning.

Phase 4 moves configuration out of `fixtures/dom_peter.yaml` -- read fresh
off disk on every `async_setup_entry` -- into config-entry subentries: a copy
that outlives any one setup and that a user can now edit through the UI
without ever touching the file again. That copy can drift from the fixture
the 92 160-scenario migration gate (`tests/parity/test_migration_gate.py`)
actually measures, silently: editing a subentry never touches
`fixtures/dom_peter.yaml`, and editing the fixture never touches a subentry.
`__init__.py`'s `_check_fixture_conformance` calls `diff_configs` on every
setup of a subentry-backed entry and turns a mismatch into a repair issue;
`tests/parity/test_subentry_conformance.py` calls it directly against the
real, on-disk config-entry storage, for the same reason
`tests/parity/test_migration_gate.py` reaches outside this repo into a
`matica.py` this repo does not ship: the guarantee is only worth anything
measured against the actual live state, not a copy of a copy.

No Home Assistant import: `diff_configs` takes and returns plain values, and
`repo_fixture_path` only ever touches `pathlib.Path` -- so both stay on the
pure side of the split `tests/test_purity.py` enforces, and both run under
system Python 3.12 with no `homeassistant` installed at all.

That purity is also why the `repo_*_path` functions below cannot hop to an
executor themselves: they stat the filesystem, which is blocking, but they
have no `hass` to hop with. An event-loop caller owns that hop -- see
`__init__._read_repo_fixture`.
"""

from pathlib import Path

from .model import Config

# Every top-level field of `Config` a subentry-built copy could diverge from
# the fixture on. Compared one at a time -- not "`live != reference`" as one
# boolean -- so a failure names *which* piece drifted: "rules" and "guards"
# point a reader somewhere very different to look.
_FIELDS = ("blinds", "zones", "modes", "rules", "conditions", "values", "guards")


def diff_configs(live: Config, reference: Config) -> list[str]:
    """Return the `Config` field names where `live` and `reference` disagree.

    Structural equality (the dataclass-derived `__eq__` on `Config` and
    everything it holds), never a text/YAML diff -- two YAML documents can
    differ in key order, quoting or comments and still parse to the exact
    same `Config`, and that must not be reported as drift. An empty result
    means the two are equal by meaning, field for field.
    """
    return [name for name in _FIELDS if getattr(live, name) != getattr(reference, name)]


# `custom_components/cover_logic/conformance.py` -> `parents[2]` is the repo
# root only when this file is loaded from inside a checkout of this project
# that also ships its own `fixtures/` directory next to `custom_components/`
# -- true on this project's own development host, where
# `/config/custom_components/cover_logic` is a symlink into
# `/config/dev/cover-logic/custom_components/cover_logic` and `Path.resolve()`
# follows that symlink before `parents[2]` is taken. An install anywhere else
# (HACS, a manual copy of just `custom_components/cover_logic`) has no such
# sibling directory at all, so this path simply does not exist there -- see
# `repo_fixture_path`.
_REPO_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "dom_peter.yaml"


def repo_fixture_path() -> Path | None:
    """The on-disk `fixtures/dom_peter.yaml` this checkout ships, or `None` elsewhere.

    `diff_configs` above is a general-purpose comparison a different house's
    own fixture (if it ever has one) could use instead; this function is the
    one house-specific (well, checkout-specific) piece of this module. `None`
    must be treated as "nothing to compare against", never as an error: an
    installation with no sibling `fixtures/` directory has no fixture to
    drift from, and this project's own "universal integration" goal
    (`MODELS.md` Sec. 1) means that has to stay a silent no-op, not a startup
    failure for every other house that ever installs this integration.

    Blocking (one stat): an event-loop caller must use an executor.
    """
    return _REPO_FIXTURE if _REPO_FIXTURE.is_file() else None


# Same reasoning as `_REPO_FIXTURE` above, one directory over: a checkout of
# this project ships `docs/example-config.yaml` (a worked example for a
# different, invented house -- see that file's own header and
# `tests/test_example_config.py`), but a HACS install or a manual copy of
# just `custom_components/cover_logic` has no sibling `docs/` at all.
_REPO_EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "docs" / "example-config.yaml"


def repo_example_config_path() -> Path | None:
    """The on-disk `docs/example-config.yaml` this checkout ships, or `None` elsewhere.

    `config_flow.CoverLogicConfigFlow.async_step_from_example` is the one
    reader of this: a setup step that lets a brand-new install start from a
    worked example instead of an empty configuration. `None` is not an error
    here either, for the same reason it is not one for `repo_fixture_path` --
    that step turns it into a plain "not available on this install" message
    rather than a crash, precisely because most installs are expected to hit
    it.

    Blocking (one stat): an event-loop caller must use an executor.
    """
    return _REPO_EXAMPLE_CONFIG if _REPO_EXAMPLE_CONFIG.is_file() else None
