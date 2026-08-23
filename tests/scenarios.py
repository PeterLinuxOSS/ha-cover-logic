"""Derive the scenario space from the configuration itself.

This is what makes the suite universal: add a condition in the new house and
the scenarios that exercise it appear without anyone writing them.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any, Iterator

from cover_logic.conditions import evaluate_condition
from cover_logic.model import Config
from cover_logic.world import Event, Target, World

NOW = dt.datetime(2026, 8, 19, 13, 0)

# Fallback probes for a 45-degree sector, used only when a configuration
# declares no facade at all. Real probes are derived per-config below.
DEFAULT_AZIMUTH_PROBES = ["-1", "44", "45", "134", "135", "224", "225", "314", "315"]

SUN_ENTITY = "sun.sun"
DEFAULT_AZIMUTH_ENTITY = "sensor.sun_solar_azimuth"

# Separator encoding an attribute into an axis key. `pairwise()` treats keys as
# opaque, so this lets attribute conditions be varied like any other input
# without the covering array needing to know attributes exist. Entity ids are
# `domain.object_id` and never contain it.
AXIS_SEP = "|"


def _axis_key(entity: str, attribute: str | None = None) -> str:
    return entity if attribute is None else f"{entity}{AXIS_SEP}{attribute}"


def _split_axis_key(key: str) -> tuple[str, str | None]:
    entity, sep, attribute = key.partition(AXIS_SEP)
    return entity, (attribute if sep else None)


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, list):
        for child in node:
            yield from _walk(child)
    elif isinstance(node, dict):
        yield node
        for child in node.get("conditions", []):
            yield from _walk(child)


def _all_condition_nodes(config: Config) -> Iterator[dict]:
    """Every condition node the configuration can evaluate.

    Not only the `conditions:` section: a condition written inline in a mode's
    `when` or a rule's `if` is just as real, and the entities it reads need
    axes too. Reading only named conditions works by accident in a config that
    routes everything through `!ref`, and silently under-covers one that does
    not.
    """
    for cond in config.conditions.values():
        yield from _walk(cond)
    for mode in config.modes:
        if mode.when is not None:
            yield from _walk(mode.when)
    for rules in config.rules.values():
        for rule in rules:
            if rule.when is not None:
                yield from _walk(rule.when)


def _sun_entities(config: Config) -> tuple[str, str]:
    """Which entities `sun_hits_target` reads in THIS configuration.

    The condition accepts `sun_entity` and `azimuth_entity` overrides. A house
    using its own azimuth sensor would otherwise get an axis for the default
    name only, every generated world would leave its real sensor unset,
    `world.number` would fall back to -1, and every sun rule would look dead.
    """
    sun, azimuth = SUN_ENTITY, DEFAULT_AZIMUTH_ENTITY
    for node in _all_condition_nodes(config):
        if node.get("condition") == "sun_hits_target":
            sun = node.get("sun_entity", sun)
            azimuth = node.get("azimuth_entity", azimuth)
    return sun, azimuth


def _azimuth_probes(config: Config) -> list[str]:
    """Probe each facade's real sector boundaries, not a fixed 45-degree grid.

    The boundary is where an off-by-one in the half-open interval shows up, and
    it moves with `facade_azimuth` and `tolerance`. A hardcoded grid only probes
    the boundaries of the house it was written for.
    """
    probes: set[float] = {-1.0}
    for blind in config.blinds.values():
        if blind.facade_azimuth is None:
            continue
        probes.add(blind.facade_azimuth % 360.0)
        for edge in (
            blind.facade_azimuth - blind.tolerance,
            blind.facade_azimuth + blind.tolerance,
        ):
            base = edge % 360.0
            probes.update({(base - 1.0) % 360.0, base, (base + 1.0) % 360.0})
    if probes == {-1.0}:
        return list(DEFAULT_AZIMUTH_PROBES)
    return [f"{value:g}" for value in sorted(probes)]


def derive_axes(config: Config) -> dict[str, list[str]]:
    """One axis per input the configuration reads, with the values that matter.

    An axis key is an entity id, or `entity|attribute` for a condition that
    reads an attribute rather than a state.
    """
    axes: dict[str, set[str]] = {}

    def add(key: str, values) -> None:
        axes.setdefault(key, set()).update(values)

    for node in _all_condition_nodes(config):
        kind = node.get("condition")
        entity = node.get("entity_id")
        if not entity:
            continue
        key = _axis_key(entity, node.get("attribute"))
        if kind == "state":
            wanted = node["state"]
            values = list(wanted) if isinstance(wanted, (list, tuple)) else [wanted]
            add(key, [str(value) for value in values] + ["__other__"])
        elif kind == "numeric_state":
            bounds = [float(node[k]) for k in ("above", "below") if k in node]
            probes = {"unavailable"}
            for bound in bounds:
                probes.update({str(bound - 1), str(bound), str(bound + 1)})
            add(key, probes)

    sun_entity, azimuth_entity = _sun_entities(config)
    add(sun_entity, ["above_horizon", "below_horizon"])
    add(azimuth_entity, _azimuth_probes(config))
    for ref in config.values.values():
        add(ref.entity, [str(ref.default), "unavailable"])

    return {key: sorted(values) for key, values in axes.items()}


def pairwise(axes: dict[str, list[str]]) -> list[dict[str, str]]:
    """Greedy covering array: every pair of values appears in at least one row.

    Not minimal, but small — and pair coverage is where the real bugs live.
    """
    keys = sorted(axes)
    needed: set[tuple[str, str, str, str]] = set()
    for a, b in itertools.combinations(keys, 2):
        for va in axes[a]:
            for vb in axes[b]:
                needed.add((a, va, b, vb))

    rows: list[dict[str, str]] = []
    while needed:
        row = {k: axes[k][0] for k in keys}
        for key in keys:
            best, best_gain = row[key], -1
            for value in axes[key]:
                candidate = {**row, key: value}
                gain = sum(
                    1
                    for (a, va, b, vb) in needed
                    if candidate.get(a) == va and candidate.get(b) == vb
                )
                if gain > best_gain:
                    best, best_gain = value, gain
            row[key] = best
        covered = {
            (a, va, b, vb)
            for (a, va, b, vb) in needed
            if row.get(a) == va and row.get(b) == vb
        }
        if not covered:
            break
        needed -= covered
        rows.append(row)
    return rows


# --- Rule witnesses ---------------------------------------------------------
#
# Pairwise coverage guarantees every PAIR of axis values appears together in
# some row. It does not guarantee that a rule reached only by a conjunction
# of three or more simultaneous facts -- a deep first-match-wins chain, or a
# guard that ANDs several independent conditions -- ever gets its exact
# combination generated. On the real fixture, 25 of 83 rules needed such a
# combination and pairwise() alone (53 rows) never produced it, even though
# every individual axis VALUE those rules need was already present in
# derive_axes()'s output (verified by hand-built witnesses before writing
# this). That is the "derived scenario space is too narrow" branch the task
# brief calls out -- but the fix isn't a missing axis, it's coverage depth.
#
# Rather than widen axis VALUES (which would do nothing here -- nothing is
# missing) or blindly raise pairwise() to high-order N-wise combinatorics
# (computationally infeasible for the axis counts involved, and still no
# guarantee for a chain deeper than N), this solves each rule's own `when`
# directly from its parsed condition tree, negating every rule that precedes
# it in the same first-match-wins list. It is structural, not enumerated: a
# rule added to a future house gets its own witness the same way a new pair
# gets covered today, without anyone hand-writing a scenario for it. It only
# ever ADDS worlds to the pairwise-derived set -- pairwise's own guarantee is
# untouched.
#
# A rule whose guard cannot be solved from the axis vocabulary is silently
# skipped: `test_every_rule_fires_at_least_once` still reports it as dead,
# with the same message. This function can only add coverage, never hide a
# genuine finding.


class _Infeasible(Exception):
    """This rule's guard cannot be solved from the current axis vocabulary."""


