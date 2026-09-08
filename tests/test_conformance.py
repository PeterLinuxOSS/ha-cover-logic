"""`conformance.diff_configs`/`repo_fixture_path` -- pure, no `homeassistant` import.

These are the two building blocks `__init__._check_fixture_conformance` (the
runtime repair-issue check) and `tests/parity/test_subentry_conformance.py`
(the dev-time test against this host's real config-entry storage) both use;
see `conformance.py`'s own module docstring for why the actual comparison
lives here instead of being duplicated in each caller.
"""

from pathlib import Path

from cover_logic import conformance
from cover_logic.config_schema import load_config
from cover_logic.conformance import diff_configs, repo_fixture_path

# One blind, one zone, one fallback mode, one rule, matching every other
# small fixture text in this test suite (e.g. `tests/ha/conftest.py`'s
# `CONFIG_TEXT`) -- these tests are about `diff_configs` itself, not about
# exercising `config_schema`'s parser (already covered elsewhere).
BASE_TEXT = """
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


def test_diff_configs_is_empty_for_two_configs_built_from_the_same_text():
    a = load_config(BASE_TEXT)
    b = load_config(BASE_TEXT)
    assert a is not b  # two independent parses, not the same object
    assert diff_configs(a, b) == []


def test_diff_configs_is_empty_regardless_of_yaml_key_order_quoting_or_comments():
    """The whole point: meaning, not text. A second document that differs only
    in comments, key order and quoting style must still compare equal.
    """
    reordered = """
# a comment the first document does not have
modes:
  - id: "any"
rules:
  "any.z":
    - then: {tilt: keep, position: keep}
zones:
  z:
    members:
      - "cover.a"
blinds:
  - entity: "cover.a"
"""
    a = load_config(BASE_TEXT)
    b = load_config(reordered)
    assert diff_configs(a, b) == []


def test_diff_configs_names_the_field_that_differs():
    a = load_config(BASE_TEXT)
    b = load_config(BASE_TEXT.replace("cover.a", "cover.b"))
    # Renaming the one blind changes both `blinds` (its key) and `zones`
    # (its `members`) and `rules` is untouched -- assert the exact set, not
    # just "non-empty", so a change that narrows or widens what this reports
    # is caught here rather than downstream.
    assert set(diff_configs(a, b)) == {"blinds", "zones"}


def test_diff_configs_catches_a_guards_only_difference():
    a = load_config(BASE_TEXT)
    b = load_config(BASE_TEXT + "guards: [{policy: skip, applies_to: closing}]\n")
    assert diff_configs(a, b) == ["guards"]


def test_repo_fixture_path_finds_the_real_fixture_on_this_checkout():
    """This test suite runs from inside the actual project checkout, where
    `fixtures/dom_peter.yaml` genuinely sits two directories above this
    package -- see `conformance.py`'s own docstring for why that is not true
    of every installation of this integration.
    """
    path = repo_fixture_path()
    assert path is not None
    assert path.name == "dom_peter.yaml"
    assert path.is_file()


def test_repo_fixture_path_is_none_when_the_fixture_is_not_there(monkeypatch):
    """The only way to observe the "installed elsewhere" case from this checkout:
    point the module's own constant at a path that does not exist and confirm
    the function reports `None`, never raises.
    """
    monkeypatch.setattr(conformance, "_REPO_FIXTURE", Path("/nonexistent/dom_peter.yaml"))
    assert repo_fixture_path() is None
