"""Static checks over a configuration.

Answers the questions the old Jinja matrix could not be asked: is every blind
owned, is every rule reachable, does every (mode, zone) pair actually decide
anything. Runs in the test suite and again on import in the UI.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from .const import COND_EVENT_TARGETS_ZONE, COND_REF, COND_SUN_HITS_TARGET
from .model import Config

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Problem:
    """One issue found in a configuration, at `ERROR` or `WARNING` severity."""

    severity: str
    code: str
    message: str


def validate(config: Config) -> list[Problem]:
    """Run every static check and return all problems found, if any."""
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
                out.append(
                    Problem(
                        ERROR,
                        "zone_member_unknown",
                        f"zone {zone_id!r} refers to unknown blind {entity!r}",
                    )
                )
            if entity in owner:
                out.append(
                    Problem(
                        ERROR,
                        "blind_in_two_zones",
                        f"blind {entity!r} is owned by {owner[entity]!r} and {zone_id!r}",
                    )
                )
            else:
                owner[entity] = zone_id

    out.extend(
        Problem(
            ERROR,
            "blind_without_zone",
            f"blind {entity!r} belongs to no zone, so no rule decides it",
        )
        for entity in config.blinds
        if entity not in owner
    )
    return out


def _check_modes(config: Config) -> list[Problem]:
    out: list[Problem] = []
    fallbacks = [i for i, m in enumerate(config.modes) if m.when is None]
    if not fallbacks:
        out.append(
            Problem(
                ERROR,
                "no_fallback_mode",
                "no mode without a condition; some states would resolve to no mode",
            )
        )
        return out
    first = fallbacks[0]
    if first != len(config.modes) - 1:
        dead = ", ".join(m.id for m in config.modes[first + 1 :])
        out.append(
            Problem(
                ERROR,
                "fallback_mode_not_last",
                f"mode {config.modes[first].id!r} has no condition but is not last; "
                f"these can never match: {dead}",
            )
        )
    return out


def _check_rule_keys(config: Config) -> list[Problem]:
    out: list[Problem] = []
    mode_ids = {m.id for m in config.modes}
    for key in config.rules:
        mode, _, zone = key.partition(".")
        if mode not in mode_ids or zone not in config.zones:
            out.append(
                Problem(
                    ERROR, "unknown_rule_key", f"rule key {key!r} names an unknown mode or zone"
                )
            )
    return out


def _check_rule_lists(config: Config) -> list[Problem]:
    out: list[Problem] = []
    for mode in config.modes:
        for zone_id in config.zones:
            key = f"{mode.id}.{zone_id}"
            rules = config.rules.get(key)
            if not rules:
                out.append(
                    Problem(
                        WARNING,
                        "missing_rule_list",
                        f"{key} has no rules; every blind there keeps its position",
                    )
                )
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
                out.append(
                    Problem(
                        WARNING,
                        "unreachable_rule",
                        f"{key}#{index} can never fire; an earlier rule "
                        f"with no condition already matches everything",
                    )
                )
                break
        if rule.when is None:
            catch_all_scopes.append(rule.events)

    if not any(r.when is None and r.events is None for r in rules):
        out.append(
            Problem(
                WARNING,
                "no_catch_all",
                f"{key} has no final rule without a condition; "
                f"some states fall through to keep/keep silently",
            )
        )
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
                out.append(
                    Problem(
                        ERROR,
                        "circular_condition_ref",
                        f"circular condition reference: {loop}",
                    )
                )

    return out


def _find_cycle_from(start_name: str, registry: dict[str, dict]) -> list[str] | None:
    """Find a cycle reachable from `start_name`, or return `None`.

    See docs/rationale.md -- "Why `_find_cycle_from` is iterative, not
    recursive".
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

    See docs/rationale.md -- "Why `_walk_condition_nodes` is the single
    traversal".
    """
    if isinstance(node, dict):
        yield node
        for sub_cond in node.get("conditions", []):
            yield from _walk_condition_nodes(sub_cond)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_condition_nodes(item)


def _referenced_condition_names(node) -> set[str]:
    """Return every condition name a `{condition: ref, name: ...}` in `node` refers to."""
    return {
        n.get("name", "") for n in _walk_condition_nodes(node) if n.get("condition") == COND_REF
    }


def _condition_sites(config: Config) -> Iterator[tuple[dict | list | None, str]]:
    """Yield every top-level condition slot in the config, with a label for problem messages."""
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

    See docs/rationale.md -- "Why `_check_unknown_condition_refs` exists
    despite YAML-time checking".
    """
    out: list[Problem] = []
    for node, where in _condition_sites(config):
        if node is None:
            continue
        out.extend(
            Problem(ERROR, "unknown_condition_ref", f"{where} refers to unknown condition {name!r}")
            for name in sorted(_referenced_condition_names(node))
            if name not in config.conditions
        )
    return out


# Required keys per condition type. `time`, `numeric_state` and
# `sun_hits_target`/`event_targets_zone` need extra "at least one of" or
# "none required" handling beyond a flat required-set, so they are handled
# separately in `_check_condition_shape` -- this only covers the flat case.
# Entries and each tuple's contents are alphabetised by condition/key name;
# lookup is always by key (`_REQUIRED_CONDITION_KEYS[kind]`), never by
# position or iteration order.
_REQUIRED_CONDITION_KEYS: dict[str, tuple[str, ...]] = {
    "and": ("conditions",),
    COND_EVENT_TARGETS_ZONE: (),
    "not": ("conditions",),
    "numeric_state": ("default", "entity_id"),
    "or": ("conditions",),
    COND_REF: ("name",),
    "state": ("entity_id", "state"),
    COND_SUN_HITS_TARGET: (),
    "template": ("value_template",),
    "time": (),
}


def _check_condition_shape(node: dict, where: str) -> list[Problem]:
    """Check one condition dict's own shape; the caller walks its children.

    See docs/rationale.md -- "Why `_check_condition_shape` only checks known
    types and required keys".
    """
    kind = node.get("condition")
    if kind not in _REQUIRED_CONDITION_KEYS:
        return [Problem(ERROR, "bad_condition_shape", f"{where}: unknown condition type {kind!r}")]

    out: list[Problem] = []
    missing = [key for key in _REQUIRED_CONDITION_KEYS[kind] if key not in node]
    if missing:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} is missing required key(s) {missing}",
            )
        )
    if kind == "numeric_state" and "above" not in node and "below" not in node:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} needs at least one of 'above'/'below'",
            )
        )
    if kind == "time" and "after" not in node and "before" not in node:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} needs at least one of 'after'/'before'",
            )
        )
    return out


def _check_condition_shapes(config: Config) -> list[Problem]:
    """Check every condition body's shape: known type, required keys present.

    See docs/rationale.md -- "Why `_check_condition_shapes` exists as a
    separate check".
    """
    out: list[Problem] = []
    for node, where in _condition_sites(config):
        if node is None:
            continue
        for n in _walk_condition_nodes(node):
            out += _check_condition_shape(n, where)
    return out
