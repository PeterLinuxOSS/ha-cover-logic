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
`Config.modes` and `Config.rules` hold. The sort key is `(order, subentry
id)`, not `order` alone: `entry.subentries` is a mapping, and nothing in
Home Assistant's contract promises it preserves insertion order (a storage
round-trip is free to reorder it), so relying on that order as an implicit
tiebreaker would make the result depend on something this module cannot
see or pin down. The subentry id is stable and unique, so the explicit
tiebreaker makes the sort total -- its result cannot vary run to run for
the same subentries. Two rule subentries claiming the same `order` in the
same `(mode, zone)` group still is not a silent pick worth keeping,
though: see `duplicate_rule_order_problems` below, which must run *before*
the tie is folded into a tuple and the `order` that caused it is gone.

**One grouping, not two.** `_grouped_rules` below is the single place that
groups rule subentries by `(mode, zone)` and sorts them; `_build_rules`
(which becomes `Config.rules`) and `rule_owner_ids` (which names each
subentry's position in that same tuple for `validate()` and `subentry_flow`
to point at) both read its output rather than each running their own copy
of the grouping. A second, hand-mirrored copy of the same sort was tried
first and rejected: nothing forces two copies to move together, so a
change to one without the other silently breaks the equivalence
`rule_owner_ids` depends on, in a way the 92,160-scenario migration gate
cannot see -- that gate exercises what the engine decides, not which
subentry a validation problem is attributed to.

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
    _blind_to_dict,
    _mode_to_dict,
    _parse_blind,
    _parse_condition,
    _parse_rule as _yaml_parse_rule,
    _parse_values,
    _reject_dot,
    _reject_zone_id,
    _rule_to_dict,
    _zone_to_dict,
    unparse_condition,
)
from .const import RULE_DEFAULT_ZONE
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
        _reject_zone_id(zone_id, "zone id")
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


def _grouped_rules(entry: Any) -> dict[str, list[tuple[str, dict]]]:
    """Every rule subentry as `(subentry id, data)`, grouped by `(mode, zone)` and sorted.

    The one place that groups-and-sorts rule subentries; see the module
    docstring's "One grouping, not two" section for why every other
    function that needs this shape -- `_build_rules`, `rule_owner_ids`,
    `duplicate_rule_order_problems` -- reads this function's output instead
    of grouping and sorting again on its own. That makes it structurally
    impossible for `Config.rules`'s order and `rule_owner_ids`'s indices to
    disagree: they are built by iterating the exact same list.

    The `(order, subentry id)` sort key -- not `order` alone -- is what
    makes the sort total; see the module docstring's "Ordering" section.
    """
    groups: dict[str, list[tuple[str, dict]]] = {}
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != RULE:
            continue
        data = dict(subentry.data)
        mode = _require(data, "mode", "rule subentry")
        zone = _require(data, "zone", "rule subentry")
        _order(data, "rule subentry")  # validated eagerly; value read again in the sort key
        groups.setdefault(f"{mode}.{zone}", []).append((subentry_id, data))
    for items in groups.values():
        items.sort(key=lambda item: (_order(item[1], "rule subentry"), item[0]))
    return groups


