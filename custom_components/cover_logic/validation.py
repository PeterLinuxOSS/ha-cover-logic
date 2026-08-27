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
    """One issue found in a configuration, at `ERROR` or `WARNING` severity.

    `owners` is which `(subentry_type, id)` pairs -- `id` being the value at
    that type's own `id_key` (`config_store._ID_KEY` for `condition`/`mode`,
    the equivalent for a future `rule`) -- carry the data this problem is
    actually about, for codes whose owning *type* alone is not enough to
    say which specific save could fix it: a condition body lives verbatim in
    a `condition` subentry's own fields, a `mode`'s `when`, or a `rule`'s
    `if`, and a dangling ref in one must not block a save of an unrelated
    other. Empty for every other code -- those have exactly one owning type,
    decided by the code alone (see `config_flow._CODE_OWNERS`), so no
    per-instance attribution is needed. Populated only by
    `_check_unknown_condition_refs`, `_check_condition_shapes` and
    `_check_circular_condition_refs`, the three checks whose codes need it;
    `config_flow._blocks_on` is the only reader.
    """

    severity: str
    code: str
    message: str
    owners: frozenset[tuple[str, str]] = frozenset()


def _rule_owner(key: str, index: int) -> tuple[str, str]:
    """The `(subentry_type, id)` naming one rule: its `(mode, zone)` key and its position.

    A rule subentry has no id field of its own -- its identity is
    `(mode, zone, order)` -- and `Config.rules` no longer carries `order` by
    the time this module sees it, so position within the already-`order`-sorted
    tuple is what is left to name it by. That is not a compromise invented
    here: `engine._apply_rules` labels the rule it fired with exactly this
    string, so a `Problem` and a decision trace name the same rule the same
    way. `config_store.rule_owner_ids` produces the matching side, mapping a
    real subentry id to this same string, and `tests/test_config_store.py`
    pins the two together.
    """
    return ("rule", f"{key}#{index}")


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
    for key, rules in config.rules.items():
        mode, _, zone = key.partition(".")
        if mode not in mode_ids or zone not in config.zones:
            # Every rule filed under this key carries the offending
            # `mode`/`zone` pair in its own subentry data, and each one's own
            # form is where it is repointed at a pair that exists -- so all
            # of them own this, the same way every name on a reference cycle
            # owns that cycle. Without this, a rule left stranded by deleting
            # its mode would block *adding a rule to an unrelated, healthy
            # pair*, a form with no way to reach the stranded one.
            out.append(
                Problem(
                    ERROR,
                    "unknown_rule_key",
                    f"rule key {key!r} names an unknown mode or zone",
                    owners=frozenset(_rule_owner(key, index) for index in range(len(rules))),
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
                # Every name on the cycle is a `condition` subentry (the
                # traversal only ever follows `config.conditions`, never a
                # mode's/rule's `when` -- neither can be *part of* a cycle,
                # only refer into one), and editing any single one of them to
                # break its outgoing ref fixes the whole cycle -- so all of
                # them are owners, not just the traversal's start.
                out.append(
                    Problem(
                        ERROR,
                        "circular_condition_ref",
                        f"circular condition reference: {loop}",
                        owners=frozenset(("condition", name) for name in cycle),
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


def _condition_sites(
    config: Config,
) -> Iterator[tuple[dict | list | None, str, tuple[str, str]]]:
    """Yield every top-level condition slot: its body, a label, and its owner.

    The owner is `(subentry_type, id)` -- `id` matching exactly what that
    type's own form would submit as `data[id_key]`, so `config_flow._blocks_
    on` can compare it directly against the subentry actually being saved.
    A rule has no such field, so it is named `f"{key}#{index}"` instead --
    see `_rule_owner` for why that shape, and `config_store.rule_owner_ids`
    for the mapping the `rule` flow uses to answer with the same string.
    """
    for cond_name, body in config.conditions.items():
        yield body, f"condition {cond_name!r}", ("condition", cond_name)
    for mode in config.modes:
        yield mode.when, f"mode {mode.id!r}", ("mode", mode.id)
    for key, rules in config.rules.items():
        for index, rule in enumerate(rules):
            yield rule.when, f"rule {key}#{index}", _rule_owner(key, index)


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
    for node, where, owner in _condition_sites(config):
        if node is None:
            continue
        out.extend(
            Problem(
                ERROR,
                "unknown_condition_ref",
                f"{where} refers to unknown condition {name!r}",
                owners=frozenset({owner}),
            )
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


def _check_condition_shape(node: dict, where: str, owner: tuple[str, str]) -> list[Problem]:
    """Check one condition dict's own shape; the caller walks its children.

    `owner` is the same `(subentry_type, id)` the whole site (`where`) came
    from -- every node nested inside one site's body lives in that one
    subentry's data blob, so a shape problem anywhere within it is fixed by
    that same subentry's own form, at whatever depth it is found.

    See docs/rationale.md -- "Why `_check_condition_shape` only checks known
    types and required keys".
    """
    kind = node.get("condition")
    owners = frozenset({owner})
    if kind not in _REQUIRED_CONDITION_KEYS:
        return [
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: unknown condition type {kind!r}",
                owners=owners,
            )
        ]

    out: list[Problem] = []
    missing = [key for key in _REQUIRED_CONDITION_KEYS[kind] if key not in node]
    if missing:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} is missing required key(s) {missing}",
                owners=owners,
            )
        )
    if kind == "numeric_state" and "above" not in node and "below" not in node:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} needs at least one of 'above'/'below'",
                owners=owners,
            )
        )
    if kind == "time" and "after" not in node and "before" not in node:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} needs at least one of 'after'/'before'",
                owners=owners,
            )
        )
    return out


