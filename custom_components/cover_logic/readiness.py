"""Whether the world the engine just read was complete enough to act on.

**This module exists because of one measured minute.** On 2026-08-31, on the
live house (local times, from the log):

    11:45:34.995   cover_logic set up
    11:45:35.0xx   cover_logic[dry_run] cover.kuchyna_zaluzia_1_4 ... SetPosition(100)
                   ... the same SetPosition(100) for all ten blinds, mode=bezny_den

Half a second after setup, the engine decided "open" for every blind in the
house. At that instant `input_boolean.cover_down`, the `zaluzie_aktivna_*` room
flags, `sun.sun` and the weather entity were all still missing or
`unavailable`: Home Assistant had not finished restoring state. With no input
present, mode resolution fell through to the `bezny_den` catch-all and every
zone got its daytime "open" rule. It was harmless only because `dry_run` was
still on; at 11:49:31, with it off, the house opened.

The engine is not wrong to answer -- `evaluate` is total by design, and a
`Decision` on a half-loaded world is still the honest answer to "what do these
inputs say". What was missing is a layer that refuses to *act* on such an
answer. That is this module, and the answer it computes is used to gate
dispatch and nothing else: the decision is still made, still published and
still visible on the diagnostic sensor, because a diagnostic that goes blank
when something is wrong is the opposite of useful.

**The same hazard is not confined to startup.** The house's own `CLAUDE.md`
records `sensor.zaluzie_cielovy_stav` sitting at `unavailable` for four minutes
on 2026-08-06, during which the old system silently did nothing -- its
`script.zaluzie_uplatnit` opens with a `wait_template` for exactly this. This
is that same interlock, with two differences: it is derived from the config
rather than hand-written per script, and it is not silent.

## A read the configuration already answers is not a fault

Measured on the same house on 2026-08-31, this time by the gate itself: `assess`
reported `ready=False` with three names, permanently, so nothing could ever be
dispatched. Both causes were the configuration answering a question this module
then re-asked -- two dead anemometers read only by conditions carrying an
explicit `default:`, and `alarm_control_panel.alarmo`'s `arm_mode` attribute,
which Alarmo populates only while armed and whose absence *is* the answer "not
armed". So a read is a fault only where the configuration has no answer for it:
a node's `default:` exempts every entity that node reads, and an absent
attribute on a readable entity is a value rather than a fault. What still blocks
is an entity Home Assistant itself cannot answer for -- absent, `unknown` or
`unavailable` -- read by a node that stated no default, which is every mode
condition in this house and therefore the whole of the incident above. See
`docs/rationale.md` -- "Why a stated `default:` is not a readiness fault".

Read from the same `World` the decision was made from, never from
`hass.states` again. A second read of the state machine happens at a second
instant, and then "was the world ready" and "what did the world say" can
disagree about which world they mean -- which is the whole class of bug
`World` exists to remove.

## A veto, not a wait -- and per blind, not per house

Both halves of that are decisions with a cost; `docs/rationale.md`
("Why readiness is a veto and not a wait") carries the argument. In short: a
wait needs a deadline and a "proceed anyway", and proceeding on a decision
derived from missing inputs *is* the defect -- while `guards.py`'s `defer`
policy already is this project's one wait primitive, with `max_wait` and
`on_timeout` written down per guard. And scoping the veto per blind is what
keeps one dead sensor from stopping a house for a day: a blind is blocked by
the entities *its own* decision reads, so a dead kitchen sensor blocks the
kitchen and a dead mode input blocks everything -- the latter correctly, since
mode is a global fact and every blind's rule list is chosen by it.

No `homeassistant` import and no clock: it is in `tests/test_purity.py`'s
`PURE_MODULES` list, alongside the rest of the decision core it guards.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .config_schema import Read, node_reads, referenced_reads, walk_condition_nodes
from .const import (
    COND_REF,
    READINESS_MAX_NAMED,
    READINESS_REASON_PREFIX,
    RULE_DEFAULT_ZONE,
    UNREADY_STATES,
)
from .engine import resolve_ownership
from .guards import guard_blinds
from .model import Action, Config, Ref
from .world import World


@dataclass(frozen=True)
class Readiness:
    """Which referenced entities are not readable, and which blinds that blocks.

    `missing` is the whole-config answer required of this gate: every entity
    `config_schema.referenced_reads` names, undefaulted, whose state is absent,
    `unknown` or `unavailable`. `blocked` is the per-blind attribution the
    dispatcher actually gates on -- a blind maps to the subset of `missing` that
    its own decision reads, and a blind absent from `blocked` is one whose
    inputs are all present or all answered.

    The two are deliberately not the same question. An unready entity read only
    by a named condition nothing references blocks nobody, and must still be
    visible; a blind whose rules are unconditional depends on nothing and is
    correctly never blocked, however broken the rest of the house is.
    """

    missing: tuple[str, ...]
    blocked: Mapping[str, tuple[str, ...]]

    @property
    def ready(self) -> bool:
        """Whether every entity the configuration reads is currently readable."""
        return not self.missing

    def blocked_by(self, entity: str) -> tuple[str, ...]:
        """The unready entities `entity`'s own decision reads; empty means dispatchable."""
        return self.blocked.get(entity, ())

    def reason(self, entity: str) -> str:
        """One line naming why `entity` may not be commanded, for a log or an attribute."""
        return f"{READINESS_REASON_PREFIX}: {name_list(self.blocked_by(entity))}"

    def as_attributes(self) -> dict[str, object]:
        """The sensor's `readiness` attribute: the verdict plus who caused it.

        Both lists are truncated (`const.READINESS_MAX_NAMED`). Ten names is a
        diagnostic; a hundred is noise, and a state attribute this integration
        cannot bound is one a big enough house could use to take the entity
        down at write time.
        """
        return {
            "ready": self.ready,
            "missing": list(self.missing[:READINESS_MAX_NAMED]),
            "missing_count": len(self.missing),
            "blocked": {
                entity: list(names[:READINESS_MAX_NAMED])
                for entity, names in sorted(self.blocked.items())
            },
        }


