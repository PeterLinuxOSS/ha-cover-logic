"""Field help is one sentence; longer explanations live on the site, linked from each screen."""

import json
from pathlib import Path
import re

import pytest

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "cover_logic"
_SITE = Path(__file__).parent.parent / "docs" / "site"
_FILES = [_COMPONENT / "strings.json", *sorted((_COMPONENT / "translations").glob("*.json"))]
_LINK = re.compile(r"\[[^\]]+\]\(\{docs_url\}/([^)#]*?)/?(?:#[\w-]+)?\)")


def _walk(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, (*path, k))
    elif isinstance(node, str):
        yield path, node


def _strings(p):
    return list(_walk(json.loads(p.read_text(encoding="utf-8"))))


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_field_help_is_short(path):
    long = [(p, len(s)) for p, s in _strings(path) if "data_description" in p and len(s) > 150]
    assert not long, long


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_descriptions_are_short(path):
    long = [
        (p, len(_LINK.sub("", s).strip()))
        for p, s in _strings(path)
        if p[-1] == "description" and len(_LINK.sub("", s).strip()) > 300
    ]
    assert not long, long


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_every_learn_more_link_points_at_a_page(path):
    broken = [
        (p, m)
        for p, s in _strings(path)
        for m in _LINK.findall(s)
        if not ((_SITE / f"{m}.md").exists() or (_SITE / m / "index.md").exists())
    ]
    assert not broken, broken


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_every_docs_url_placeholder_is_inside_a_matched_link(path):
    """A malformed Learn-more link (e.g. a stray `/` before the anchor) does not match `_LINK`
    at all, so the test above silently finds nothing to check -- count the raw placeholder
    occurrences against the regex's own match count to catch that instead.
    """
    mismatched = [
        (p, s) for p, s in _strings(path) if s.count("{docs_url}") != len(_LINK.findall(s))
    ]
    assert not mismatched, mismatched


def test_every_issue_and_subentry_step_links_to_the_site():
    strings = json.loads((_COMPONENT / "strings.json").read_text(encoding="utf-8"))
    missing = [k for k, v in strings["issues"].items() if "{docs_url}" not in v["description"]]
    for kind, body in strings["config_subentries"].items():
        for step_id, step in body.get("step", {}).items():
            if "description" in step and "{docs_url}" not in step["description"]:
                missing.append(f"{kind}.{step_id}")
    assert not missing, missing


def test_en_is_strings():
    en = (_COMPONENT / "translations/en.json").read_bytes()
    assert en == (_COMPONENT / "strings.json").read_bytes()