def _probe_world(key: str, value: str) -> World:
    entity, attribute = _split_axis_key(key)
    if attribute is None:
        return World(states={entity: value}, attributes={}, now=NOW, event=Event())
    return World(
        states={}, attributes={(entity, attribute): value}, now=NOW, event=Event()
    )


def _leaf_true(key: str, node: dict, values: dict[str, str], axes: dict[str, list[str]]) -> None:
    for value in axes.get(key, []):
        if key in values and values[key] != value:
            continue
        if evaluate_condition(node, _probe_world(key, value), None, {}):
            values[key] = value
            return
    raise _Infeasible(f"no candidate value makes {key} satisfy {node}")


def _leaf_false(key: str, node: dict, values: dict[str, str], axes: dict[str, list[str]]) -> None:
    if key in values:
        if not evaluate_condition(node, _probe_world(key, values[key]), None, {}):
            return
        raise _Infeasible(f"pinned {key}={values[key]!r} does not falsify {node}")
    for value in axes.get(key, []):
        if not evaluate_condition(node, _probe_world(key, value), None, {}):
            values[key] = value
            return
    raise _Infeasible(f"no candidate value makes {key} falsify {node}")


def _require(
    cond: Any,
    want_true: bool,
    values: dict[str, str],
    axes: dict[str, list[str]],
    registry: dict[str, dict],
    target: Target,
) -> None:
    """Resolve `cond` to `want_true` in place, backtracking at choice points.

    AND-false and OR-true each involve a choice (which child carries the
    requirement); a child picked without knowledge of the rest of the tree
    can conflict with a pin made elsewhere (e.g. `pocasie_otvorene`'s first
    AND-child also reads `sun.sun`, which `sun_hits_target` may already have
    pinned for the rule itself). Each choice is tried against a snapshot of
    `values` and rolled back on failure, so the search finds a combination
    consistent with everything already required, not just the first option.
    """
    if cond is None:
        if not want_true:
            raise _Infeasible("cannot falsify 'no condition'")
        return
    if isinstance(cond, list):
        cond = {"condition": "and", "conditions": cond}

    kind = cond.get("condition")

    if kind == "ref":
        _require(registry[cond["name"]], want_true, values, axes, registry, target)
        return

    if kind in ("and", "or", "not"):
        children = cond["conditions"]
        # AND is true iff all children true; OR is true iff any child true;
        # NOT (this dialect's list-NOR) is true iff all children false.
        all_required = (kind == "and" and want_true) or (kind == "or" and not want_true) or (
            kind == "not" and want_true
        )
        child_truth = want_true if kind != "not" else not want_true
        if all_required:
            for child in children:
                _require(child, child_truth, values, axes, registry, target)
            return
        errors = []
        for child in children:
            snapshot = dict(values)
            try:
                _require(child, child_truth, values, axes, registry, target)
                return
            except _Infeasible as err:
                values.clear()
                values.update(snapshot)
                errors.append(str(err))
        raise _Infeasible(f"no child of {kind!r} could be resolved: {errors}")

    if kind == "time":
        actual = evaluate_condition(cond, World(states={}, attributes={}, now=NOW, event=Event()), None, registry)
        if actual != want_true:
            raise _Infeasible(f"time condition is fixed by NOW, cannot be made {want_true}")
        return

    if kind == "sun_hits_target":
        # Read the entities from the condition itself, not from module
        # constants: a house may point this at its own sun/azimuth sensors.
        sun_entity = cond.get("sun_entity", SUN_ENTITY)
        azimuth_entity = cond.get("azimuth_entity", DEFAULT_AZIMUTH_ENTITY)
        sun_node = {"condition": "state", "entity_id": sun_entity, "state": "above_horizon"}
        if not want_true:
            _leaf_false(sun_entity, sun_node, values, axes)
            return
        errors = []
        for azimuth in axes.get(azimuth_entity, DEFAULT_AZIMUTH_PROBES):
            snapshot = dict(values)
            try:
                probe = World(
                    states={sun_entity: "above_horizon", azimuth_entity: azimuth},
                    attributes={}, now=NOW, event=Event(),
                )
                if not evaluate_condition(cond, probe, target, registry):
                    raise _Infeasible("azimuth probe does not hit the target's facade")
                _leaf_true(sun_entity, sun_node, values, axes)
                _leaf_true(
                    azimuth_entity,
                    {"condition": "state", "entity_id": azimuth_entity, "state": azimuth},
                    values, axes,
                )
                return
            except _Infeasible as err:
                values.clear()
                values.update(snapshot)
                errors.append(str(err))
        raise _Infeasible(f"no azimuth probe hits the target's facade: {errors}")

    if kind == "event_targets_zone":
        # Depends on the chosen event's person, not on entity state; the
        # caller picks the event separately, so there is nothing to pin here.
        return

    entity = cond.get("entity_id")
    if entity is None:
        raise _Infeasible(f"leaf condition without entity_id: {cond}")
    key = _axis_key(entity, cond.get("attribute"))
    if want_true:
        _leaf_true(key, cond, values, axes)
    else:
        _leaf_false(key, cond, values, axes)