def name_list(names: tuple[str, ...]) -> str:
    """`names`, truncated, as one comma-separated string ending in a count of the rest."""
    if not names:
        return "nothing"
    shown = ", ".join(names[:READINESS_MAX_NAMED])
    hidden = len(names) - READINESS_MAX_NAMED
    return shown if hidden <= 0 else f"{shown} (+{hidden} more)"


def assess(config: Config, world: World) -> Readiness:
    """Which of `config`'s referenced entities `world` cannot read, and whom that blocks.

    Takes no `Decision`, deliberately. Scoping a blind's dependencies to the
    *resolved* mode's rules would be tighter, and was rejected: the resolved
    mode is derived from the very inputs whose presence is in question, so on
    the one world that matters it is the untrustworthy value being used to
    decide how much to trust the rest. Every mode's rules for the blind's own
    zone are read instead -- broader, and independent of anything that could
    have gone wrong upstream.
    """
    owner = resolve_ownership(config)
    shared = _mode_reads(config)
    blocked: dict[str, tuple[str, ...]] = {}

    for entity, zone_id in owner.items():
        reads = shared | _zone_reads(config, zone_id) | _guard_reads(config, entity)
        unready = _unready(reads, world)
        if unready:
            blocked[entity] = tuple(sorted(unready))

    # Everything the config reads, not only what some blind reads: an unready
    # entity behind a named condition nothing references is still a real fault,
    # and would otherwise stay invisible until the day something referenced it.
    missing = _unready(referenced_reads(config), world)
    return Readiness(missing=tuple(sorted(missing)), blocked=blocked)


def _unready(reads: set[Read], world: World) -> set[str]:
    """The entity ids among `reads` that neither `world` nor the config can answer for.

    Two faults, one verdict, because acting on either is the same mistake: the
    entity is not in the snapshot at all (`ha_world.build_world` leaves a
    missing entity out rather than inventing a state for it), or its state is
    the literal `unknown`/`unavailable` Home Assistant reports. An attribute
    read reports its *entity's* fault, under the entity's own id -- the name a
    person has to go and look at is `sun.sun`, not `('sun.sun', 'azimuth')`.

    Two things are deliberately not faults, and both were measured vetoing this
    house forever on 2026-08-31. A `Read` its own node defaults is skipped
    outright: `default:` is the author saying "this may be missing, use this",
    and re-asking overrides an explicit answer. And an attribute with no value
    on a readable entity is a value, not a fault -- Home Assistant has no
    "attribute unavailable" marker, so an integration omits an attribute to
    *mean* something (`alarm_control_panel.alarmo` drops `arm_mode` while
    disarmed), and every attribute read in this dialect is total when it is
    absent: `state` compares, `numeric_state` must carry a `default`, and
    `sun_hits_target` falls back in `conditions._sun_hits_target`.
    """
    out: set[str] = set()
    for read in reads:
        if read.defaulted:
            continue
        if world.state(read.entity) in UNREADY_STATES:
            out.add(read.entity)
    return out


