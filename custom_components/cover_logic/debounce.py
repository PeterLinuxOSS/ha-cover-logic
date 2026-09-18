"""Since when a numeric threshold has held -- the only thing the engine remembers.

`condition: state` already takes `for:`, because a bed sensor that flickers
must not move a blind. A `numeric_state` reading a noisy sensor needs it for
the same reason and could not have it: `World.since` dates the entity's last
*state change*, and a lux sensor changes value every couple of minutes whether
or not it has crossed anything. What `for:` needs there is when the
*predicate* last became true, which is what this module tracks.

See docs/rationale.md -- "Why `numeric_state` takes `for:`, and why it needs
its own memory".
"""

from collections.abc import Iterable, Mapping
import datetime as dt

from .world import World

_BOUND_KEYS = ("above", "below")


def is_debounced(cond: Mapping) -> bool:
    """Whether this condition states a dwell, and so needs to be remembered."""
    return cond.get("condition") == "numeric_state" and "for" in cond


def threshold_key(cond: Mapping) -> str:
    """What the memory is keyed by: the reading and its bounds, not where it is written.

    Two identically written conditions share one entry deliberately -- they
    would give the same answer anyway, and keying by config location would
    restart a dwell whenever a rule was reordered.
    """
    parts = [str(cond["entity_id"]), str(cond.get("attribute") or "")]
    parts += [f"{key}={cond[key]}" for key in _BOUND_KEYS if key in cond]
    return "|".join(parts)


def crosses(cond: Mapping, value: float) -> bool:
    """The plain threshold test, with no memory -- what `numeric_state` always meant."""
    # Two independent bounds, either of which may be absent; kept symmetrical
    # for the same reason `conditions._numeric_state` states it this way.
    if "above" in cond and not value > float(cond["above"]):
        return False
    if "below" in cond and not value < float(cond["below"]):  # noqa: SIM103
        return False
    return True


def resolve(
    nodes: Iterable[Mapping],
    world: World,
    previous: Mapping[str, dt.datetime | None],
) -> dict[str, dt.datetime | None]:
    """When each debounced threshold last became true, `None` if it is not true now.

    Resolved once from the finished snapshot, before anything decides, so the
    rules, the guards and the readiness gate cannot read different answers
    within one evaluation.

    Every debounced condition gets an entry, so a missing key means "no memory
    at all" rather than "not true" -- which is what lets `for:` fall back to
    the plain threshold in the pure tests and in the migration gate.
    """
    out: dict[str, dt.datetime | None] = {}
    for cond in nodes:
        if not is_debounced(cond):
            continue
        key = threshold_key(cond)
        if key in out:
            continue
        value = world.number(
            cond["entity_id"],
            default=float(cond["default"]),
            attribute=cond.get("attribute"),
        )
        if not crosses(cond, value):
            out[key] = None
            continue
        # Keep the moment it first became true: a dwell measures one unbroken
        # stretch, so re-reading the same true predicate must not restart it.
        out[key] = previous.get(key) or world.now
    return out


def held_long_enough(cond: Mapping, world: World, value: float) -> bool:
    """Whether this threshold has held for its stated `for:`."""
    key = threshold_key(cond)
    if key not in world.numeric_since:
        # No memory has ever been resolved -- the plain threshold is what
        # `numeric_state` meant before `for:` existed, and every existing test
        # and the migration gate evaluate in exactly that state.
        return crosses(cond, value)
    since = world.numeric_since[key]
    if since is None:
        return False
    return world.now - since >= dt.timedelta(seconds=int(cond["for"]))