def _solve_rule_witness(config: Config, axes: dict[str, list[str]], key: str, index: int) -> World:
    mode_id, zone_id = key.split(".", 1)
    zone = config.zones[zone_id]
    blind = config.blinds[zone.members[0]]
    rule = config.rules[key][index]
    target = Target(blind=blind, zone=zone)

    event_kind = sorted(rule.events)[0] if rule.events is not None else "state_change"
    person = (zone.occupants[0] if zone.occupants else "peter") if event_kind == "arrival" else None
    event = Event(kind=event_kind, person=person)

    values: dict[str, str] = {}
    modes = list(config.modes)
    mode_pos = next(i for i, m in enumerate(modes) if m.id == mode_id)

    # The target rule's own guard first: it is usually the most specific
    # constraint (e.g. it pins sun.sun via sun_hits_target), and the
    # AND-false backtracking for everything negated below routes around it.
    _require(rule.when, True, values, axes, config.conditions, target)
    _require(modes[mode_pos].when, True, values, axes, config.conditions, target)

    for earlier_mode in modes[:mode_pos]:
        _require(earlier_mode.when, False, values, axes, config.conditions, target)

    for prior in config.rules[key][:index]:
        if prior.events is not None and event_kind not in prior.events:
            continue  # already skipped by event-kind filter, nothing to negate
        _require(prior.when, False, values, axes, config.conditions, target)

    full = {key: candidates[0] for key, candidates in axes.items()}
    full.update(values)
    return _world_from_row(full, event)


