"""The decision core: config + world snapshot -> one action per blind.

Pure and total. Every blind reachable from the configuration gets exactly one
action, and the trace records which rule produced it — so 'why did it do that'
is answerable from the output alone, never by re-deriving the logic by hand.
"""

from dataclasses import dataclass

from .conditions import evaluate_condition
from .const import RULE_DEFAULT_ZONE
from .model import Action, Config, Ref, Value, Zone
from .world import Target, World


class EngineError(Exception):
    """Raised when the configuration cannot produce a decision."""


@dataclass(frozen=True)
class Decision:
    """The resolved mode, the action for every blind, and why each one fired."""

    mode: str
    targets: dict[str, Action]
    trace: dict[str, str]


def evaluate(config: Config, world: World) -> Decision:
    """Decide the action for every blind in `config`, given one `World` snapshot."""
    mode = _resolve_mode(config, world)
    # The call must stay -- it raises on a duplicate or orphaned blind -- but
    # the ownership map itself is never read here; `_apply_rules` below
    # resolves each blind's rules straight from `config.rules`.
    _resolve_ownership(config)

    targets: dict[str, Action] = {}
    trace: dict[str, str] = {}

    # Looked up once per mode, outside the zone loop: a default is a
    # property of the mode, shared verbatim by every zone that falls back to
    # it, not something re-read per zone. See the "Inherited rules" section
    # below `_apply_rules` for how `own_rules` and `default_rules` combine.
    default_rules = config.rules.get(f"{mode}.{RULE_DEFAULT_ZONE}")

    for zone_id, zone in config.zones.items():
        own_rules = config.rules.get(f"{mode}.{zone_id}")
        zone_targets, zone_trace = _evaluate_zone(
            config, own_rules, default_rules, world, zone, mode, zone_id
        )
        targets.update(zone_targets)
        trace.update(zone_trace)

    return Decision(mode=mode, targets=targets, trace=trace)


def _evaluate_zone(
    config: Config,
    own_rules: tuple | None,
    default_rules: tuple | None,
    world: World,
    zone: Zone,
    mode: str,
    zone_id: str,
) -> tuple[dict[str, Action], dict[str, str]]:
    """Decide every blind in one zone; a broken rule here must not affect other zones.

    See docs/rationale.md -- "Why `EngineError` must propagate uncontained
    out of `_evaluate_zone`".
    """
    targets: dict[str, Action] = {}
    trace: dict[str, str] = {}
    try:
        for entity in zone.members:
            blind = config.blinds.get(entity)
            if blind is None:
                msg = f"zone {zone_id!r} refers to unknown blind {entity!r}"
                raise EngineError(msg)
            target = Target(blind=blind, zone=zone)
            action, label = _apply_rules(
                config, own_rules, default_rules, world, target, mode, zone_id
            )
            targets[entity] = action
            trace[entity] = label
    except EngineError:
        raise
    except Exception as err:  # noqa: BLE001 -- contain any rule failure, by design
        label = f"{mode}.{zone_id}#error {type(err).__name__}: {err}"
        return (
            dict.fromkeys(zone.members, Action()),
            dict.fromkeys(zone.members, label),
        )
    return targets, trace


def _resolve_mode(config: Config, world: World) -> str:
    for mode in config.modes:
        if evaluate_condition(mode.when, world, None, config.conditions):
            return mode.id
    msg = "no mode matched and no fallback mode is defined"
    raise EngineError(msg)


def _resolve_ownership(config: Config) -> dict[str, str]:
    """Exactly one zone owns each blind.

    Two owners and no owners are both the bug class this kills: a blind
    nobody decides is a blind nobody moves, silently, far from this check.
    """
    owner: dict[str, str] = {}
    for zone_id, zone in config.zones.items():
        for entity in zone.members:
            if entity in owner:
                msg = f"blind {entity!r} is in two zones: {owner[entity]!r} and {zone_id!r}"
                raise EngineError(msg)
            owner[entity] = zone_id

    orphans = set(config.blinds) - set(owner)
    if orphans:
        msg = f"blind {min(orphans)!r} is configured but owned by no zone"
        raise EngineError(msg)
    return owner


