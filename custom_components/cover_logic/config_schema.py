"""Parse the YAML configuration into frozen model objects.

This is also the import/export format: what the UI writes, what the tests read.
There is exactly one representation of the rules, so config and tests can never
drift apart.
"""

from pathlib import Path
from typing import Any

import yaml

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
