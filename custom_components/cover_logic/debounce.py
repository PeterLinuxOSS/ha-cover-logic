"""What a numeric threshold remembers between evaluations.

Two independent problems, both from the same sensor noise, and both needed:

`for:` qualifies the *entry* -- a threshold may not count as crossed until it
has stayed crossed. `condition: state` already takes this key; a
`numeric_state` could not, because `World.since` dates the entity's last
*state change* and a lux sensor rewrites its value every couple of minutes
whether or not it has crossed anything.

`latch: daily` refuses the *exit* -- once the threshold has genuinely been
crossed today, it stays crossed until the local date rolls over. Dusk does not
un-happen, and without this a late wobble reopens a house that was correctly
closed. It is deliberately useless on its own: a single spurious reading at
midday would latch the rest of the day, which is why the entry is what `for:`
is for.

See docs/rationale.md -- "Why `numeric_state` takes `for:`, and why it needs
its own memory".
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import datetime as dt

from .world import World

LATCH_DAILY = "daily"

_BOUND_KEYS = ("above", "below")


@dataclass(frozen=True, slots=True)
class Dwell:
    """One debounced threshold's memory, and the answer resolved from it."""

    since: dt.datetime | None = None
    held_on: dt.date | None = None
    answer: bool = False


def is_remembered(cond: Mapping) -> bool:
    """Whether this condition's answer depends on more than the current reading."""
    return cond.get("condition") == "numeric_state" and ("for" in cond or "latch" in cond)


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


def _step(cond: Mapping, world: World, was: Dwell) -> Dwell:
    """This snapshot's memory and answer for one threshold, given the previous."""
    value = world.number(
        cond["entity_id"],
        default=float(cond["default"]),
        attribute=cond.get("attribute"),
    )
    today = world.now.date()
    # A held answer keeps the moment it first became true rather than being
    # restamped, so a dwell measures one unbroken stretch.
    since = (was.since or world.now) if crosses(cond, value) else None
    dwell = dt.timedelta(seconds=int(cond.get("for", 0)))
    dwelled = since is not None and world.now - since >= dwell
    held_on = today if dwelled else was.held_on
    latched = cond.get("latch") == LATCH_DAILY and held_on == today
    return Dwell(since=since, held_on=held_on, answer=dwelled or latched)


def resolve(
    nodes: Iterable[Mapping],
    world: World,
    previous: Mapping[str, Dwell],
) -> dict[str, Dwell]:
    """Every remembered threshold's answer for this snapshot.

    Resolved once from the finished snapshot, before anything decides, so the
    rules, the guards and the readiness gate cannot read different answers
    within one evaluation.

    Every remembered condition gets an entry, so a missing key means "no memory
    at all" rather than "not true" -- which is what lets these keys fall back
    to the plain threshold in the pure tests and in the migration gate.
    """
    out: dict[str, Dwell] = {}
    for cond in nodes:
        if not is_remembered(cond):
            continue
        key = threshold_key(cond)
        if key not in out:
            out[key] = _step(cond, world, previous.get(key) or Dwell())
    return out


def remembered_answer(cond: Mapping, world: World, value: float) -> bool:
    """This threshold's resolved answer, or the plain test when nothing is remembered."""
    key = threshold_key(cond)
    if key not in world.numeric_since:
        # No memory has ever been resolved -- the plain threshold is what
        # `numeric_state` meant before these keys existed, and every existing
        # test and the migration gate evaluate in exactly that state.
        return crosses(cond, value)
    return world.numeric_since[key].answer
