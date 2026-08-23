"""Parse the YAML configuration into frozen model objects.

This is also the import/export format: what the UI writes, what the tests read.
There is exactly one representation of the rules, so config and tests can never
drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .model import KEEP, Action, Blind, Config, Mode, Ref, Rule, Value, Zone


class ConfigError(Exception):
    """Raised when the configuration cannot be turned into a valid Config."""


class RefTag:
    """Placeholder produced by the `!ref` YAML tag.

    Interpreted by context: inside `if:` it names a condition, inside an action
    axis it names a value. The two namespaces are separate on purpose.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"RefTag({self.name!r})"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor(
    "!ref", lambda loader, node: RefTag(loader.construct_scalar(node))
)


def load_config(text: str) -> Config:
    raw = yaml.load(text, Loader=_Loader) or {}
    conditions = raw.get("conditions") or {}
    values = _parse_values(raw.get("values") or {})

    blinds = {}
    for item in raw.get("blinds") or []:
        blind = _parse_blind(item)
        blinds[blind.entity] = blind

    zones = {
        zone_id: Zone(
            id=zone_id,
            members=tuple(body.get("members") or ()),
            occupants=tuple(body.get("occupants") or ()),
        )
        for zone_id, body in (raw.get("zones") or {}).items()
    }

    modes = tuple(
        Mode(id=item["id"], when=_parse_condition(item.get("when"), conditions))
        for item in raw.get("modes") or []
    )

    rules = {
        key: tuple(_parse_rule(item, conditions, values) for item in items)
        for key, items in (raw.get("rules") or {}).items()
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
    return load_config(Path(path).read_text(encoding="utf-8"))


def _parse_values(raw: dict[str, Any]) -> dict[str, Ref]:
    out: dict[str, Ref] = {}
    for name, body in raw.items():
        try:
            out[name] = Ref(entity=body["entity"], default=int(body["default"]))
        except (KeyError, TypeError, ValueError) as err:
            raise ConfigError(f"value {name!r} needs 'entity' and integer 'default'") from err
    return out


def _parse_blind(item: dict[str, Any]) -> Blind:
    if "entity" not in item:
        raise ConfigError(f"blind without 'entity': {item!r}")
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
            raise ConfigError(f"unknown condition ref: {node.name!r}")
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
    raise ConfigError(f"cannot read condition: {node!r}")


def _parse_axis(node: Any, values: dict[str, Ref]) -> Value:
    if node is None or node == "keep":
        return KEEP
    if isinstance(node, RefTag):
        if node.name not in values:
            raise ConfigError(f"unknown value ref: {node.name!r}")
        return values[node.name]
    try:
        number = int(node)
    except (TypeError, ValueError) as err:
        raise ConfigError(f"cannot read action axis: {node!r}") from err
    if not 0 <= number <= 100:
        raise ConfigError(f"action axis must be 0..100, got {number}")
    return number


def _parse_action(node: Any, values: dict[str, Ref]) -> Action:
    if not isinstance(node, dict):
        raise ConfigError(f"action must be a mapping, got {node!r}")
    return Action(
        position=_parse_axis(node.get("position"), values),
        tilt=_parse_axis(node.get("tilt"), values),
    )


def _parse_rule(
    item: dict[str, Any], conditions: dict[str, Any], values: dict[str, Ref]
) -> Rule:
    if "then" not in item:
        raise ConfigError(f"rule without 'then': {item!r}")
    events = item.get("events")
    return Rule(
        then=_parse_action(item["then"], values),
        when=_parse_condition(item.get("if"), conditions),
        events=None if events is None else frozenset(events),
        name=item.get("name", ""),
    )