def check_duplicate_rule_order(orders: dict[str, list[tuple[str, int]]]) -> list[Problem]:
    """Flag more than one rule subentry claiming the same `order` in one key.

    `orders` maps a `"<mode id>.<zone id>"` key to a `(owner id, order)` pair
    per rule subentry filed under it -- the owner id being the `_rule_owner`
    string naming that specific rule, so the resulting `Problem` can say
    *which* rules are tied rather than only that some are. Without that, a
    tie left behind anywhere would block every rule save (`config_flow.
    _blocks_on` would have nothing finer than the type to go on), including
    adding a rule to an unrelated pair that has no tie at all.

    Not part of `validate()`: rules are first-match-wins, so a subentry
    author's `order` *is* the behaviour, and Home Assistant subentries are a
    flat list with no native reordering -- but once
    `config_store.config_from_subentries` sorts a tie into `Config.rules`'s
    plain tuple, the tie is gone and indistinguishable from a deliberate
    sequence. This must run over the subentry-side grouping, before that
    happens, or a duplicate `order` becomes a silent pick the UI never shows
    as ambiguous. See `config_store.duplicate_rule_order_problems`, the only
    caller.
    """
    out: list[Problem] = []
    for key, items in orders.items():
        by_order: dict[int, list[str]] = {}
        for owner_id, order in items:
            by_order.setdefault(order, []).append(owner_id)
        # One problem per tied `order`, not one per extra rule on it: three
        # rules sharing an order are a single ambiguity to resolve, and every
        # one of them is an owner because editing any of them is a way to
        # resolve it.
        out.extend(
            Problem(
                ERROR,
                "duplicate_rule_order",
                f"{key}: more than one rule has order={order}",
                owners=frozenset(("rule", owner_id) for owner_id in owner_ids),
            )
            for order, owner_ids in by_order.items()
            if len(owner_ids) > 1
        )
    return out


def _check_condition_shapes(config: Config) -> list[Problem]:
    """Check every condition body's shape: known type, required keys present.

    See docs/rationale.md -- "Why `_check_condition_shapes` exists as a
    separate check".
    """
    out: list[Problem] = []
    for node, where, owner in _condition_sites(config):
        if node is None:
            continue
        for n in _walk_condition_nodes(node):
            out += _check_condition_shape(n, where, owner)
    return out
