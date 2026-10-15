"""Every flow screen and repair issue must supply `{docs_url}`; HA would render it literally."""

import ast
from pathlib import Path

from cover_logic.const import DOCS_URL
from cover_logic.docs_link import docs_placeholders

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "cover_logic"
_FLOW_MODULES = ("config_flow.py", "options_flow.py", "subentry_flow.py")


def test_docs_placeholders_merges_extra():
    assert docs_placeholders({"blind": "cover.x"}) == {"docs_url": DOCS_URL, "blind": "cover.x"}
    assert docs_placeholders() == {"docs_url": DOCS_URL}


def _calls(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in names:
                yield node


def _passes_docs(call, keyword):
    for kw in call.keywords:
        if kw.arg == keyword:
            v = kw.value
            return isinstance(v, ast.Call) and getattr(v.func, "id", None) == "docs_placeholders"
    return False


def test_every_flow_screen_supplies_docs_url():
    missing = [
        f"{mod}:{call.lineno}"
        for mod in _FLOW_MODULES
        for call in _calls(_COMPONENT / mod, {"async_show_form", "async_show_menu"})
        if not _passes_docs(call, "description_placeholders")
    ]
    assert not missing, missing


def test_every_repair_issue_supplies_docs_url():
    calls = list(_calls(_COMPONENT / "__init__.py", {"async_create_issue"}))
    assert len(calls) == 4
    missing = [c.lineno for c in calls if not _passes_docs(c, "translation_placeholders")]
    assert not missing, missing


def test_manifest_documentation_is_the_site():
    import json

    manifest = json.loads((_COMPONENT / "manifest.json").read_text())
    assert manifest["documentation"] == DOCS_URL