def _mode_reads(config: Config) -> set[Read]:
    """What mode resolution reads -- shared by every blind, because mode is global.

    A missing entity in any mode's `when` is what turned the measured incident
    into a house-wide one: with none of them readable, resolution fell through
    to the catch-all and chose the daytime rules for all ten blinds. Every
    mode's `when` counts, not just the ones before the one that matched, since
    which one matched is exactly what cannot be trusted here.
    """
    out: set[Read] = set()
    for mode in config.modes:
        out |= _condition_reads(config, mode.when)
    return out


def _zone_reads(config: Config, zone_id: str) -> set[Read]:
    """What the rules that can decide `zone_id` read, across every mode.

    A rule filed under the default-zone key (`const.RULE_DEFAULT_ZONE`) can
    decide this zone too, so it counts here as well -- `engine._apply_rules`
    falls through to it, and a readiness rule that missed it would let a blind
    be commanded from a rule whose own input was unreadable.
    """
    out: set[Read] = set()
    for key, rules in config.rules.items():
        _, _, zone = key.partition(".")
        if zone not in (zone_id, RULE_DEFAULT_ZONE):
            continue
        for rule in rules:
            out |= _condition_reads(config, rule.when)
            out |= _action_reads(rule.then)
    return out


def _guard_reads(config: Config, entity: str) -> set[Read]:
    """What the guards that target `entity` read -- `guard_blinds`, not `guard.targets`.

    Guards are in here for a sharper reason than the rules are. A `state`
    condition against a missing entity does not raise -- it evaluates `False`
    -- so an unreadable door sensor does not break a guard loudly, it stands
    the interlock down silently. That is the sauna/door interlock being off
    during exactly the minutes after a restart when nobody is watching.

    Read through `guards.guard_blinds` so "which blinds does this guard cover"
    keeps having one answer: a bare `targets` read would miss that no targets
    at all means every blind, and that a zone id stands for its members.
    """
    out: set[Read] = set()
    for guard in config.guards:
        if entity not in guard_blinds(config, guard):
            continue
        out |= _condition_reads(config, guard.when)
        if guard.then is not None:
            out |= _action_reads(guard.then)
    return out


def _condition_reads(
    config: Config, node: dict | list | None, _seen: frozenset[str] = frozenset()
) -> set[Read]:
    """Every read a condition subtree performs, following `!ref` into `config.conditions`.

    Refs are followed because the whole point is per-blind attribution:
    `referenced_reads` collects every named condition whether anything
    references it or not, which is right for "what to subscribe to" and far too
    wide for "what does *this* blind depend on". `_seen` breaks a circular
    reference the same way `conditions.evaluate_condition` does -- by refusing
    to re-enter a name, not by trusting the config to be acyclic.
    """
    out: set[Read] = set()
    for child in walk_condition_nodes(node):
        out |= node_reads(child)
        if child.get("condition") != COND_REF:
            continue
        name = child.get("name")
        if name in config.conditions and name not in _seen:
            out |= _condition_reads(config, config.conditions[name], _seen | {name})
    return out


def _action_reads(action: Action) -> set[Read]:
    """The helper entities an action's axes read at evaluation time, never defaulted.

    A `Ref` axis falls back to its own `default` when the helper is unreadable
    (`engine._resolve_value`), which is a designed fallback and not an error --
    but on a half-loaded world it means "send the default position", and 34 %
    sent to ten blinds is still the house moving on a world nobody saw. That is
    why a `values:` default, unlike a condition's, does not answer this
    question: see `docs/rationale.md` -- "Why a `values:` default is not an
    answer".
    """
    return {Read(axis.entity) for axis in (action.position, action.tilt) if isinstance(axis, Ref)}
