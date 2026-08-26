"""Build the same frozen `Config` that `config_schema.load_config` builds.

Builds it from Home Assistant config subentries instead of YAML text.

`config_schema.py` parses one YAML document with up to seven top-level keys.
Six of them are naturally "many small items" -- a list or a mapping of
individually-editable entries -- and each becomes its own subentry type here:
`blind`, `zone`, `mode`, `condition`, `value`, `rule`. The seventh, `guards`,
is not one of them: its schema is not settled (see `MODELS.md` §4 /
`docs/rationale.md`), so it is never modelled as individual subentry rows --
it is carried through unchanged from `entry.data["guards"]`, exactly as
`config_schema.load_config` carries it through unchanged from the YAML
top-level `guards:` key.

**Ordering.** A `rule` subentry carries an explicit integer `order`, and so
does a `mode` subentry. Home Assistant subentries are a flat list with no
native reordering, but both rule resolution and mode resolution are
first-match-wins -- order *is* the behaviour, not presentation -- so this
module sorts by `order` within each group before building the tuples
`Config.modes` and `Config.rules` hold. Two rule subentries claiming the same
`order` in the same `(mode, zone)` group is not resolved by a silent pick
here; see `duplicate_rule_order_problems` below, which must run *before*
that tie is folded into a tuple and becomes unrecoverable.

**Refs.** YAML has the `!ref <name>` tag to point an action axis at a
`values:` entry or a condition slot at a `conditions:` entry. Subentry data
is plain, JSON-shaped data with no such tag, so this module's own convention
stands in for it: a bare mapping of exactly one key, `{"ref": "<name>"}`,
wherever a literal, a condition body, or `"keep"` would otherwise go.
`_to_reftag` turns every such marker into a `config_schema.RefTag` before
handing the tree to `config_schema`'s own `_parse_condition`/`_parse_action`
-- the same functions the YAML path uses -- so ref resolution, range
checking and every other parsing rule are reused unchanged, never
re-implemented a second time. (See `docs/rationale.md`'s design principle:
one representation, not two that can drift.)

**Duck-typed on purpose, not Home Assistant-typed.** This module works
against anything exposing `.data` (a mapping) and `.subentries` (a mapping of
id -> object with `.subentry_type: str` and `.data: Mapping`) -- exactly the
shape of `homeassistant.config_entries.ConfigEntry`/`ConfigSubentry` -- but
never imports `homeassistant` to say so. That keeps it in the pure, no-HA
half of the split `tests/test_purity.py` enforces (see `MODELS.md` §9), so
its own tests run in the fast suite instead of needing `tests/ha/` and a
running Home Assistant type.
"""

from typing import Any

from .config_schema import (
    ConfigError,
    RefTag,
    _parse_blind,
    _parse_condition,
    _parse_rule as _yaml_parse_rule,
    _parse_values,
    _reject_dot,
)
from .model import Config, Mode, Zone
from .validation import Problem, check_duplicate_rule_order

BLIND = "blind"
ZONE = "zone"
MODE = "mode"
CONDITION = "condition"
VALUE = "value"
RULE = "rule"

SUBENTRY_TYPES = frozenset({BLIND, ZONE, MODE, CONDITION, VALUE, RULE})

# The single per-item key that names a zone, mode, condition or value --
# playing the role the YAML mapping key (`zones: {<this>: {...}}`) plays
# there. Rule subentries have no such key of their own: their identity is
# `(mode, zone, order)`, not a name.
_ID_KEY = "id"