def _apply_rules(
    config: Config,
    own_rules: tuple | None,
    default_rules: tuple | None,
    world: World,
    target: Target,
    mode: str,
    zone_id: str,
) -> tuple[Action, str]:
    """Run the zone's own rules, then the mode's defaults, first match wins.

    Inherited rules. A rule filed under `f"{mode}.{RULE_DEFAULT_ZONE}"`
    (`"*"`, see `const.RULE_DEFAULT_ZONE`) applies to every zone in that mode
    that does not shadow it -- a zone's own rule list is tried in full
    first, and only once nothing in it matches does evaluation fall through
    to the shared defaults, also in full, in their own `order`. This is two
    sequential list scans, not one list built by interleaving `own_rules` and
    `default_rules` by a shared `order` value: `config_store._grouped_rules`
    already sorts each of the two lists *within itself* (see its own "One
    grouping, not two" docstring section), so this function's job is only to
    decide the sequence the two already-ordered lists run in, never to
    re-sort either. That sequencing is also the answer to "what does it mean
    when a default and a zone rule share the same numeric `order`": nothing
    special -- `order` only orders rules against others in the *same* list;
    a zone's own rule at `order: 100` still runs before a default at
    `order: 1`, because the zone's whole list is exhausted before the
    default's is ever considered. Pinned in `tests/test_engine.py` and, where
    `order` values actually still exist to cross, in `tests/test_config_store.py`.

    Trace labels a default match with the default's own key
    (`f"{mode}.{RULE_DEFAULT_ZONE}#{index}"`), not the zone's -- the same
    default row fired for two different zones must read as the same rule in
    both traces, matching the one subentry it actually is (see
    `config_store.rule_owner_ids`, which names it the same way).

    See docs/rationale.md -- "Why the `#none` trace label is ambiguous on
    purpose" for why a miss against both lists collapses to one label
    instead of naming which of "no own rules", "own rules all missed", "no
    defaults" or "defaults all missed" happened -- inheritance adds two more
    ways to reach `#none`, and the existing rationale for not distinguishing
    them applies unchanged: debugging it means checking every cause, exactly
    as before.
    """
    match = _match_rules(config, own_rules, world, target, f"{mode}.{zone_id}")
    if match is not None:
        return match

    match = _match_rules(config, default_rules, world, target, f"{mode}.{RULE_DEFAULT_ZONE}")
    if match is not None:
        return match

    return Action(), f"{mode}.{zone_id}#none"


def _match_rules(
    config: Config,
    rules: tuple | None,
    world: World,
    target: Target,
    key: str,
) -> tuple[Action, str] | None:
    """The first rule in `rules` that matches, or `None` if none do (or there are none).

    Shared by `_apply_rules`'s two scans (the zone's own list, then the
    mode's defaults) so the match/skip logic -- `events` scoping, then the
    condition -- exists in exactly one place regardless of which list is
    being walked.
    """
    if not rules:
        return None

    for index, rule in enumerate(rules):
        if rule.events is not None and world.event.kind not in rule.events:
            continue
        if not evaluate_condition(rule.when, world, target, config.conditions):
            continue
        label = f"{key}#{index}"
        if rule.name:
            label = f"{label} {rule.name}"
        return _resolve_action(rule.then, world), label

    return None


def _resolve_action(action: Action, world: World) -> Action:
    return Action(
        position=_resolve_value(action.position, world),
        tilt=_resolve_value(action.tilt, world),
    )


def _resolve_value(value: Value, world: World) -> Value:
    """Resolve a `Ref` to the helper's current value, truncated and unclamped.

    See docs/rationale.md -- "Why `_resolve_value` truncates instead of
    rounding" and "Why the engine does not clamp resolved values to 0..100".
    """
    if isinstance(value, Ref):
        return int(world.number(value.entity, default=float(value.default)))
    return value