def rule_witnesses(config: Config, axes: dict[str, list[str]]) -> list[World]:
    """One structurally-derived world per rule, supplementing pairwise()."""
    out: list[World] = []
    for key, rules in config.rules.items():
        zone = config.zones.get(key.split(".", 1)[1])
        if zone is None or not zone.members or zone.members[0] not in config.blinds:
            continue
        for index in range(len(rules)):
            try:
                out.append(_solve_rule_witness(config, axes, key, index))
            except _Infeasible:
                continue  # left for test_every_rule_fires_at_least_once to report
    return out


def _world_from_row(row: dict[str, str], event: Event) -> World:
    """Split a covering-array row back into states and attributes."""
    states: dict[str, str] = {}
    attributes: dict[tuple[str, str], str] = {}
    for key, value in row.items():
        entity, attribute = _split_axis_key(key)
        if attribute is None:
            states[entity] = value
        else:
            attributes[(entity, attribute)] = value
    return World(states=states, attributes=attributes, now=NOW, event=event)


def worlds(config: Config) -> list[World]:
    axes = derive_axes(config)
    rows = pairwise(axes)
    out: list[World] = []
    for row in rows:
        for event in (Event(), Event(kind="arrival", person="peter")):
            out.append(_world_from_row(row, event))
    out.extend(rule_witnesses(config, axes))
    return out


def fired_rules(config: Config, all_worlds) -> set[str]:
    from cover_logic.engine import evaluate

    fired: set[str] = set()
    for world in all_worlds:
        for label in evaluate(config, world).trace.values():
            fired.add(label.split(" ")[0])
    return fired