def _to_reftag(node: Any) -> Any:
    """Turn every `{"ref": "<name>"}` marker in `node` into a `RefTag`, recursively.

    See the module docstring's "Refs" section for why this exists instead of
    a second ref-resolution implementation.
    """
    if isinstance(node, dict):
        if set(node) == {"ref"} and isinstance(node["ref"], str):
            return RefTag(node["ref"])
        return {key: _to_reftag(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_to_reftag(item) for item in node]
    return node


def _check_types(entry: Any) -> None:
    unknown = {s.subentry_type for s in entry.subentries.values()} - SUBENTRY_TYPES
    if unknown:
        msg = f"unknown subentry type(s) {sorted(unknown)}"
        raise ConfigError(msg)


def _of_type(entry: Any, kind: str) -> list[dict]:
    """Every subentry `.data` of type `kind`, in `entry.subentries` iteration order."""
    return [
        dict(subentry.data)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == kind
    ]


def _require(data: dict, key: str, where: str) -> Any:
    if key not in data:
        msg = f"{where} without {key!r}: {data!r}"
        raise ConfigError(msg)
    return data[key]


def _order(data: dict, where: str) -> int:
    raw = _require(data, "order", where)
    try:
        return int(raw)
    except (TypeError, ValueError) as err:
        msg = f"{where} 'order' must be an integer, got {raw!r}"
        raise ConfigError(msg) from err


def _build_blinds(entry: Any) -> dict[str, Any]:
    blinds = {}
    for data in _of_type(entry, BLIND):
        blind = _parse_blind(data)
        blinds[blind.entity] = blind
    return blinds


def _build_zones(entry: Any) -> dict[str, Zone]:
    zones = {}
    for data in _of_type(entry, ZONE):
        zone_id = _require(data, _ID_KEY, "zone subentry")
        _reject_dot(zone_id, "zone id")
        zones[zone_id] = Zone(
            id=zone_id,
            members=tuple(data.get("members") or ()),
            occupants=tuple(data.get("occupants") or ()),
        )
    return zones


def _build_conditions(entry: Any) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return `(raw_conditions, conditions)`.

    `raw_conditions` (name -> unparsed body) is the lookup namespace ref
    resolution checks membership against -- mirroring `load_config`'s own
    `raw_conditions` variable, reused for the same purpose in mode/rule
    parsing below.
    """
    raw_conditions = {}
    for data in _of_type(entry, CONDITION):
        name = _require(data, _ID_KEY, "condition subentry")
        raw_conditions[name] = {k: v for k, v in data.items() if k != _ID_KEY}

    conditions = {
        name: _parse_condition(_to_reftag(body), raw_conditions)
        for name, body in raw_conditions.items()
    }
    return raw_conditions, conditions


def _build_values(entry: Any) -> dict[str, Any]:
    raw = {}
    for data in _of_type(entry, VALUE):
        name = _require(data, _ID_KEY, "value subentry")
        raw[name] = {k: v for k, v in data.items() if k != _ID_KEY}
    return _parse_values(raw)


def _build_modes(entry: Any, raw_conditions: dict[str, dict]) -> tuple[Mode, ...]:
    items = _of_type(entry, MODE)
    items.sort(key=lambda data: _order(data, "mode subentry"))
    modes = []
    for data in items:
        mode_id = _require(data, _ID_KEY, "mode subentry")
        _reject_dot(mode_id, "mode id")
        when = _parse_condition(_to_reftag(data.get("when")), raw_conditions)
        modes.append(Mode(id=mode_id, when=when))
    return tuple(modes)


def _rule_body(data: dict) -> dict:
    """Strip `mode`/`zone`/`order` off a rule subentry, leaving what `_parse_rule` expects."""
    return {k: v for k, v in data.items() if k in ("if", "then", "events", "name")}


def _rule_groups(entry: Any) -> dict[str, list[dict]]:
    """Every rule subentry's `.data`, grouped by `"<mode>.<zone>"`, insertion order preserved."""
    groups: dict[str, list[dict]] = {}
    for data in _of_type(entry, RULE):
        mode = _require(data, "mode", "rule subentry")
        zone = _require(data, "zone", "rule subentry")
        _order(data, "rule subentry")  # validated eagerly; value read again in the sort key
        key = f"{mode}.{zone}"
        groups.setdefault(key, []).append(data)
    return groups


def _build_rules(
    entry: Any, raw_conditions: dict[str, dict], values: dict[str, Any]
) -> dict[str, tuple]:
    rules = {}
    for key, items in _rule_groups(entry).items():
        ordered = sorted(items, key=lambda data: _order(data, "rule subentry"))
        rules[key] = tuple(
            _yaml_parse_rule(_to_reftag(_rule_body(data)), raw_conditions, values)
            for data in ordered
        )
    return rules


def config_from_subentries(entry: Any) -> Config:
    """Build a frozen `Config` from `entry.subentries`, the shape `load_config` builds from YAML.

    Raises `config_schema.ConfigError` on the same class of problem
    `load_config` raises on -- a required key missing, an out-of-range
    action axis, an unknown subentry type -- since both are, structurally,
    the same parse. `validate()` (run separately by the caller, exactly as
    the YAML path already does -- see `__init__.py`/`config_flow.py`) is
    still the place for everything that is valid shape but bad config
    (an orphaned blind, an unreachable rule, ...); this function does not
    call it. `duplicate_rule_order_problems`, below, covers the one thing
    `validate()` cannot see once this function returns -- see its docstring.
    """
    _check_types(entry)

    raw_conditions, conditions = _build_conditions(entry)
    values = _build_values(entry)

    return Config(
        blinds=_build_blinds(entry),
        zones=_build_zones(entry),
        modes=_build_modes(entry, raw_conditions),
        rules=_build_rules(entry, raw_conditions, values),
        conditions=conditions,
        values=values,
        guards=tuple((entry.data or {}).get("guards") or ()),
    )


def duplicate_rule_order_problems(entry: Any) -> list[Problem]:
    """Problems visible only at the subentry layer: two rules sharing an `order`.

    Call this alongside `validate(config_from_subentries(entry))` -- together
    they are this source's complete set of checks, just as
    `validate(load_config(text))` is complete for the YAML source. This
    cannot be folded into `validate()` itself: `Config.rules` is a plain
    tuple that no longer carries `order` once built (see `check_
    duplicate_rule_order`'s own docstring), so this must run over
    `entry.subentries` directly, before that information is lost.
    """
    orders: dict[str, list[int]] = {}
    for data in _of_type(entry, RULE):
        mode = _require(data, "mode", "rule subentry")
        zone = _require(data, "zone", "rule subentry")
        order = _order(data, "rule subentry")
        orders.setdefault(f"{mode}.{zone}", []).append(order)
    return check_duplicate_rule_order(orders)
