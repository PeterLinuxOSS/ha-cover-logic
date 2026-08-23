"""Static checks over a configuration.

Answers the questions the old Jinja matrix could not be asked: is every blind
owned, is every rule reachable, does every (mode, zone) pair actually decide
anything. Runs in the test suite and again on import in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import COND_REF
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
                out.append(Problem(ERROR, "blind_in_two_zones",
                                   f"blind {entity!r} is owned by {owner[entity]!r} and {zone_id!r}"))
            else:
                owner[entity] = zone_id

    for entity in config.blinds:
        if entity not in owner:
            out.append(Problem(ERROR, "blind_without_zone",
                               f"blind {entity!r} belongs to no zone, so no rule decides it"))
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
            cycle_set = frozenset(cycle)
            if cycle_set not in cycles_reported:
                cycles_reported.add(cycle_set)
                sorted_names = sorted(cycle)
                out.append(Problem(ERROR, "circular_condition_ref",
                                   f"circular condition reference: {' -> '.join(sorted_names)} -> {sorted_names[0]}"))

    return out


def _find_cycle_from(start_name: str, registry: dict[str, dict]) -> list[str] | None:
    """Find a cycle starting from start_name using DFS.

    Returns a list of condition names forming a cycle, or None if no cycle exists.
    """
    visited: set[str] = set()
    rec_stack: set[str] = set()
    path: list[str] = []
    found_cycle: list[str] | None = None

    def dfs(node: str) -> None:
        nonlocal found_cycle
        if found_cycle is not None:
            return

        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        # Get all conditions referenced by this node
        referenced = _get_referenced_conditions(node, registry)
        for ref_name in referenced:
            if ref_name not in visited:
                dfs(ref_name)
                if found_cycle is not None:
                    path.pop()
                    rec_stack.remove(node)
                    return
            elif ref_name in rec_stack:
                # Found a cycle: extract it from path
                cycle_start_idx = path.index(ref_name)
                found_cycle = path[cycle_start_idx:] + [ref_name]
                path.pop()
                rec_stack.remove(node)
                return

        path.pop()
        rec_stack.remove(node)

    dfs(start_name)
    # Remove the duplicate last element (found_cycle includes it for easier detection)
    # to return just the cycle without repeating the start node at the end
    if found_cycle is not None:
        return found_cycle[:-1]  # Remove the repeated element
    return None


def _get_referenced_conditions(cond_name: str, registry: dict[str, dict]) -> set[str]:
    """Return the set of condition names directly referenced by cond_name."""
    if cond_name not in registry:
        return set()

    cond = registry[cond_name]
    referenced: set[str] = set()

    def walk(node):
        """Recursively walk the condition structure to find all refs."""
        if isinstance(node, dict):
            if node.get("condition") == COND_REF:
                referenced.add(node.get("name", ""))
            elif "conditions" in node:
                for sub_cond in node["conditions"]:
                    walk(sub_cond)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(cond)
    return referenced
