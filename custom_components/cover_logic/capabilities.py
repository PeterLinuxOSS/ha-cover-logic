"""Which `cover` features a configuration will actually need of each blind.

**Measured on a clean install, 2026-09-03.** Home Assistant's entity picker in
both the config flow and the blind subentry form filters on `domain="cover"`
and nothing else, so a demo instance happily offered `cover.garage_door`
(`supported_features` 3 -- open and close, no position, no tilt) as a blind.
Nothing refused it: the subentry saved, and the runner would have gone on
sending it `set_cover_position` and `close_cover_tilt` for as long as it
existed.

Filtering the picker was the obvious fix and was rejected: entity-selector
filters are evaluated in the frontend, so the backend cannot verify the filter
works, and a wrong feature string would silently filter nothing or everything.
This module answers the same question where it can be tested, and it catches a
blind however it arrived -- form, YAML file or `import_config`.

The requirement is derived from the values the configuration *asks for*, not
from a fixed list, because `runner._service_for_position`/`_service_for_tilt`
pick their service by value: 0 is `close_cover`, 100 is `open_cover`, and
anything between is `set_cover_position`. A blind that only opens and closes is
therefore perfectly usable by a configuration that only ever says 0 or 100 --
and saying otherwise would be a false alarm on exactly the shutters that have
no position axis at all.

A `Ref` value (a `values:` entry read at evaluation time) counts as "could be
anything", so it requires the setter. That is the honest reading: the number is
not knowable until the world is read.

No `homeassistant` import -- this is in `tests/test_purity.py`'s
`PURE_MODULES`. The feature bits below therefore *mirror*
`homeassistant.components.cover.CoverEntityFeature` rather than importing it,
and `tests/ha/test_capabilities_mirror.py` fails if the two ever disagree.
"""

from .model import Config, Ref

# Mirrors `homeassistant.components.cover.CoverEntityFeature`. Kept in sync by
# a test rather than by hope; see the module docstring.
OPEN = 1
CLOSE = 2
SET_POSITION = 4
OPEN_TILT = 16
CLOSE_TILT = 32
SET_TILT_POSITION = 128

FEATURE_NAMES = {
    OPEN: "OPEN",
    CLOSE: "CLOSE",
    SET_POSITION: "SET_POSITION",
    OPEN_TILT: "OPEN_TILT",
    CLOSE_TILT: "CLOSE_TILT",
    SET_TILT_POSITION: "SET_TILT_POSITION",
}

# The two ends of each axis, matching `runner.FULLY_CLOSED`/`FULLY_OPEN`.
_CLOSED = 0
_OPEN = 100


def _position_feature(value) -> int:
    """The bit `runner._service_for_position` will need for this value."""
    if isinstance(value, Ref):
        return SET_POSITION
    if value <= _CLOSED:
        return CLOSE
    if value >= _OPEN:
        return OPEN
    return SET_POSITION


def _tilt_feature(value) -> int:
    """The bit `runner._service_for_tilt` will need for this value."""
    if isinstance(value, Ref):
        return SET_TILT_POSITION
    if value <= _CLOSED:
        return CLOSE_TILT
    if value >= _OPEN:
        return OPEN_TILT
    return SET_TILT_POSITION


def _is_number(value) -> bool:
    """Whether this axis carries a value at all -- `KEEP` commands nothing.

    `bool` is excluded deliberately: it is an `int` subclass, and a `True`
    slipping in from a mis-parsed YAML would otherwise be read as position 1.
    """
    return isinstance(value, Ref) or (isinstance(value, int) and not isinstance(value, bool))


def required_features(config: Config) -> dict[str, int]:
    """Per blind entity, the bitmask of `cover` features this configuration needs.

    Zones are how a rule reaches a blind, so a rule's `then` is charged to
    every member of its zone -- and to every blind for a `*` default row.
    Guards are included: a `force` guard's `then` is a command like any other,
    and it is the one that fires when something has gone wrong, which is the
    worst moment to discover the blind cannot carry it out.
    """
    needed = dict.fromkeys(config.blinds, 0)

    def charge(entities, action) -> None:
        for entity in entities:
            if entity not in needed:
                continue
            if _is_number(action.position):
                needed[entity] |= _position_feature(action.position)
            if _is_number(action.tilt) and config.blinds[entity].has_tilt:
                needed[entity] |= _tilt_feature(action.tilt)

    for key, rules in config.rules.items():
        _mode, _, zone_id = key.partition(".")
        members = (
            list(config.blinds) if zone_id not in config.zones else config.zones[zone_id].members
        )
        for rule in rules:
            charge(members, rule.then)

    for guard in config.guards:
        if guard.then is None:
            continue
        targets = guard.targets or list(config.blinds)
        entities: list[str] = []
        for target in targets:
            if target in config.zones:
                entities.extend(config.zones[target].members)
            else:
                entities.append(target)
        charge(entities, guard.then)

    return needed


def missing_features(needed: int, supported: int) -> list[str]:
    """The names of the features `needed` asks for and `supported` does not have."""
    return [
        name for bit, name in sorted(FEATURE_NAMES.items()) if needed & bit and not supported & bit
    ]