def effective_rule_items(entry: Any, mode_id: str, zone_id: str) -> list[tuple[str, dict, bool]]:
    """Every rule subentry deciding `zone_id`'s blinds under `mode_id`, as `(id, data, is_default)`.

    Own rules first, then the mode's shared defaults, each tagged whether it
    is inherited -- the same "own list runs to completion, only then fall
    back to the mode's defaults" rule `engine._apply_rules` evaluates over
    parsed `Rule` tuples (`Config.rules`), applied one layer up over raw
    subentry data instead. Built from `_grouped_rules(entry)` -- the single
    place that groups and sorts rule subentries -- by reading the zone's own
    key and the mode's default key (`f"{mode_id}.{RULE_DEFAULT_ZONE}"`) and
    concatenating the two lists in that order. Neither list is re-sorted:
    `_grouped_rules` already sorted each one internally, so this is a plain
    concatenation, not a second sort -- see that function's own "One
    grouping, not two" docstring section for why a second sort is exactly
    the mistake this project has already paid for once.

    `options_flow.py`'s per-zone rule screen is the one caller today, but
    this lives here (not in `options_flow.py` itself) so a future second
    presentation of "what does this zone actually decide with" reads the
    same definition instead of re-deriving the concatenation a third time
    (`engine._apply_rules` and `validation._check_rule_lists` are the other
    two places this exact sequence already exists, both over `Config`-level
    `Rule` tuples that carry no subentry id -- this is the subentry-level
    counterpart the UI needs).

    `zone_id == RULE_DEFAULT_ZONE` views a mode's default list on its own
    terms: there is no further default to fall back to, so every row comes
    back tagged `False` -- inherited is relative to a *real* zone, not to
    itself.
    """
    groups = _grouped_rules(entry)
    own = groups.get(f"{mode_id}.{zone_id}", [])
    items: list[tuple[str, dict, bool]] = [(sid, data, False) for sid, data in own]
    if zone_id != RULE_DEFAULT_ZONE:
        defaults = groups.get(f"{mode_id}.{RULE_DEFAULT_ZONE}", [])
        items += [(sid, data, True) for sid, data in defaults]
    return items


def _build_rules(
    entry: Any, raw_conditions: dict[str, dict], values: dict[str, Any]
) -> dict[str, tuple]:
    return {
        key: tuple(
            _yaml_parse_rule(_to_reftag(_rule_body(data)), raw_conditions, values)
            for _subentry_id, data in items
        )
        for key, items in _grouped_rules(entry).items()
    }


