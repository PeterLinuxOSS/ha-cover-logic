"""`strings.json` and every `translations/*.json` must carry the same keys.

A missing key does not fail anything at runtime -- Home Assistant simply
renders the raw identifier (`numeric_state_default`) where a label should be,
in the one language that is missing it. Nothing in the test suite, the config
flow or `hassfest` would notice, so this has been checked by hand once per
task; two tasks in, that is a habit worth replacing with a test.

Pure on purpose: this only reads JSON off disk, so it runs in the fast suite
too rather than needing `tests/ha/`. The step- and field-level checks that
*do* need to know what a flow renders live in
`tests/ha/test_subentry_flows.py`, next to the flows themselves.
"""

import json
from pathlib import Path

import pytest

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "cover_logic"
_STRINGS = _COMPONENT / "strings.json"
_TRANSLATIONS = sorted((_COMPONENT / "translations").glob("*.json"))


def _key_paths(node, prefix=()):
    """Every leaf path in a nested mapping, as tuples of keys."""
    if not isinstance(node, dict):
        return {prefix}
    return {path for key, value in node.items() for path in _key_paths(value, (*prefix, key))}


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_translations_directory_is_not_empty():
    """Guards the parametrisation below: an empty glob would make every
    per-language test vanish silently rather than fail.
    """
    assert [path.name for path in _TRANSLATIONS] == ["en.json", "sk.json"]


@pytest.mark.parametrize("path", _TRANSLATIONS, ids=lambda path: path.name)
def test_every_translation_has_exactly_the_keys_strings_json_declares(path):
    """Both directions matter. A key in `strings.json` and missing here shows
    the user a raw identifier; a key here and missing from `strings.json` is
    a label for a field that no longer exists, which is how a translation
    quietly rots after a field is renamed.
    """
    expected = _key_paths(_load(_STRINGS))
    actual = _key_paths(_load(path))

    assert sorted(expected - actual) == [], f"{path.name} is missing keys"
    assert sorted(actual - expected) == [], f"{path.name} has keys strings.json does not"


@pytest.mark.parametrize("path", [_STRINGS, *_TRANSLATIONS], ids=lambda path: path.name)
def test_no_translation_string_is_empty(path):
    """An empty string passes the key check above while rendering as a blank
    label -- indistinguishable in the UI from a field with no name at all.
    """
    doc = _load(path)
    blank = [
        "::".join(keys) for keys in sorted(_key_paths(doc)) if not str(_reach(doc, keys)).strip()
    ]
    assert blank == []


def _reach(doc, keys):
    for key in keys:
        doc = doc[key]
    return doc
