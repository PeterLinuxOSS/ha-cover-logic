"""`strings.json` and every `translations/*.json` must carry the same keys.

A missing key does not fail anything at runtime -- Home Assistant simply
renders the raw identifier (`numeric_state_default`) where a label should be,
in the one language that is missing it. Nothing in the test suite, the config
flow or `hassfest` would notice, so this has been checked by hand once per
task; two tasks in, that is a habit worth replacing with a test.

Pure on purpose: this only reads JSON (and, below, parses `.py`/`.yaml` files
as *text* -- `ast.parse`, not `import`) off disk, so it runs in the fast suite
too rather than needing `tests/ha/`. The step- and field-level checks for the
six *subentry* flows -- which do need a running flow to render a real
`vol.Schema` built from Home Assistant selectors -- live in
`tests/ha/test_subentry_flows.py`, next to the flows themselves
(`test_every_subentry_type_has_its_own_strings`,
`test_every_step_a_flow_can_dispatch_has_a_title`,
`test_every_field_of_every_rendered_form_has_a_label`); this module does not
re-derive those, only the top-level flow's one field-plain step and every
other surface a raw string identifier can leak through: services, service
fields, exceptions, and repair issues.

**What this module proves, and what it cannot.** Every function below reads
its *source of truth* off disk -- `services.yaml` for service/field names,
`services.py`/`__init__.py` parsed with `ast` for the literal
`translation_key=`/`errors["base"] = ` strings those modules actually use --
and asserts each key it finds is declared in `strings.json` (transitively, in
every translation file too, via the whole-file key-equality tests above).
None of it imports `homeassistant` or the package itself, so a key that is
only ever produced by *running* code this cannot see (a dynamically computed
`translation_key`, for instance) would not be caught -- every such key in
this codebase today is a plain string literal or a module-level constant
resolved back to one (`_FIXTURE_DRIFT_ISSUE`), and each helper raises loudly,
rather than silently skipping, the moment it meets one it cannot resolve
statically, so a future dynamic key does not quietly stop being checked.
"""

import ast
import json
from pathlib import Path

import pytest
import yaml

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


# ---------------------------------------------------------------------------
# Derived checks: services, service fields, exceptions, repair issues.
#
# Everything below parses `.py` files as text (`ast.parse`) rather than
# importing them, and `.yaml` files with `yaml.safe_load` -- neither needs
# `homeassistant` installed, which is what keeps this module collectible
# under the system-Python 3.12 suite (see the module docstring, §8 of
# `MODELS.md`).
# ---------------------------------------------------------------------------

_SERVICES_YAML = _COMPONENT / "services.yaml"
_SERVICES_PY = _COMPONENT / "services.py"
_INIT_PY = _COMPONENT / "__init__.py"
_CONFIG_FLOW_PY = _COMPONENT / "config_flow.py"
_CONST_PY = _COMPONENT / "const.py"


def _ast_tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_level_string_constants(tree):
    """`NAME = "literal"` assignments at module scope, as a `{name: value}` dict.

    Used to resolve a `translation_key=SOME_NAME` or `vol.Required(SOME_NAME)`
    call argument that is a bare identifier rather than a string literal --
    `__init__.py`'s `_FIXTURE_DRIFT_ISSUE` and `config_flow.py`'s
    `CONF_CONFIG_PATH` (imported from `const.py`) are exactly this shape.
    """
    constants = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.targets[0].id] = node.value.value
    return constants


