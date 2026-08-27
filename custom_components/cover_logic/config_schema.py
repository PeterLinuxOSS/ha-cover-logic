"""Parse the YAML configuration into frozen model objects.

This is also the import/export format: what the UI writes, what the tests read.
There is exactly one representation of the rules, so config and tests can never
drift apart.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml

from .conditions import DEFAULT_AZIMUTH_ENTITY, SUN_ENTITY
from .const import COND_REF, COND_SUN_HITS_TARGET
from .model import KEEP, Action, Blind, Config, Mode, Ref, Rule, Value, Zone


class ConfigError(Exception):
    """Raised when the configuration cannot be turned into a valid Config."""


# Key sets for the structures this file owns. Condition bodies (native HA
# condition dicts) and `guards` (schema not settled yet) are deliberately
# excluded from strict checking -- see the scoping note on `_check_keys`.
# Constants and set contents below are alphabetised; membership testing
# (`set(mapping) - allowed`) never depends on order.
_ACTION_KEYS = {"position", "tilt"}
_BLIND_KEYS = {
    "entity",
    "facade_azimuth",
    "has_tilt",
    "tilt_after_arrival",
    "tolerance",
    "travel_time",
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

    See docs/rationale.md -- "Why condition bodies and `guards` are exempt
    from strict key checking".
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
        _reject_dot(zone_id, "zone id")
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
        guards=tuple(raw.get("guards") or ()),
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
        doc["guards"] = list(config.guards)

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
    mode's `when` or a rule's `if` is just as real, and the entities it reads
    need to be subscribed to just the same. Reading only named conditions
    works by accident in a config that routes everything through `!ref`, and
    silently under-covers one that does not.
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


def referenced_entities(config: Config) -> set[str | tuple[str, str]]:
    """Every entity `config` reads, for subscribing a coordinator to exactly that set.

    A plain `entity_id` string means a state read; an `(entity_id, attribute)`
    tuple means an attribute read -- mirroring how `conditions.py` reads each
    one (`World.state` vs. `World.attribute`/`World.number(..., attribute=)`).

    Covers:
    - every `state` / `numeric_state` condition reached by `all_condition_nodes`
      (named, or written inline in a mode's `when` or a rule's `if`), honouring
      an optional `attribute`;
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
    """
    out: set[str | tuple[str, str]] = set()

    def add(entity: str, attribute: str | None = None) -> None:
        out.add(entity if attribute is None else (entity, attribute))

    for node in all_condition_nodes(config):
        kind = node.get("condition")
        if kind in ("state", "numeric_state"):
            entity = node.get("entity_id")
            if entity is not None:
                add(entity, node.get("attribute"))
        elif kind == COND_SUN_HITS_TARGET:
            add(node.get("sun_entity", SUN_ENTITY))
            add(node.get("azimuth_entity", DEFAULT_AZIMUTH_ENTITY), node.get("azimuth_attribute"))

    for ref in config.values.values():
        add(ref.entity)

    return out
