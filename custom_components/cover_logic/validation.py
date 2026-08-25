"""Static checks over a configuration.

Answers the questions the old Jinja matrix could not be asked: is every blind
owned, is every rule reachable, does every (mode, zone) pair actually decide
anything. Runs in the test suite and again on import in the UI.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .const import COND_EVENT_TARGETS_ZONE, COND_REF, COND_SUN_HITS_TARGET
from .model import Config

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Problem:
    severity: str
    code: str
    message: str


def validate(config: Config) -> list[Problem]:
    problems: list[Problem] = []
    problems += _check_ownership(config)
    problems += _check_modes(config)
    problems += _check_rule_keys(config)
    problems += _check_rule_lists(config)
    problems += _check_circular_condition_refs(config)
    problems += _check_unknown_condition_refs(config)
    problems += _check_condition_shapes(config)
    return problems


def _check_ownership(config: Config) -> list[Problem]:
    out: list[Problem] = []
    owner: dict[str, str] = {}

    for zone_id, zone in config.zones.items():
        for entity in zone.members:
            if entity not in config.blinds:
                out.append(Problem(ERROR, "zone_member_unknown",
                                   f"zone {zone_id!r} refers to unknown blind {entity!r}"))
            if entity in owner:
                out.append(Problem(
                    ERROR, "blind_in_two_zones",
                    f"blind {entity!r} is owned by {owner[entity]!r} and {zone_id!r}",
                ))
            else:
                owner[entity] = zone_id

    out.extend(
        Problem(ERROR, "blind_without_zone",
                f"blind {entity!r} belongs to no zone, so no rule decides it")
        for entity in config.blinds
        if entity not in owner
    )
    return out


def _check_modes(config: Config) -> list[Problem]:
    out: list[Problem] = []
    fallbacks = [i for i, m in enumerate(config.modes) if m.when is None]
    if not fallbacks:
        out.append(Problem(ERROR, "no_fallback_mode",
                           "no mode without a condition; some states would resolve to no mode"))
        return out
    first = fallbacks[0]
    if first != len(config.modes) - 1:
        dead = ", ".join(m.id for m in config.modes[first + 1:])
        out.append(Problem(ERROR, "fallback_mode_not_last",
                           f"mode {config.modes[first].id!r} has no condition but is not last; "
                           f"these can never match: {dead}"))
    return out


def _check_rule_keys(config: Config) -> list[Problem]:
    out: list[Problem] = []
    mode_ids = {m.id for m in config.modes}
    for key in config.rules:
        mode, _, zone = key.partition(".")
        if mode not in mode_ids or zone not in config.zones:
            out.append(Problem(ERROR, "unknown_rule_key",
                               f"rule key {key!r} names an unknown mode or zone"))
    return out


def _check_rule_lists(config: Config) -> list[Problem]:
    out: list[Problem] = []
    for mode in config.modes:
        for zone_id in config.zones:
            key = f"{mode.id}.{zone_id}"
            rules = config.rules.get(key)
            if not rules:
                out.append(Problem(WARNING, "missing_rule_list",
                                   f"{key} has no rules; every blind there keeps its position"))
                continue
            out += _check_reachability(key, rules)
    return out


def _check_reachability(key: str, rules) -> list[Problem]:
    """A rule with no `if` swallows everything after it in the same event scope."""
    out: list[Problem] = []
    catch_all_scopes: list[frozenset | None] = []

    for index, rule in enumerate(rules):
        for scope in catch_all_scopes:
            if scope is None or (rule.events is not None and rule.events <= scope):
                out.append(Problem(WARNING, "unreachable_rule",
                                   f"{key}#{index} can never fire; an earlier rule "
                                   f"with no condition already matches everything"))
                break
        if rule.when is None:
            catch_all_scopes.append(rule.events)

    if not any(r.when is None and r.events is None for r in rules):
        out.append(Problem(WARNING, "no_catch_all",
                           f"{key} has no final rule without a condition; "
                           f"some states fall through to keep/keep silently"))
    return out


def _check_circular_condition_refs(config: Config) -> list[Problem]:
    """Detect cycles in condition references.

    A cycle is when a condition refers to itself directly or through a chain
    of references. For example: A -> B -> A is a cycle.
    """
    out: list[Problem] = []
    cycles_reported: set[frozenset[str]] = set()

    for cond_name in config.conditions:
        cycle = _find_cycle_from(cond_name, config.conditions)
        if cycle is not None:
            # Normalize cycle to avoid reporting the same cycle multiple times
            # (a cycle found starting from different members is the same set
            # of names, just rotated to a different starting point).
            cycle_set = frozenset(cycle)
            if cycle_set not in cycles_reported:
                cycles_reported.add(cycle_set)
                # `cycle` is already in real traversal order (the order the
                # DFS actually followed the references) -- report it as-is,
                # not re-sorted, so the message names an edge that exists.
                loop = f"{' -> '.join(cycle)} -> {cycle[0]}"
                out.append(Problem(
                    ERROR, "circular_condition_ref",
                    f"circular condition reference: {loop}",
                ))

    return out


def _find_cycle_from(start_name: str, registry: dict[str, dict]) -> list[str] | None:
    """Find a cycle starting from start_name using an iterative DFS.

    Returns a list of condition names forming a cycle in the actual order the
    references were followed, or None if no cycle exists. Iterative (an
    explicit stack) rather than recursive so that a long-but-legal reference
    chain is bounded by heap, not by the interpreter's frame limit -- a config
    with a few thousand chained conditions must still validate, not crash.

    `visited` marks every node whose references have been (or are being)
    explored; `on_path` is the subset of `visited` currently on the DFS path
    from `start_name`. A reference into a visited-but-not-on-path node is a
    legal cross-edge (e.g. the shared bottom of a diamond); a reference into
    a node that is on the current path is a back-edge, i.e. a cycle.
    """
    visited: set[str] = set()
    on_path: set[str] = set()
    path: list[str] = []
    # Each stack frame pairs a node with an iterator over its outgoing
    # references, so resuming a frame after a child is fully explored is a
    # plain next() on that same iterator rather than a recursive call.
    stack: list[tuple[str, Iterator[str]]] = []

    def enter(node: str) -> None:
        visited.add(node)
        on_path.add(node)
        path.append(node)
        stack.append((node, iter(_get_referenced_conditions(node, registry))))

    enter(start_name)
    while stack:
        node, refs = stack[-1]
        ref_name = next(refs, None)
        if ref_name is None:
            # No more outgoing references from this node -- backtrack.
            stack.pop()
            path.pop()
            on_path.discard(node)
            continue
        if ref_name not in visited:
            enter(ref_name)
        elif ref_name in on_path:
            cycle_start_idx = path.index(ref_name)
            return path[cycle_start_idx:]
        # else: already fully explored via another branch -- a legal
        # cross-edge, not a cycle.

    return None


def _walk_condition_nodes(node) -> Iterator[dict]:
    """Yield every condition dict reachable from `node`.

    `node` is a condition body (a dict), a bare list of conditions (this
    dialect's list-as-AND shorthand), or None. Recurses into the
    `conditions:` list of an `and`/`or`/`not` combinator. This is the single
    traversal behind every check that needs to visit each condition dict in
    a config once -- the circular-ref check, the unknown-ref check, and the
    condition-shape check (issue #3) all read from it instead of each
    re-walking the tree its own way.
    """
    if isinstance(node, dict):
        yield node
        for sub_cond in node.get("conditions", []):
            yield from _walk_condition_nodes(sub_cond)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_condition_nodes(item)


def _referenced_condition_names(node) -> set[str]:
    """Return every condition name a `{condition: ref, name: ...}` refers to,
    anywhere within `node` (a condition body, a bare list of conditions, or
    None). Built on `_walk_condition_nodes` -- the single walker behind both
    the circular-ref and the unknown-ref checks.
    """
    return {
        n.get("name", "")
        for n in _walk_condition_nodes(node)
        if n.get("condition") == COND_REF
    }


def _condition_sites(config: Config) -> Iterator[tuple[dict | list | None, str]]:
    """Every top-level condition slot in the config, paired with a label
    identifying where it lives (for problem messages). Shared by the
    unknown-ref check and the condition-shape check so both name locations
    the same way and neither re-derives this list of slots on its own.
    """
    for cond_name, body in config.conditions.items():
        yield body, f"condition {cond_name!r}"
    for mode in config.modes:
        yield mode.when, f"mode {mode.id!r}"
    for key, rules in config.rules.items():
        for index, rule in enumerate(rules):
            yield rule.when, f"rule {key}#{index}"


def _get_referenced_conditions(cond_name: str, registry: dict[str, dict]) -> set[str]:
    """Return the set of condition names directly referenced by cond_name."""
    if cond_name not in registry:
        return set()
    return _referenced_condition_names(registry[cond_name])


def _check_unknown_condition_refs(config: Config) -> list[Problem]:
    """Every `{condition: ref, name: N}` must name a condition that exists.

    The YAML parser only catches this for refs written with the `!ref` tag.
    A hand-written literal `{condition: ref, name: ...}` dict passes
    `load_config` unchecked -- condition bodies are deliberately exempt from
    strict key checking -- and would otherwise surface as a bare unhandled
    `KeyError` deep inside conditions.py at evaluation time instead of a
    validation report.
    """
    out: list[Problem] = []
    for node, where in _condition_sites(config):
        if node is None:
            continue
        out.extend(
            Problem(ERROR, "unknown_condition_ref",
                    f"{where} refers to unknown condition {name!r}")
            for name in sorted(_referenced_condition_names(node))
            if name not in config.conditions
        )
    return out


# Required keys per condition type. `time`, `numeric_state` and
# `sun_hits_target`/`event_targets_zone` need extra "at least one of" or
# "none required" handling beyond a flat required-set, so they are handled
# separately in `_check_condition_shape` -- this only covers the flat case.
_REQUIRED_CONDITION_KEYS: dict[str, tuple[str, ...]] = {
    "state": ("entity_id", "state"),
    "numeric_state": ("entity_id", "default"),
    "time": (),
    "template": ("value_template",),
    COND_REF: ("name",),
    "and": ("conditions",),
    "or": ("conditions",),
    "not": ("conditions",),
    COND_SUN_HITS_TARGET: (),
    COND_EVENT_TARGETS_ZONE: (),
}


def _check_condition_shape(node: dict, where: str) -> list[Problem]:
    """Check one condition dict's own shape (not its children -- the caller
    walks those separately). Only unknown types and missing *required* keys
    are reported; extra keys this dialect does not know about (`alias`,
    `enabled`, whatever Home Assistant adds next) are deliberately ignored,
    since condition bodies are native Home Assistant dicts this project does
    not own the full schema of.
    """
    kind = node.get("condition")
    if kind not in _REQUIRED_CONDITION_KEYS:
        return [Problem(ERROR, "bad_condition_shape",
                        f"{where}: unknown condition type {kind!r}")]

    out: list[Problem] = []
    missing = [key for key in _REQUIRED_CONDITION_KEYS[kind] if key not in node]
    if missing:
        out.append(Problem(ERROR, "bad_condition_shape",
                           f"{where}: condition {kind!r} is missing required "
                           f"key(s) {missing}"))
    if kind == "numeric_state" and "above" not in node and "below" not in node:
        out.append(Problem(ERROR, "bad_condition_shape",
                           f"{where}: condition {kind!r} needs at least one "
                           f"of 'above'/'below'"))
    if kind == "time" and "after" not in node and "before" not in node:
        out.append(Problem(ERROR, "bad_condition_shape",
                           f"{where}: condition {kind!r} needs at least one "
                           f"of 'after'/'before'"))
    return out


def _check_condition_shapes(config: Config) -> list[Problem]:
    """Check every condition body's shape: known type, required keys present.

    `config_schema._check_keys` deliberately exempts condition bodies from
    strict key checking (they are native Home Assistant condition dicts, a
    schema this project does not own), and `_check_unknown_condition_refs`
    only checks ref *names*. Nothing else validates a condition body's shape
    -- so an unknown `condition:` value or a missing required key currently
    passes `validate()` clean and only surfaces as a bare `ValueError` or
    `KeyError` deep inside `conditions.py` at evaluation time, far from the
    config that caused it.
    """
    out: list[Problem] = []
    for node, where in _condition_sites(config):
        if node is None:
            continue
        for n in _walk_condition_nodes(node):
            out += _check_condition_shape(n, where)
    return out