def _resolve_str(node, constants, *, context):
    """A string literal, or a bare name resolved via `constants` -- else a loud failure.

    The loud failure is the point: a future `translation_key` or schema field
    name computed some other way (an f-string, a function call, ...) must not
    be silently skipped by this derivation -- see the module docstring's
    "what this module cannot" paragraph.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    pytest.fail(f"{context}: not a statically resolvable string literal ({ast.dump(node)})")


def _translation_keys_from_calls(tree, constants, func_names):
    """Every `translation_key=` value passed to a call whose function name is in `func_names`.

    Matches by the called function's own name (`node.func.id` for a bare
    name, `node.func.attr` for an attribute access like `ir.async_create_issue`)
    -- the exact two shapes `services.py` (`ServiceValidationError(...)`,
    `HomeAssistantError(...)`) and `__init__.py` (`ir.async_create_issue(...)`)
    use.
    """
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in func_names:
            continue
        for keyword in node.keywords:
            if keyword.arg == "translation_key":
                keys.add(
                    _resolve_str(keyword.value, constants, context=f"{name}(translation_key=...)")
                )
    return keys


def test_every_exception_translation_key_services_py_raises_is_declared():
    """`services.py` is the only module that raises a translated
    `ServiceValidationError`/`HomeAssistantError` (see `grep -n
    translation_key custom_components/cover_logic/*.py`, checked when this
    test was written) -- every `translation_key=` it passes to either
    constructor must have a matching `strings.json["exceptions"]` entry, or
    the user sees the raw key instead of a message.
    """
    tree = _ast_tree(_SERVICES_PY)
    constants = _module_level_string_constants(tree)
    used = _translation_keys_from_calls(
        tree, constants, {"ServiceValidationError", "HomeAssistantError"}
    )
    declared = set(_load(_STRINGS)["exceptions"])

    assert used, "derivation found no translation_key at all -- the AST walk itself is broken"
    missing = used - declared
    assert missing == set(), f"services.py raises undeclared exception key(s): {sorted(missing)}"


def test_every_issue_translation_key_init_py_creates_is_declared():
    """`__init__.py`'s `ir.async_create_issue` call is the only repair issue
    this integration raises today; its `translation_key` must have a matching
    `strings.json["issues"]` entry.
    """
    tree = _ast_tree(_INIT_PY)
    constants = _module_level_string_constants(tree)
    used = _translation_keys_from_calls(tree, constants, {"async_create_issue"})
    declared = set(_load(_STRINGS)["issues"])

    assert used, "derivation found no translation_key at all -- the AST walk itself is broken"
    missing = used - declared
    assert missing == set(), f"__init__.py creates undeclared issue key(s): {sorted(missing)}"


def test_every_service_and_field_in_services_yaml_has_strings():
    """`services.yaml` is what actually registers each service's fields with
    Home Assistant (`hassfest`/the services UI reads it, not `strings.json`);
    `strings.json["services"]` is only the label/description layer on top. A
    field present in one and not the other is either a silent unlabelled
    field or a label for a field nobody can fill in any more.
    """
    declared = yaml.safe_load(_SERVICES_YAML.read_text(encoding="utf-8"))
    services = _load(_STRINGS)["services"]

    assert set(declared) == set(services), (
        f"services.yaml declares {sorted(declared)}, strings.json declares {sorted(services)}"
    )
    for service_name, service_spec in declared.items():
        assert services[service_name]["name"]
        assert services[service_name]["description"]
        declared_fields = set(service_spec.get("fields", {}))
        described_fields = set(services[service_name].get("fields", {}))
        assert declared_fields == described_fields, (
            f"{service_name}: services.yaml fields {sorted(declared_fields)} != "
            f"strings.json fields {sorted(described_fields)}"
        )


def _class_node(tree, class_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    pytest.fail(f"no class named {class_name!r} found")


def _base_error_literals(class_node):
    """Every string literal assigned to `errors["base"]` anywhere in `class_node`.

    `config_flow.py` has exactly two places that assign `errors["base"]`: the
    top-level `CoverLogicConfigFlow.async_step_user` and the shared subentry
    `_SubentryFlowBase._step`. Restricting the walk to one class's own AST
    subtree (rather than the whole module) is what keeps this test about the
    top-level flow specifically -- the subentry side is already covered,
    flow-rendered, by `tests/ha/test_subentry_flows.py`.
    """
    literals = set()
    for node in ast.walk(class_node):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Subscript):
            continue
        key_node = target.slice
        value_is_base = isinstance(key_node, ast.Constant) and key_node.value == "base"
        value_is_str = isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        if value_is_base and value_is_str:
            literals.add(node.value.value)
    return literals


def _vol_schema_field_names(class_node, constants):
    """Field names passed as the first argument to a `vol.Required`/`vol.Optional` call.

    Only within `class_node`'s own AST subtree.
    """
    names = set()
    for node in ast.walk(class_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("Required", "Optional")):
            continue
        if not node.args:
            continue
        names.add(_resolve_str(node.args[0], constants, context=f"vol.{func.attr}(...)"))
    return names


def test_top_level_config_flow_error_key_is_declared():
    """The one-field `user` step's `errors["base"]` literal(s) must each have a
    `strings.json["config"]["error"]` entry.
    """
    tree = _ast_tree(_CONFIG_FLOW_PY)
    class_node = _class_node(tree, "CoverLogicConfigFlow")
    used = _base_error_literals(class_node)
    declared = set(_load(_STRINGS)["config"]["error"])

    assert used, "derivation found no errors['base'] literal at all -- the AST walk is broken"
    missing = used - declared
    assert missing == set(), f"CoverLogicConfigFlow uses undeclared error key(s): {sorted(missing)}"


def test_top_level_config_flow_user_step_fields_are_declared():
    """Every field the `user` step's `vol.Schema` declares must have a
    `strings.json["config"]["step"]["user"]["data"]` label -- derived from the
    schema-building call itself (`vol.Required`/`vol.Optional`), not a
    hand-kept list, so a second field added to this step is caught the same
    way a renamed one would be.
    """
    flow_tree = _ast_tree(_CONFIG_FLOW_PY)
    const_tree = _ast_tree(_CONST_PY)
    constants = {
        **_module_level_string_constants(const_tree),
        **_module_level_string_constants(flow_tree),
    }
    class_node = _class_node(flow_tree, "CoverLogicConfigFlow")
    used = _vol_schema_field_names(class_node, constants)
    declared = set(_load(_STRINGS)["config"]["step"]["user"]["data"])

    assert used, (
        "derivation found no vol.Required/vol.Optional field at all -- the AST walk is broken"
    )
    missing = used - declared
    assert missing == set(), (
        f"CoverLogicConfigFlow 'user' step has undeclared field(s): {sorted(missing)}"
    )


def test_top_level_config_flow_abort_key_is_declared():
    """`already_configured` is not a string literal anywhere in this
    project's own source -- it is Home Assistant's own fixed abort reason for
    `ConfigFlow._abort_if_unique_id_configured()` (see
    `homeassistant/config_entries.py`), so it cannot be derived the way every
    other key in this module is. What *can* be derived and is checked here:
    that the call is still actually present in `CoverLogicConfigFlow` -- if a
    future change ever removed it, this project's own `strings.json` entry
    would become dead rather than this test silently keeping on checking a
    key nothing produces any more.
    """
    tree = _ast_tree(_CONFIG_FLOW_PY)
    class_node = _class_node(tree, "CoverLogicConfigFlow")
    calls_it = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_abort_if_unique_id_configured"
        for node in ast.walk(class_node)
    )
    assert calls_it, (
        "CoverLogicConfigFlow no longer calls _abort_if_unique_id_configured() -- "
        "if single-instance enforcement moved elsewhere, update this test's own "
        "premise, don't just delete it"
    )
    assert _load(_STRINGS)["config"]["abort"]["already_configured"]