def config_from_subentries(entry: Any) -> Config:
    """Build a frozen `Config` from `entry.subentries`, the shape `load_config` builds from YAML.

    Raises `config_schema.ConfigError` on the same class of problem
    `load_config` raises on -- a required key missing, an out-of-range
    action axis, an unknown subentry type -- since both are, structurally,
    the same parse. `validate()` (run separately by the caller, exactly as
    the YAML path already does -- see `__init__.py`/`config_flow.py`/
    `subentry_flow.py`) is still the place for everything that is valid shape
    but bad config (an orphaned blind, an unreachable rule, ...); this
    function does not call it. `duplicate_rule_order_problems`, below, covers
    the one thing `validate()` cannot see once this function returns -- see
    its docstring.
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


def rule_owner_ids(entry: Any) -> dict[str, str]:
    """Map each rule subentry's id to the `"<mode>.<zone>#<index>"` string naming it.

    That string is how `validation` attributes a rule's problems (see
    `validation._rule_owner`) and how `engine._apply_rules` labels a decision,
    but neither can be handed a Home Assistant subentry id -- both work off a
    `Config`, where subentry ids no longer exist. This is the one place the
    two identities are tied together, so `subentry_flow` can ask "is the
    problem `validate()` just reported about the very subentry being saved?"
    without re-deriving the sort a second time.

    The index enumerates `_grouped_rules(entry)[key]` -- the exact list
    `_build_rules` turns into `Config.rules[key]` -- so a subentry's id here
    names the same position it occupies there by construction, not by two
    sorts having been written to agree.

    Raises `ConfigError` for the same reasons `config_from_subentries` does
    (a rule subentry missing `mode`, `zone` or a valid `order`); a caller that
    cannot tolerate that should call it only after the config has parsed.
    """
    return {
        subentry_id: f"{key}#{index}"
        for key, items in _grouped_rules(entry).items()
        for index, (subentry_id, _data) in enumerate(items)
    }


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
    return check_duplicate_rule_order(
        {
            key: [
                (f"{key}#{index}", _order(data, "rule subentry"))
                for index, (_subentry_id, data) in enumerate(items)
            ]
            for key, items in _grouped_rules(entry).items()
        }
    )


# ---------------------------------------------------------------------------
# `subentries_from_config`: the write side, used by the `import_config`
# service (`services.py`) to turn an imported YAML `Config` into the
# subentries it writes. The inverse of `config_from_subentries` above, the
# same way `config_schema.dump_config` is the inverse of `load_config` --
# together the two write sides make both doors into `Config` (YAML, and now
# subentries) round-trip, not just the read side that already had to.
# ---------------------------------------------------------------------------

# The gap left between one rule's or mode's assigned `order` and the next --
# matches `subentry_flow._ORDER_GAP`, for the same reason: a user who starts
# hand-editing a rule set an import produced can still slip a new row between
# two existing ones without renumbering either. `Config.rules`/`Config.modes`
# hold no `order` of their own (see `duplicate_rule_order_problems`'s own
# docstring) -- only their tuple *position* survives -- so any strictly
# increasing sequence reproduces that position; this just picks one a human
# editing the result afterwards will not immediately need to touch.
_ORDER_STEP = 10


def _ref_marker(name: str) -> dict[str, str]:
    """The `{"ref": "<name>"}` marker this module's own docstring describes.

    Passed as `unparse_condition`/`unparse_axis`'s `ref_factory` wherever this
    module builds subentry data -- never `config_schema.RefTag` (the YAML
    spelling), which `_to_reftag` above exists specifically to translate back
    from. Using this marker instead of writing the already-parsed
    `{"condition": "ref", "name": ...}` shape (as `subentry_flow.py`'s own
    `mode`/`rule` forms do, for reasons specific to that UI -- see
    `ModeSubentryFlowHandler._to_data`'s docstring) keeps a subentry an
    imported `import_config` writes indistinguishable from one a human built
    by hand through those same forms picking a named condition or value from
    a dropdown: both spellings parse identically (`_to_reftag` turns this one
    into a `RefTag`, whose eager membership check is exactly what
    `config_from_subentries`/`_build_rules` already run for a freshly
    imported config, so there is no unrelated-save-blocking hazard here the
    way there is for an *edit* through the flow, which is what that decision
    was actually about), but only one of the two is what `config_store.py`'s
    own module docstring calls "this module's own convention".
    """
    return {"ref": name}


def _condition_body_to_data(name: str, body: Any) -> dict[str, Any]:
    """A named condition's subentry `data`: `{"id": name, **flattened body}`.

    `config_store._build_conditions` merges every key of a condition
    subentry's `data` except `id` into one dict and hands it to
    `config_schema._parse_condition` unchanged -- so `body` must already be a
    single condition node (a mapping), not a bare list. `Config.conditions`
    is typed `dict[str, dict]` (see `model.Config`) for exactly this reason;
    a YAML file that names a bare list as one condition's whole body (legal
    for `load_config`, since `_parse_condition` accepts a top-level list, but
    never produced by anything in `subentry_flow.py`) cannot become a `condition`
    subentry undistorted, and this raises rather than silently wrapping it in
    an implicit `and` the user did not write.
    """
    if not isinstance(body, dict):
        msg = (
            f"condition {name!r}: only a single condition (a mapping), not a bare list, "
            f"can become a 'condition' subentry -- got {body!r}"
        )
        raise ConfigError(msg)
    return {_ID_KEY: name, **unparse_condition(body, _ref_marker)}


class _StubSubentry:
    """The minimal duck-typed subentry `config_from_subentries` needs to read back.

    Same surface as `subentry_flow._SubentryStub`, redefined here rather than
    imported from it: `subentry_flow.py` is the Home Assistant layer (imports
    `homeassistant` unconditionally, per its own module docstring), and this
    module stays on the pure side of the split `tests/test_purity.py`
    enforces -- importing from `subentry_flow` would drag that import in
    transitively the moment anything under `custom_components/cover_logic/`
    is loaded from the system-Python test run.
    """

    __slots__ = ("data", "subentry_type")

    def __init__(self, subentry_type: str, data: dict[str, Any]) -> None:
        """Store the two attributes `config_from_subentries` reads."""
        self.subentry_type = subentry_type
        self.data = data


class _StubEntry:
    """The minimal duck-typed entry `config_from_subentries` needs to read back.

    See `_StubSubentry`'s docstring for why this is not imported from
    `subentry_flow._EntryStub` instead.
    """

    __slots__ = ("data", "subentries")

    def __init__(self, data: dict[str, Any], subentries: dict[str, _StubSubentry]) -> None:
        """Store the two attributes `config_from_subentries` reads."""
        self.data = data
        self.subentries = subentries


def entry_from_subentry_items(
    items: list[tuple[str, dict[str, Any]]], guards: tuple = ()
) -> _StubEntry:
    """Build the minimal entry `config_from_subentries` can read from `(type, data)` pairs.

    Shared by `subentries_from_config`'s own round-trip self-check below and
    by `tests/test_config_store.py`'s tests for it, so neither has to
    fabricate a second stand-in for "an entry with exactly these subentries".
    """
    subentries = {str(i): _StubSubentry(kind, data) for i, (kind, data) in enumerate(items)}
    return _StubEntry(data={"guards": list(guards)}, subentries=subentries)


def subentries_from_config(config: Config) -> list[tuple[str, dict[str, Any]]]:
    """Invert `config_from_subentries`: every `(subentry_type, data)` pair that reproduces `config`.

    The write side `import_config` (`services.py`) uses to turn a parsed YAML
    `Config` into real subentries -- `hass.config_entries.async_add_subentry`
    wraps each pair in a real `ConfigSubentry`, a step this function does not
    take itself so it can stay on the pure side of the split (see
    `_StubSubentry`'s docstring) and be tested without any `homeassistant`
    import.

    Ordering: `Config.modes` and each `Config.rules[...]` tuple carry
    first-match-wins meaning (see `MODELS.md` Sec. 3) but no `order` field of
    their own -- only tuple *position* -- so this assigns a fresh, strictly
    increasing `order` (`_ORDER_STEP` apart) to each mode and to each rule
    within its `(mode, zone)` group, in exact tuple order. Because the
    assigned values are strictly increasing and never repeat within a group,
    `_grouped_rules`'/`_build_modes`'s own `(order, subentry id)` sort
    reproduces that same order regardless of what subentry ids the real
    `ConfigSubentryFlowManager` ends up assigning -- the id half of the sort
    key never has to break a tie. (The literal numeric order values chosen
    here are not preserved from wherever `config` originally came from --
    `Config` does not carry them at all once built, by construction -- so
    "preserving order" means preserving the resulting sequence, not
    reproducing specific numbers a user may have typed into a UI form.)

    Before returning, rebuilds a `Config` from exactly the pairs about to be
    handed back (via `entry_from_subentry_items`/`config_from_subentries`)
    and raises `ConfigError` if it does not equal `config`. This is a
    self-check on this function and `config_from_subentries` staying inverses
    of one another, not a check on `config` itself (which already parsed
    successfully, or there would be no `Config` to call this with) -- see the
    task report for why a mismatch here must be treated as this function's
    own bug, not the caller's.
    """
    items: list[tuple[str, dict[str, Any]]] = [
        (BLIND, _blind_to_dict(blind)) for blind in config.blinds.values()
    ]

    for zone_id, zone in sorted(config.zones.items()):
        items.append((ZONE, {_ID_KEY: zone_id, **_zone_to_dict(zone)}))

    for name, ref in sorted(config.values.items()):
        items.append((VALUE, {_ID_KEY: name, "entity": ref.entity, "default": ref.default}))

    for name, body in sorted(config.conditions.items()):
        items.append((CONDITION, _condition_body_to_data(name, body)))

    for index, mode in enumerate(config.modes):
        data = _mode_to_dict(mode, _ref_marker)
        data["order"] = index * _ORDER_STEP
        items.append((MODE, data))

    ref_names = {id(ref): name for name, ref in config.values.items()}
    for key, rules in sorted(config.rules.items()):
        mode_id, _dot, zone_id = key.partition(".")
        for index, rule in enumerate(rules):
            data = _rule_to_dict(rule, ref_names, _ref_marker)
            data["mode"] = mode_id
            data["zone"] = zone_id
            data["order"] = index * _ORDER_STEP
            items.append((RULE, data))

    rebuilt = config_from_subentries(entry_from_subentry_items(items, config.guards))
    if rebuilt != config:
        msg = (
            "subentries_from_config produced subentries that do not round-trip back to "
            "the same Config -- this is a bug in subentries_from_config/config_from_subentries "
            "itself, not a problem with the input Config"
        )
        raise ConfigError(msg)

    return items
