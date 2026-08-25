"""Guard: nothing under `custom_components/cover_logic/` may issue a cover command.

*** THIS FILE IS DELIBERATELY TEMPORARY. ***

Phase 2 wires this integration into a real, occupied house. Its entire
promise for that phase is that it *computes* decisions and *moves nothing* --
the coordinator subscribes to entities and derives a `Decision`, full stop.
Nothing here is allowed to call `hass.services.async_call(...)` (or any
equivalent, indexed-through-a-variable spelling of it) against any domain --
`cover` included, but really any domain, because a decision-only engine that
happens to also fire notifications or lights is just as much a broken
promise.

Phase 3 gives the integration hands: a service-calling executor is added on
purpose, and control finally does something. At that point THIS FILE IS
DELETED in one commit -- on purpose, not by accident, not by carving an
exception into it for the new code. A reader who finds this guard failing
after Phase 3 has landed should reach for `git rm tests/test_no_movement.py`,
not for a way to make the new, correct service call pass this check.

Do not mistake this for a permanent architectural rule about the codebase.
It encodes exactly one fact: "as of phase 2, this integration has no hands."
That fact has an expiry date.
"""

import ast
from pathlib import Path

import pytest

COVER_LOGIC_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "cover_logic"


def _python_files() -> list[Path]:
    return sorted(COVER_LOGIC_DIR.rglob("*.py"))


class _ServiceCallFinder(ast.NodeVisitor):
    """Finds every call shaped like `<anything>.services.async_call(...)`.

    Matching on the attribute names rather than the receiver is what catches
    all three shapes in the task brief with one rule: `hass.services.async_call`,
    `self.hass.services.async_call`, `hass.services.async_call(COVER_DOMAIN,
    SERVICE_CLOSE_COVER, ...)`, and `await hass.services.async_call(Platform.COVER,
    ...)` are all, syntactically, a `Call` whose `func` is `Attribute(attr=
    "async_call")` on something whose own `attr` is `"services"`. The domain
    and service name arguments (literal, constant-imported, or `Platform.X`)
    never have to be inspected at all -- any call through `.services.async_call`
    is a potential cover move and is caught regardless of how its arguments
    spell "cover".
    """

    def __init__(self) -> None:
        """Start with an empty list of offending call sites."""
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        """Record `node` if it matches the `.services.async_call(...)` shape."""
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "async_call"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "services"
        ):
            self.calls.append(node)
        self.generic_visit(node)


def test_expected_files_are_present() -> None:
    """A guard that silently scans zero files is not a guard."""
    files = _python_files()
    assert files, f"expected .py files under {COVER_LOGIC_DIR}, found none"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_module_issues_no_service_call(path: Path) -> None:
    """AST walk: no `<...>.services.async_call(...)` call site anywhere in `path`.

    A file this check cannot parse is a file it cannot vouch for, so a parse
    failure fails the test loudly rather than skipping the file.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as err:
        pytest.fail(f"{path}: could not be parsed by ast, cannot verify: {err}")
        return

    finder = _ServiceCallFinder()
    finder.visit(tree)
    offending_lines = sorted(node.lineno for node in finder.calls)
    assert not offending_lines, (
        f"{path} calls a Home Assistant service at line(s) {offending_lines} -- "
        "phase 2 must only compute decisions, never move a cover"
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_module_contains_no_async_call_substring(path: Path) -> None:
    """Grep-based backstop alongside the AST check above.

    Deliberately cruder than the AST walk: it fails on the bare substring
    `"async_call"` anywhere in the file, whether or not it parses as the
    `.services.async_call(...)` shape the AST check looks for (e.g. behind
    `getattr`, string-built, or otherwise obfuscated dispatch). Phase 2's
    modules have no legitimate reason to contain this substring at all --
    they are pure Python with no Home Assistant imports (see
    `tests/test_purity.py`) -- so any hit here is a guard doing its job, not
    a false positive to work around.
    """
    text = path.read_text(encoding="utf-8")
    assert "async_call" not in text, (
        f"{path} contains the substring 'async_call' -- phase 2 must only "
        "compute decisions, never move a cover"
    )
