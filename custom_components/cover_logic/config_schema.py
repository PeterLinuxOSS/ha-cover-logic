"""Parse the YAML configuration into frozen model objects.

This is also the import/export format: what the UI writes, what the tests read.
There is exactly one representation of the rules, so config and tests can never
drift apart.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .conditions import DEFAULT_AZIMUTH_ENTITY, SUN_ENTITY
from .const import (
    COND_REF,
    COND_SUN_HITS_TARGET,
    GUARD_ANY,
    GUARD_DEFAULT_RECHECK,
    GUARD_DEFER,
    GUARD_STAGE_OUTPUT,
    RULE_DEFAULT_ZONE,
)
from .model import KEEP, UNSET, Action, Blind, Config, Guard, Mode, Ref, Rule, Value, Zone


class ConfigError(Exception):
    """Raised when the configuration cannot be turned into a valid Config."""


# Key sets for the structures this file owns. Condition bodies (native HA
# condition dicts) are deliberately excluded from strict checking -- see the
# scoping note on `_check_keys`. Constants and set contents below are
# alphabetised; membership testing (`set(mapping) - allowed`) never depends
# on order.
_ACTION_KEYS = {"position", "tilt"}
_BLIND_KEYS = {
    "entity",
    "facade_azimuth",
    "has_tilt",
    "tilt_after_arrival",
    "tolerance",
    "travel_time",
}
_GUARD_KEYS = {
    "applies_to",
    "max_wait",
    "name",
    "on_timeout",
    "policy",
    "recheck_every",
    "stage",
    "targets",
    "then",
    "when",
}
_MODE_KEYS = {"id", "when"}
_RULE_KEYS = {"events", "if", "name", "then"}
_TOP_LEVEL_KEYS = {"blinds", "conditions", "guards", "modes", "rules", "values", "zones"}
_VALUE_KEYS = {"default", "entity"}
_ZONE_KEYS = {"members", "occupants"}

# A cover's position and tilt are both percentages.
_AXIS_MAX = 100
_AXIS_MIN = 0


def _check_keys(mapping: dict, allowed: set[str], where: str) -> None:
    """Raise if `mapping` has keys outside `allowed`.

    See docs/rationale.md -- "Why condition bodies are exempt from strict key
    checking".
    """
    unknown = set(mapping) - allowed
    if unknown:
        msg = f"unknown key(s) {sorted(unknown)} in {where}"
        raise ConfigError(msg)


def _expect_mapping(node: Any, where: str) -> dict:
    if not isinstance(node, dict):
        msg = f"{where} must be a mapping, got {node!r}"
        raise ConfigError(msg)

    return node


def _expect_list(node: Any, where: str) -> list:
    if not isinstance(node, list):
        msg = f"{where} must be a list, got {node!r}"
        raise ConfigError(msg)

    return node


def _reject_dot(identifier: Any, where: str) -> None:
    """A mode or zone id must not contain '.'.

    See docs/rationale.md -- "Why a mode or zone id must not contain a dot".
    """
    if isinstance(identifier, str) and "." in identifier:
        msg = f"{where} must not contain '.': {identifier!r}"
        raise ConfigError(msg)


def _reject_zone_id(zone_id: Any, where: str) -> None:
    """A zone id must not contain '.' (see `_reject_dot`) and must not be `"*"`.

    `"*"` in the zone half of a `"<mode>.<zone>"` rules key is reserved to
    mean "the default rule list for this mode" (`const.RULE_DEFAULT_ZONE`,
    read by `engine.evaluate`) -- a real zone allowed to claim that name
    would make `f"{mode}.{zone_id}"` collide with `f"{mode}.{RULE_DEFAULT_ZONE}"`
    and silently steal every mode's default rules as if they were that one
    zone's own. Checked wherever a zone id is declared: here for the YAML
    path (`load_config`'s zone loop) and again in `config_store._build_zones`
    for the subentry path, so a zone cannot be named this through either
    door into `Config`.
    """
    _reject_dot(zone_id, where)
    if zone_id == RULE_DEFAULT_ZONE:
        msg = f"{where} must not be {RULE_DEFAULT_ZONE!r}: reserved for a mode's default rules"
        raise ConfigError(msg)


class RefTag:
    """Placeholder produced by the `!ref` YAML tag.

    Interpreted by context: inside `if:` it names a condition, inside an action
    axis it names a value. The two namespaces are separate on purpose.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        """Store the referenced condition or value name."""
        self.name = name

    def __repr__(self) -> str:
        """Return a debug repr including the referenced name."""
        return f"RefTag({self.name!r})"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!ref", lambda loader, node: RefTag(loader.construct_scalar(node)))


def load_config(text: str) -> Config:
    """Parse YAML config text into a frozen `Config`, raising `ConfigError` on any problem."""
    raw = yaml.load(text, Loader=_Loader) or {}
    raw = _expect_mapping(raw, "top level config")
    _check_keys(raw, _TOP_LEVEL_KEYS, "top level")

    # `raw_conditions` is the lookup namespace for every `!ref` in a
    # condition slot -- built once, up front, so a condition may reference
    # another regardless of which one is declared first in the file.
    raw_conditions = _expect_mapping(raw.get("conditions") or {}, "'conditions'")
    conditions = {
        name: _parse_condition(body, raw_conditions) for name, body in raw_conditions.items()
    }

    values = _parse_values(_expect_mapping(raw.get("values") or {}, "'values'"))

    blinds = {}
    for item in _expect_list(raw.get("blinds") or [], "'blinds'"):
        blind = _parse_blind(item)
        blinds[blind.entity] = blind

    raw_zones = _expect_mapping(raw.get("zones") or {}, "'zones'")
    zones = {}
    for zone_id, raw_body in raw_zones.items():
        _reject_zone_id(zone_id, "zone id")
        body = _expect_mapping(raw_body, f"zone {zone_id!r}")
        _check_keys(body, _ZONE_KEYS, f"zone {zone_id!r}")
        zones[zone_id] = Zone(
            id=zone_id,
            members=tuple(body.get("members") or ()),
            occupants=tuple(body.get("occupants") or ()),
        )

    modes = []
    for raw_item in _expect_list(raw.get("modes") or [], "'modes'"):
        item = _expect_mapping(raw_item, "mode entry")
        _check_keys(item, _MODE_KEYS, "mode")
        if "id" not in item:
            msg = f"mode without 'id': {item!r}"
            raise ConfigError(msg)

        _reject_dot(item["id"], "mode id")
        modes.append(Mode(id=item["id"], when=_parse_condition(item.get("when"), raw_conditions)))
    modes = tuple(modes)

    raw_rules = _expect_mapping(raw.get("rules") or {}, "'rules'")
    rules = {
        key: tuple(
            _parse_rule(item, raw_conditions, values)
            for item in _expect_list(items, f"rules {key!r}")
        )
        for key, items in raw_rules.items()
    }

    return Config(
        blinds=blinds,
        zones=zones,
        modes=modes,
        rules=rules,
        conditions=conditions,
        values=values,
        guards=parse_guards(raw.get("guards"), raw_conditions, values),
    )


def load_config_file(path: str | Path) -> Config:
    """Read and parse a YAML config file into a frozen `Config`."""
    return load_config(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# `dump_config`/`dump_config_file`: the write side, used by the
# `export_config` service (`services.py`). Together with `load_config`/
# `load_config_file` above, these make config text and a `Config` two
# directions of the one representation this module owns -- see the module
# docstring's "one representation of the rules" line, which up to now only
# had to hold in one direction.
# ---------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """`yaml.SafeDumper` plus one representer: `RefTag` back into the `!ref` tag."""


_Dumper.add_representer(RefTag, lambda dumper, data: dumper.represent_scalar("!ref", data.name))


def unparse_condition(node: Any, ref_factory: Callable[[str], Any]) -> Any:
    """Invert `_parse_condition`'s ref handling: `{"condition": "ref", "name": X}` -> ref.

    A parsed `Config` never keeps the original `!ref`/`{"ref": ...}` spelling
    once a condition ref has resolved -- `_parse_condition`'s `RefTag` branch
    above replaces it with this one marker shape, the same one whether the
    ref came from YAML's `!ref` tag or `config_store`'s own `{"ref": ...}`
    convention. This is the one walk that turns that marker back into
    whatever `ref_factory` names it as, so `dump_config` (`ref_factory=
    RefTag`, reproducing `!ref X`) and `config_store.subentries_from_config`
    (`ref_factory` wrapping `X` as `{"ref": X}`) share it instead of running
    two independent copies of the same tree walk that could silently drift
    apart -- exactly the risk `config_store.py`'s own "Refs" docstring
    section warns about for the parse direction.

    Only the `conditions` key is ever recursed into, mirroring exactly what
    `_parse_condition`'s dict branch itself recurses into and nothing more;
    any other key (`entity_id`, `state`, ...) is passed through unchanged,
    since `_parse_condition` never touches it either.
    """
    if node is None:
        return None
    if isinstance(node, list):
        return [unparse_condition(child, ref_factory) for child in node]
    if isinstance(node, dict):
        if node.get("condition") == COND_REF and set(node) == {"condition", "name"}:
            return ref_factory(node["name"])
        return {
            key: (unparse_condition(value, ref_factory) if key == "conditions" else value)
            for key, value in node.items()
        }
    return node


def unparse_axis(value: Any, ref_names: dict[int, str], ref_factory: Callable[[str], Any]) -> Any:
    """Invert `_parse_axis`: a resolved action axis -> literal / `"keep"` / ref.

    `ref_names` maps `id(ref)` -- identity, not equality: two differently
    named `values:` entries could coincidentally share `entity`/`default` --
    to the name that produced it. Every `Ref` a parsed `Config` holds on an
    action axis was put there by `_parse_axis` resolving a `RefTag`/
    `{"ref": ...}` against `values[name]` and handing back that exact object
    (see `_parse_axis`'s `RefTag` branch above), so for any `Config` that
    actually came from `load_config`/`config_from_subentries`, `ref_names`
    (built by the caller from `config.values`) always has an entry for it.
    """
    if value is KEEP:
        return "keep"
    if isinstance(value, Ref):
        return ref_factory(ref_names[id(value)])
    return value


def _blind_to_dict(blind: Blind) -> dict[str, Any]:
    """The blind mapping `_parse_blind` reads back -- shared by YAML and subentry export.

    A blind's YAML entry and a `blind` subentry's own `data` are already the
    identical shape (both go straight through `_parse_blind`, unchanged), so
    `config_store.subentries_from_config` reuses this instead of a second
    copy.
    """
    out: dict[str, Any] = {"entity": blind.entity}
    if blind.facade_azimuth is not None:
        out["facade_azimuth"] = blind.facade_azimuth
    out["tolerance"] = blind.tolerance
    out["travel_time"] = blind.travel_time
    out["has_tilt"] = blind.has_tilt
    out["tilt_after_arrival"] = blind.tilt_after_arrival
    return out


def _zone_to_dict(zone: Zone) -> dict[str, Any]:
    out: dict[str, Any] = {"members": list(zone.members)}
    if zone.occupants:
        out["occupants"] = list(zone.occupants)
    return out


def _mode_to_dict(mode: Mode, ref_factory: Callable[[str], Any] = RefTag) -> dict[str, Any]:
    """The mode mapping `load_config`'s mode loop reads back.

    `ref_factory` defaults to `RefTag` (the YAML spelling) but is a parameter,
    not hard-coded, so `config_store.subentries_from_config` can reuse this
    unchanged with its own `{"ref": ...}` marker instead of running a second
    copy of the same `when`-shaping logic -- the write-side twin of why
    `config_store._build_modes` calls back into this module's own
    `_parse_condition` rather than re-implementing it.
    """
    out: dict[str, Any] = {"id": mode.id}
    if mode.when is not None:
        out["when"] = unparse_condition(mode.when, ref_factory)
    return out


def _rule_to_dict(
    rule: Rule, ref_names: dict[int, str], ref_factory: Callable[[str], Any] = RefTag
) -> dict[str, Any]:
    """The rule mapping `_parse_rule` reads back -- shared by YAML and subentry export.

    See `_mode_to_dict`'s docstring for why `ref_factory` is a parameter.
    """
    out: dict[str, Any] = {}
    if rule.when is not None:
        out["if"] = unparse_condition(rule.when, ref_factory)
    out["then"] = {
        "position": unparse_axis(rule.then.position, ref_names, ref_factory),
        "tilt": unparse_axis(rule.then.tilt, ref_names, ref_factory),
    }
    if rule.events is not None:
        out["events"] = sorted(rule.events)
    if rule.name:
        out["name"] = rule.name
    return out


def guard_to_dict(
    guard: Guard, ref_names: dict[int, str], ref_factory: Callable[[str], Any] = RefTag
) -> dict[str, Any]:
    """The guard mapping `_parse_guard` reads back -- shared by YAML and subentry export.

    See `_mode_to_dict`'s docstring for why `ref_factory` is a parameter.
    Public for the same reason `parse_guards` is: `config_store.py` writes
    guards into `entry.data` in exactly this shape, and a second serializer
    there would be free to drift from this one.

    `applies_to` and `stage` are always written, defaults included -- the
    same choice `_blind_to_dict` makes for `tolerance`/`travel_time`, and for
    a stronger reason here: which movements a safety interlock covers is the
    first thing a reader needs and the last thing that should be inferred
    from an absent key. `max_wait` is written only when the guard actually
    carries one, since `UNSET` (absent) and `None` (`null`, wait forever) are
    different configurations and must dump to different text.
    """
    out: dict[str, Any] = {}
    if guard.name:
        out["name"] = guard.name
    out["policy"] = guard.policy
    out["applies_to"] = guard.applies_to
    out["stage"] = guard.stage
    if guard.targets:
        out["targets"] = list(guard.targets)
    if guard.when is not None:
        out["when"] = unparse_condition(guard.when, ref_factory)
    if guard.max_wait is not UNSET:
        out["max_wait"] = guard.max_wait
    if guard.on_timeout is not None:
        out["on_timeout"] = guard.on_timeout
    if guard.recheck_every is not None:
        out["recheck_every"] = guard.recheck_every
    if guard.then is not None:
        out["then"] = {
            "position": unparse_axis(guard.then.position, ref_names, ref_factory),
            "tilt": unparse_axis(guard.then.tilt, ref_names, ref_factory),
        }
    return out


def dump_config(config: Config) -> str:
    """Serialize `config` as YAML text in the shape `load_config` reads back.

    The inverse of `load_config`. Dict-keyed sections (`zones`, `values`,
    `conditions`, `rules`) are written in key-sorted order purely so the
    output file is stable and diffable -- `load_config` never depends on key
    order, so this loses nothing. `modes` (a tuple) and each `rules[...]`
    list (also a tuple) are the opposite: order there *is* meaning (first
    match wins -- see `MODELS.md` §3), so those are written in their exact
    tuple order, never re-sorted.
    """
    ref_names = {id(ref): name for name, ref in config.values.items()}

    doc: dict[str, Any] = {}
    if config.blinds:
        doc["blinds"] = [_blind_to_dict(blind) for blind in config.blinds.values()]
    if config.zones:
        doc["zones"] = {
            zone_id: _zone_to_dict(zone) for zone_id, zone in sorted(config.zones.items())
        }
    if config.values:
        doc["values"] = {
            name: {"entity": ref.entity, "default": ref.default}
            for name, ref in sorted(config.values.items())
        }
    if config.conditions:
        doc["conditions"] = {
            name: unparse_condition(body, RefTag)
            for name, body in sorted(config.conditions.items())
        }
    if config.modes:
        doc["modes"] = [_mode_to_dict(mode) for mode in config.modes]
    if config.rules:
        doc["rules"] = {
            key: [_rule_to_dict(rule, ref_names) for rule in rules]
            for key, rules in sorted(config.rules.items())
        }
    if config.guards:
        # Written in tuple order, never sorted: guards are first-match-wins,
        # so their order is behaviour -- the same reason `modes` and each
        # `rules[...]` list above are left alone.
        doc["guards"] = [guard_to_dict(guard, ref_names) for guard in config.guards]

    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True)


def dump_config_file(path: str | Path, config: Config) -> None:
    """Write `config` as YAML to `path`. The write-side mirror of `load_config_file`."""
    Path(path).write_text(dump_config(config), encoding="utf-8")


def _parse_values(raw: dict[str, Any]) -> dict[str, Ref]:
    out: dict[str, Ref] = {}
    for name, raw_body in raw.items():
        body = _expect_mapping(raw_body, f"value {name!r}")
        _check_keys(body, _VALUE_KEYS, f"value {name!r}")
        try:
            entity = body["entity"]
            default = int(body["default"])
        except (KeyError, TypeError, ValueError) as err:
            msg = f"value {name!r} needs 'entity' and integer 'default'"
            raise ConfigError(msg) from err
        # See docs/rationale.md -- "Why a `!ref` default is range-checked
        # exactly like a literal".
        if not _AXIS_MIN <= default <= _AXIS_MAX:
            msg = f"value {name!r} default must be 0..100, got {default}"
            raise ConfigError(msg)

        out[name] = Ref(entity=entity, default=default)
    return out


def _parse_blind(item: Any) -> Blind:
    item = _expect_mapping(item, "blind entry")
    _check_keys(item, _BLIND_KEYS, "blind entry")
    if "entity" not in item:
        msg = f"blind without 'entity': {item!r}"
        raise ConfigError(msg)

    azimuth = item.get("facade_azimuth")
    return Blind(
        entity=item["entity"],
        facade_azimuth=None if azimuth is None else float(azimuth),
        tolerance=float(item.get("tolerance", 45.0)),
        travel_time=float(item.get("travel_time", 60.0)),
        tilt_after_arrival=bool(item.get("tilt_after_arrival", True)),
        has_tilt=bool(item.get("has_tilt", True)),
    )


def _parse_condition(node: Any, conditions: dict[str, Any]) -> dict | list | None:
    if node is None:
        return None
    if isinstance(node, RefTag):
        if node.name not in conditions:
            msg = f"unknown condition ref: {node.name!r}"
            raise ConfigError(msg)

        return {"condition": "ref", "name": node.name}
    if isinstance(node, list):
        return [_parse_condition(child, conditions) for child in node]
    if isinstance(node, dict):
        out = {k: v for k, v in node.items() if k != "conditions"}
        if "conditions" in node:
            out["conditions"] = [
                _parse_condition(child, conditions) for child in node["conditions"]
            ]
        return out
    msg = f"cannot read condition: {node!r}"
    raise ConfigError(msg)


def _parse_axis(node: Any, values: dict[str, Ref]) -> Value:
    if node is None or node == "keep":
        return KEEP
    if isinstance(node, RefTag):
        if node.name not in values:
            msg = f"unknown value ref: {node.name!r}"
            raise ConfigError(msg)

        return values[node.name]
    # `bool` is a subclass of `int` in Python, so this must be rejected
    # explicitly before `int(node)` -- otherwise `position: true` silently
    # becomes `1`, driving a blind to 1% instead of failing loudly.
    if isinstance(node, bool):
        msg = f"action axis must be an integer 0..100, got {node!r}"
        raise ConfigError(msg)

    if isinstance(node, float):
        # An integral float (`50.0`) is what a human writes by hand in YAML
        # and is accepted; a non-integral one (`50.5`) has no meaning for a
        # cover position/tilt and must not be silently truncated.
        if not node.is_integer():
            msg = f"action axis must be an integer 0..100, got {node!r}"
            raise ConfigError(msg)

        node = int(node)
    try:
        number = int(node)
    except (TypeError, ValueError) as err:
        msg = f"cannot read action axis: {node!r}"
        raise ConfigError(msg) from err
    if not _AXIS_MIN <= number <= _AXIS_MAX:
        msg = f"action axis must be 0..100, got {number}"
        raise ConfigError(msg)

    return number


def _parse_action(node: Any, values: dict[str, Ref]) -> Action:
    node = _expect_mapping(node, "action")
    _check_keys(node, _ACTION_KEYS, "action")
    return Action(
        position=_parse_axis(node.get("position"), values),
        tilt=_parse_axis(node.get("tilt"), values),
    )


def _expect_str(node: Any, where: str) -> str:
    if not isinstance(node, str):
        msg = f"{where} must be a string, got {node!r}"
        raise ConfigError(msg)

    return node


def _parse_seconds(node: Any, where: str) -> int:
    """A whole, non-negative number of seconds.

    `bool` is rejected before `int()` for the same reason `_parse_axis`
    rejects it: `True` is an `int` in Python, so `max_wait: yes` would
    otherwise become a one-second wait instead of failing loudly.
    """
    if isinstance(node, bool):
        msg = f"{where} must be a whole number of seconds, got {node!r}"
        raise ConfigError(msg)

    if isinstance(node, float) and not node.is_integer():
        msg = f"{where} must be a whole number of seconds, got {node!r}"
        raise ConfigError(msg)

    try:
        seconds = int(node)
    except (TypeError, ValueError) as err:
        msg = f"{where} must be a whole number of seconds, got {node!r}"
        raise ConfigError(msg) from err
    if seconds < 0:
        msg = f"{where} must not be negative, got {seconds}"
        raise ConfigError(msg)

    return seconds


def _parse_guard(item: Any, conditions: dict[str, Any], values: dict[str, Ref]) -> Guard:
    """One `guards:` entry into a frozen `Guard`.

    Shape only. Whether `policy` names a policy that exists, whether a
    `defer` said what to do on timeout, whether `targets` name anything real
    -- all of that is `validation.py`'s, so a house with one questionable
    guard still loads and can be repaired through the UI instead of failing
    to parse at all. The split is the same one the condition dialect already
    draws: `_parse_condition` accepts any dict, `validation._check_condition_
    shape` is what says `condition: nonsense` is wrong.

    The one exception is `recheck_every` for a `defer`, which is filled in
    here when absent: restart resilience is a property of the guard, and a
    runner reading `guard.recheck_every` must never have to re-derive it or
    fall back to a default of its own. See `const.GUARD_DEFAULT_RECHECK`.
    """
    item = _expect_mapping(item, "guard entry")
    _check_keys(item, _GUARD_KEYS, "guard entry")
    if "policy" not in item:
        msg = f"guard without 'policy': {item!r}"
        raise ConfigError(msg)

    targets = _expect_list(item.get("targets") or [], "guard 'targets'")
    for target in targets:
        _expect_str(target, "guard target")

    raw_max_wait = item.get("max_wait", UNSET)
    max_wait = (
        raw_max_wait
        if raw_max_wait is UNSET or raw_max_wait is None
        else _parse_seconds(raw_max_wait, "guard 'max_wait'")
    )

    raw_recheck = item.get("recheck_every")
    recheck_every = (
        None if raw_recheck is None else _parse_seconds(raw_recheck, "guard 'recheck_every'")
    )

    policy = _expect_str(item["policy"], "guard 'policy'")
    if recheck_every is None and policy == GUARD_DEFER:
        recheck_every = GUARD_DEFAULT_RECHECK

    guard_then = item.get("then")
    return Guard(
        policy=policy,
        when=_parse_condition(item.get("when"), conditions),
        targets=tuple(targets),
        # An absent `applies_to`/`stage` falls back to the widest, least
        # surprising reading: a guard that applies to every movement, and one
        # that judges the engine's answer rather than silently removing the
        # target from the question. Both defaults can only make a guard fire
        # more often than its author wrote, never less -- unlike
        # `on_timeout`, where the two candidates are opposites and neither
        # can be defaulted to safely.
        applies_to=_expect_str(item["applies_to"], "guard 'applies_to'")
        if "applies_to" in item
        else GUARD_ANY,
        stage=_expect_str(item["stage"], "guard 'stage'")
        if "stage" in item
        else GUARD_STAGE_OUTPUT,
        max_wait=max_wait,
        on_timeout=_expect_str(item["on_timeout"], "guard 'on_timeout'")
        if "on_timeout" in item
        else None,
        recheck_every=recheck_every,
        then=None if guard_then is None else _parse_action(guard_then, values),
        name=_expect_str(item.get("name", ""), "guard 'name'"),
    )


def parse_guards(raw: Any, conditions: dict[str, Any], values: dict[str, Ref]) -> tuple[Guard, ...]:
    """Every `guards:` entry, in written order -- order is first-match-wins meaning.

    Public because `config_store.py` reads the identical list out of
    `entry.data["guards"]` and must build it the same way; the alternative
    (a second guard parser on the subentry side) is the drift this project
    already paid for once with rule ordering.
    """
    return tuple(
        _parse_guard(item, conditions, values) for item in _expect_list(raw or [], "'guards'")
    )


def _parse_rule(item: Any, conditions: dict[str, Any], values: dict[str, Ref]) -> Rule:
    item = _expect_mapping(item, "rule entry")
    _check_keys(item, _RULE_KEYS, "rule entry")
    if "then" not in item:
        msg = f"rule without 'then': {item!r}"
        raise ConfigError(msg)

    events = item.get("events")
    return Rule(
        then=_parse_action(item["then"], values),
        when=_parse_condition(item.get("if"), conditions),
        events=None if events is None else frozenset(events),
        name=item.get("name", ""),
    )


def walk_condition_nodes(node: Any) -> Iterator[dict]:
    """Yield `node` and every condition nested under its `conditions` list.

    Shared by `referenced_entities` below and by `tests/scenarios.py`'s
    scenario derivation -- moved here (out of the test tree) so both walk the
    same tree the same way and cannot drift apart. `node` may be `None` (no
    condition), a single condition dict, or a list of condition dicts (the
    top-level shape a mode's `when` or a rule's `if` can take before
    `_parse_condition` wraps a bare list in `and`); yields nothing for `None`.
    """
    if isinstance(node, list):
        for child in node:
            yield from walk_condition_nodes(child)
    elif isinstance(node, dict):
        yield node
        for child in node.get("conditions", []):
            yield from walk_condition_nodes(child)


def all_condition_nodes(config: Config) -> Iterator[dict]:
    """Every condition node `config` can evaluate.

    Not only the `conditions:` section: a condition written inline in a
    mode's `when`, a rule's `if` or a guard's `when` is just as real, and the
    entities it reads need to be subscribed to just the same. Reading only
    named conditions works by accident in a config that routes everything
    through `!ref`, and silently under-covers one that does not.

    A guard's `when` is here for a sharper reason than tidiness: the
    coordinator subscribes to exactly what `referenced_entities` reports, so
    a door sensor named only by a guard and by nothing else would never wake
    the integration up -- the guard would be re-examined only when some
    unrelated entity happened to change, which for an interlock is
    indistinguishable from it not being there.
    """
    for cond in config.conditions.values():
        yield from walk_condition_nodes(cond)
    for mode in config.modes:
        if mode.when is not None:
            yield from walk_condition_nodes(mode.when)
    for rules in config.rules.values():
        for rule in rules:
            if rule.when is not None:
                yield from walk_condition_nodes(rule.when)
    for guard in config.guards:
        if guard.when is not None:
            yield from walk_condition_nodes(guard.when)


@dataclass(frozen=True, slots=True)
class Read:
    """One entity or attribute read, and whether the reading node answers it itself.

    `defaulted` is why this type exists rather than a bare id: a condition that
    writes `default:` has already said what a missing value means, so a reader
    asking "was this readable" must not treat it as a fault. `readiness.py` is
    that reader; see `docs/rationale.md` -- "Why a stated `default:` is not a
    readiness fault".
    """

    entity: str
    attribute: str | None = None
    defaulted: bool = False

    @property
    def key(self) -> str | tuple[str, str]:
        """This read as `referenced_entities` reports it: an id, or `(id, attribute)`."""
        return self.entity if self.attribute is None else (self.entity, self.attribute)


def referenced_entities(config: Config) -> set[str | tuple[str, str]]:
    """Every entity `config` reads, for subscribing a coordinator to exactly that set.

    A plain `entity_id` string means a state read; an `(entity_id, attribute)`
    tuple means an attribute read -- mirroring how `conditions.py` reads each
    one (`World.state` vs. `World.attribute`/`World.number(..., attribute=)`).
    A projection of `referenced_reads` and never a second walk: subscribing to
    an entity and judging whether it was readable must not be able to disagree
    about what the config reads.
    """
    return {read.key for read in referenced_reads(config)}


def referenced_reads(config: Config) -> set[Read]:
    """Every read `config` performs, each carrying whether its own node defaults it.

    Covers:
    - every `state` / `numeric_state` condition reached by `all_condition_nodes`
      (named, or written inline in a mode's `when`, a rule's `if` or a guard's
      `when`), honouring an optional `attribute`;
    - the `sun_entity` and `azimuth_entity` a `sun_hits_target` condition reads,
      per-condition overrides included -- a config may use several different
      overrides, and each gets its own entry, not just the last;
    - `azimuth_attribute` (issue #5): when set, the azimuth entity is read as
      an attribute, not a state, and is collected as a tuple accordingly;
    - the helper entities behind `values:` refs, always a state read (see
      `engine._resolve_value`, which calls `world.number(value.entity, ...)`
      with no `attribute`).

    A `template` condition's `value_template` is not parsed for entity
    references -- Jinja templates are an intentional escape hatch (see
    `conditions._template`) and are not enumerable the way the structured
    condition dialect is.

    A `values:` helper is reported undefaulted even though `values:` requires a
    `default` -- that fallback resolves to a *position*, so acting on it is the
    house moving on a world nobody read, which is the opposite of an answer that
    avoids a movement. See `docs/rationale.md` -- "Why a `values:` default is
    not an answer".
    """
    out: set[Read] = set()
    for node in all_condition_nodes(config):
        out |= node_reads(node)
    for ref in config.values.values():
        out.add(Read(ref.entity))
    return out


def node_reads(node: Mapping[str, Any]) -> set[Read]:
    """What one condition node reads, and whether the node itself defaults each read.

    Split out of `referenced_reads` so `readiness.py` can ask the same question
    about *one* subtree (which entities does this blind's own decision read)
    without a second implementation of "which entities does a condition node
    read" -- the drift `MODELS.md` §9 records this project already being bitten
    by once with the rule-grouping sort. The `defaulted` flag is here for the
    same reason: `readiness.py` must not re-derive from a node whether that node
    tolerates a missing value.

    Reads nothing recursively: a nested `conditions:` list is `walk_condition_nodes`'
    job and a `!ref` is the caller's, since only a caller holding the `Config`
    can resolve a name to a body.
    """
    out: set[Read] = set()
    # One flag per node, not per read: `default:` is the node's own statement of
    # what a missing value means, and it covers every entity that node reads.
    defaulted = "default" in node

    def add(entity: str, attribute: str | None = None) -> None:
        out.add(Read(entity, attribute, defaulted))

    kind = node.get("condition")
    if kind in ("state", "numeric_state"):
        entity = node.get("entity_id")
        if entity is not None:
            add(entity, node.get("attribute"))
    elif kind == COND_SUN_HITS_TARGET:
        add(node.get("sun_entity", SUN_ENTITY))
        add(node.get("azimuth_entity", DEFAULT_AZIMUTH_ENTITY), node.get("azimuth_attribute"))
    return out
