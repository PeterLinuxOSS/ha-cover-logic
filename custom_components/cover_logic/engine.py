"""The decision core: config + world snapshot -> one action per blind.

Pure and total. Every blind reachable from the configuration gets exactly one
action, and the trace records which rule produced it — so 'why did it do that'
is answerable from the output alone, never by re-deriving the logic by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from .conditions import evaluate_condition
from .model import KEEP, Action, Config, Ref, Value
from .world import Target, World


class EngineError(Exception):
    """Raised when the configuration cannot produce a decision."""


@dataclass(frozen=True)
class Decision:
    mode: str
    targets: dict[str, Action]
    trace: dict[str, str]


def evaluate(config: Config, world: World) -> Decision:
    mode = _resolve_mode(config, world)
    owner = _resolve_ownership(config)

    targets: dict[str, Action] = {}
    trace: dict[str, str] = {}

    for zone_id, zone in config.zones.items():
        rules = config.rules.get(f"{mode}.{zone_id}")
        for entity in zone.members:
            blind = config.blinds.get(entity)
            if blind is None:
                raise EngineError(f"zone {zone_id!r} refers to unknown blind {entity!r}")
            target = Target(blind=blind, zone=zone)
            action, label = _apply_rules(config, rules, world, target, mode, zone_id)
            targets[entity] = action
            trace[entity] = label

    return Decision(mode=mode, targets=targets, trace=trace)


def _resolve_mode(config: Config, world: World) -> str:
    for mode in config.modes:
        if evaluate_condition(mode.when, world, None, config.conditions):
            return mode.id
    raise EngineError("no mode matched and no fallback mode is defined")


def _resolve_ownership(config: Config) -> dict[str, str]:
    """Exactly one zone owns each blind.

    Two owners and no owners are both the bug class this kills: a blind
    nobody decides is a blind nobody moves, silently, far from this check.
    """
    owner: dict[str, str] = {}
    for zone_id, zone in config.zones.items():
        for entity in zone.members:
            if entity in owner:
                raise EngineError(
                    f"blind {entity!r} is in two zones: {owner[entity]!r} and {zone_id!r}"
                )
            owner[entity] = zone_id

    orphans = set(config.blinds) - set(owner)
    if orphans:
        raise EngineError(
            f"blind {sorted(orphans)[0]!r} is configured but owned by no zone"
        )
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

    The trace label `#none` is deliberately ambiguous between two causes: no
    rules were configured at all for this (mode, zone) key, and rules were
    configured but none of them matched this blind. Both end in "keep, keep"
    and both are represented the same way. Debugging a `#none` means checking
    for either cause — first whether `mode.zone` exists in `config.rules` at
    all, then, if it does, why every rule in it fell through.
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
    if isinstance(value, Ref):
        return int(world.number(value.entity, default=float(value.default)))
    return value
