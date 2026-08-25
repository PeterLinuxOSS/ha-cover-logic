"""The decision core: config + world snapshot -> one action per blind.

Pure and total. Every blind reachable from the configuration gets exactly one
action, and the trace records which rule produced it — so 'why did it do that'
is answerable from the output alone, never by re-deriving the logic by hand.
"""

from dataclasses import dataclass

from .conditions import evaluate_condition
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

    for zone_id, zone in config.zones.items():
        rules = config.rules.get(f"{mode}.{zone_id}")
        zone_targets, zone_trace = _evaluate_zone(config, rules, world, zone, mode, zone_id)
        targets.update(zone_targets)
        trace.update(zone_trace)

    return Decision(mode=mode, targets=targets, trace=trace)


def _evaluate_zone(
    config: Config,
    rules: tuple | None,
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
            action, label = _apply_rules(config, rules, world, target, mode, zone_id)
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
    rules: tuple | None,
    world: World,
    target: Target,
    mode: str,
    zone_id: str,
) -> tuple[Action, str]:
    """Run the first-match-wins rule list for one (mode, zone), one blind at a time.

    See docs/rationale.md -- "Why the `#none` trace label is ambiguous on
    purpose".
    """
    if not rules:
        return Action(), f"{mode}.{zone_id}#none"

    for index, rule in enumerate(rules):
        if rule.events is not None and world.event.kind not in rule.events:
            continue
        if not evaluate_condition(rule.when, world, target, config.conditions):
            continue
        label = f"{mode}.{zone_id}#{index}"
        if rule.name:
            label = f"{label} {rule.name}"
        return _resolve_action(rule.then, world), label

    return Action(), f"{mode}.{zone_id}#none"


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
