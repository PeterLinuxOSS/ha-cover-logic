"""Static checks over a configuration.

Answers the questions the old Jinja matrix could not be asked: is every blind
owned, is every rule reachable, does every (mode, zone) pair actually decide
anything. Runs in the test suite and again on import in the UI.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from .const import (
    COND_EVENT_TARGETS_ZONE,
    COND_MANUAL_MOVE,
    COND_REF,
    COND_SUN,
    COND_SUN_HITS_TARGET,
    GUARD_ANY,
    GUARD_DEFER,
    GUARD_DIRECTIONS,
    GUARD_FORCE,
    GUARD_POLICIES,
    GUARD_STAGE_INPUT,
    GUARD_STAGES,
    GUARD_TIMEOUTS,
    RULE_DEFAULT_ZONE,
)
from .engine import EngineError, resolve_ownership
from .guards import guard_blinds
from .model import KEEP, UNSET, Config, Guard

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Problem:
    """One issue found in a configuration, at `ERROR` or `WARNING` severity.

    `owners` is which `(subentry_type, id)` pairs -- `id` being the value at
    that type's own `id_key` (`config_store._ID_KEY` for `condition`/`mode`,
    the equivalent for a future `rule`) -- carry the data this problem is
    actually about, for codes whose owning *type* alone is not enough to
    say which specific save could fix it: a condition body lives verbatim in
    a `condition` subentry's own fields, a `mode`'s `when`, or a `rule`'s
    `if`, and a dangling ref in one must not block a save of an unrelated
    other. Empty for every other code -- those have exactly one owning type,
    decided by the code alone (see `subentry_flow._CODE_OWNERS`), so no
    per-instance attribution is needed. Populated only by
    `_check_unknown_condition_refs`, `_check_condition_shapes` and
    `_check_circular_condition_refs`, the three checks whose codes need it;
    `subentry_flow._blocks_on` is the only reader.
    """

    severity: str
    code: str
    message: str
    owners: frozenset[tuple[str, str]] = frozenset()


def _rule_owner(key: str, index: int) -> tuple[str, str]:
    """The `(subentry_type, id)` naming one rule: its `(mode, zone)` key and its position.

    A rule subentry has no id field of its own -- its identity is
    `(mode, zone, order)` -- and `Config.rules` no longer carries `order` by
    the time this module sees it, so position within the already-`order`-sorted
    tuple is what is left to name it by. That is not a compromise invented
    here: `engine._apply_rules` labels the rule it fired with exactly this
    string, so a `Problem` and a decision trace name the same rule the same
    way. `config_store.rule_owner_ids` produces the matching side, mapping a
    real subentry id to this same string, and `tests/test_config_store.py`
    pins the two together.
    """
    return ("rule", f"{key}#{index}")


def _guard_owner(index: int) -> tuple[str, str]:
    """The `(subentry_type, id)` naming one guard: its position in `Config.guards`.

    Guards, like rules, have no id field of their own -- their identity is
    where they sit in a first-match-wins list, and `Config.guards` no longer
    carries whatever `order` a future `guard` subentry would have used by the
    time this module sees it. Position in the tuple is therefore what is left
    to name one by, exactly as `_rule_owner` names a rule.

    Nothing matches this owner today: `guards` is still carried in
    `entry.data`, not as a subentry type, so no form can be blocked by a
    guard's problem and none should be -- see `subentry_flow._CODE_OWNERS`'s
    guard entries. When the `guard` subentry flow lands, its `_candidate_id`
    is what has to answer with this same string, the way
    `config_store.rule_owner_ids` already does for rules.
    """
    return ("guard", f"guard#{index}")


def _guard_label(index: int, guard: Guard) -> str:
    """How a guard is named in a problem message: its position, plus its name if it has one.

    Delegates to `Guard.label` so a health report and an evaluation trace name
    the same guard the same way -- see that method for why it lives on the
    model rather than being written out twice.
    """
    return guard.label(index)


def validate(config: Config) -> list[Problem]:
    """Run every static check and return all problems found, if any."""
    problems: list[Problem] = []
    problems += _check_blinds(config)
    problems += _check_guards(config)
    problems += _check_ownership(config)
    problems += _check_modes(config)
    problems += _check_rule_keys(config)
    problems += _check_rule_lists(config)
    problems += _check_circular_condition_refs(config)
    problems += _check_unknown_condition_refs(config)
    problems += _check_condition_shapes(config)
    problems += _check_tilt_on_tiltless_blinds(config)
    return problems


def _check_blinds(config: Config) -> list[Problem]:
    """Every blind's own fields, independently of any zone or rule.

    Only `travel_time` so far, and only that it is positive. It is not a
    cosmetic number: `planner.plan` derives the arrival wait from it
    (`travel_time * ARRIVAL_TIMEOUT_FACTOR`), so `travel_time: 0` produces a
    wait that expires the instant it starts, a 2 s settle against a ~55 s run,
    and a tilt command landing mid-travel -- which these motors discard. The
    slats then silently never end up where they were told to, which is the
    exact failure `planner.py` exists to prevent, arriving through the
    configuration instead of through the code.

    Checked here rather than in the planner on purpose: this is a standing
    property of the configuration, so it is reported once, statically, by the
    module that owns static checks, not re-derived on every recompute by the
    module that has to live with it. `subentry_flow`'s `travel_time` selector
    refuses to *offer* a non-positive value, but a YAML file (which only does
    `float()`) and a hand-edited subentry both reach here unfiltered, so the
    selector's `min` is a convenience and this is the check.
    """
    return [
        Problem(
            ERROR,
            "bad_travel_time",
            f"blind {entity!r} has travel_time={blind.travel_time!r}; it must be positive, "
            f"or the arrival wait derived from it expires before the blind has moved",
        )
        for entity, blind in config.blinds.items()
        if not blind.travel_time > 0
    ]


def _check_tilt_on_tiltless_blinds(config: Config) -> list[Problem]:
    """A rule that sets `tilt` on a blind that has none is a no-op, and says nothing.

    `planner.plan` gates the tilt half on `blind.has_tilt`, so the command is
    simply never issued. That is the right runtime behaviour -- there is no
    slat to move -- but nothing tells the author, so a configuration that looks
    like it sets the slats quietly does not. In this house every blind has
    tilt, so the check is inert; the install it matters to is the one that does
    not, which is the whole point of phase 7.

    `WARNING`, not `ERROR`: the configuration works, it just does less than it
    reads as. Ownership comes from `engine.resolve_ownership`, the one
    implementation of "which zone owns this blind", rather than a second walk
    over `zones` -- see `MODELS.md`'s rule about sorts that decide behaviour.
    That function *raises* on a broken ownership map, so this one has to catch
    it: `validate` exists to report on malformed configurations and must never
    fail on one.
    """
    try:
        owner_of = resolve_ownership(config)
    except EngineError:
        # Ownership is broken (an orphan, or a blind in two zones), which
        # `_check_ownership` has already reported by the time this runs. This
        # check has nothing to add about a configuration that cannot say which
        # zone decides a blind -- and it must not raise, because reporting on
        # exactly this kind of configuration is what `validate` is for.
        return []
    tiltless_by_zone: dict[str, list[str]] = {}
    for entity, blind in config.blinds.items():
        if blind.has_tilt:
            continue
        zone = owner_of.get(entity)
        if zone is not None:
            tiltless_by_zone.setdefault(zone, []).append(entity)
    if not tiltless_by_zone:
        return []

    out: list[Problem] = []
    for key, rules in config.rules.items():
        mode, _, zone = key.partition(".")
        zones = (
            sorted(tiltless_by_zone)
            if zone == RULE_DEFAULT_ZONE
            else ([zone] if zone in tiltless_by_zone else [])
        )
        if not zones:
            continue
        affected = sorted(entity for one in zones for entity in tiltless_by_zone.get(one, ()))
        for index, rule in enumerate(rules):
            if rule.then is None or rule.then.tilt is KEEP:
                continue
            out.append(
                Problem(
                    WARNING,
                    "tilt_on_tiltless_blind",
                    f"rule {key}#{index} sets tilt={rule.then.tilt!r} but "
                    f"{affected} has no tilt, so that half is never sent"
                    + ("" if zone != RULE_DEFAULT_ZONE else f" (mode {mode!r} default list)"),
                    owners=frozenset({_rule_owner(key, index)}),
                )
            )
    return out


def _direction_covers(outer: str, inner: str) -> bool:
    """Whether a guard watching `outer` movements also sees every `inner` one."""
    return outer in (GUARD_ANY, inner)


def _check_guard_policy(guard: Guard, where: str, owners: frozenset) -> list[Problem]:
    """One guard's policy and the fields that policy does or does not use.

    A field the policy ignores is an `ERROR`, not a cosmetic warning, because
    this exact mistake is already live in the house being migrated: the
    bedroom door automation carries `continue_on_timeout: true` with no
    `timeout` key at all, which Home Assistant silently ignores -- the wait is
    unlimited and the line that looks like it bounds it does nothing. A
    schema that accepted `max_wait` on a `skip` would reproduce that class of
    dead configuration verbatim.
    """
    out: list[Problem] = []
    if guard.policy not in GUARD_POLICIES:
        return [
            Problem(
                ERROR,
                "guard_unknown_policy",
                f"{where}: unknown policy {guard.policy!r}; expected one of "
                f"{', '.join(sorted(GUARD_POLICIES))}",
                owners=owners,
            )
        ]

    if guard.policy == GUARD_DEFER:
        if guard.max_wait is UNSET:
            out.append(
                Problem(
                    ERROR,
                    "guard_defer_needs_timeout",
                    f"{where}: a 'defer' must state 'max_wait'; write 'max_wait: null' "
                    f"for a deliberately unlimited wait",
                    owners=owners,
                )
            )
        if guard.on_timeout is None:
            out.append(
                Problem(
                    ERROR,
                    "guard_defer_needs_timeout",
                    f"{where}: a 'defer' must state 'on_timeout' ("
                    f"{', '.join(sorted(GUARD_TIMEOUTS))}); there is no default because "
                    f"the two do opposite things",
                    owners=owners,
                )
            )
        elif guard.on_timeout not in GUARD_TIMEOUTS:
            out.append(
                Problem(
                    ERROR,
                    "guard_defer_needs_timeout",
                    f"{where}: unknown 'on_timeout' {guard.on_timeout!r}; expected one of "
                    f"{', '.join(sorted(GUARD_TIMEOUTS))}",
                    owners=owners,
                )
            )
    else:
        out.extend(
            Problem(
                ERROR,
                "guard_unused_field",
                f"{where}: {field!r} means nothing for policy {guard.policy!r}; "
                f"it is only read for 'defer'",
                owners=owners,
            )
            for field, given in (
                ("max_wait", guard.max_wait is not UNSET),
                ("on_timeout", guard.on_timeout is not None),
                ("recheck_every", guard.recheck_every is not None),
            )
            if given
        )

    if guard.policy == GUARD_FORCE and guard.then is None:
        out.append(
            Problem(
                ERROR,
                "guard_force_needs_action",
                f"{where}: a 'force' must state the action it imposes in 'then'",
                owners=owners,
            )
        )
    if guard.policy != GUARD_FORCE and guard.then is not None:
        out.append(
            Problem(
                ERROR,
                "guard_unused_field",
                f"{where}: 'then' means nothing for policy {guard.policy!r}; "
                f"it is only read for 'force'",
                owners=owners,
            )
        )
    return out


def _check_guards(config: Config) -> list[Problem]:
    """Every guard's own shape, then whether each one can ever be reached.

    A guard's `when` is not checked here at all: it is an ordinary condition
    body, so `_condition_sites` yields it alongside every mode's and rule's
    and the existing `_check_unknown_condition_refs`/`_check_condition_shapes`
    cover it unchanged. That is the point of reusing the condition dialect
    rather than inventing a second one -- see `docs/rationale.md`, "Why a
    guard's `when` is the ordinary condition dialect".
    """
    out: list[Problem] = []
    for index, guard in enumerate(config.guards):
        where = _guard_label(index, guard)
        owners = frozenset({_guard_owner(index)})

        out += _check_guard_policy(guard, where, owners)

        if guard.applies_to not in GUARD_DIRECTIONS:
            out.append(
                Problem(
                    ERROR,
                    "guard_bad_direction",
                    f"{where}: unknown 'applies_to' {guard.applies_to!r}; expected one of "
                    f"{', '.join(sorted(GUARD_DIRECTIONS))}",
                    owners=owners,
                )
            )
        if guard.stage not in GUARD_STAGES:
            out.append(
                Problem(
                    ERROR,
                    "guard_bad_stage",
                    f"{where}: unknown 'stage' {guard.stage!r}; expected one of "
                    f"{', '.join(sorted(GUARD_STAGES))}",
                    owners=owners,
                )
            )
        elif guard.stage == GUARD_STAGE_INPUT and guard.applies_to != GUARD_ANY:
            # Not a style objection: an `input` guard removes its target
            # before anything has been decided for it, so there is no
            # candidate command whose direction could be read. `guards.py`
            # refuses to guess (it would either over-block -- dropping the
            # blind from decisions the author never meant to touch, the same
            # harm `const.GUARD_CLOSING` warns about, reached through the
            # stage rather than the axis -- or silently delete an interlock),
            # so this has to be caught before it ever loads.
            out.append(
                Problem(
                    ERROR,
                    "guard_input_direction",
                    f"{where}: 'applies_to' is {guard.applies_to!r}, but a guard at stage "
                    f"{GUARD_STAGE_INPUT!r} acts before anything is decided for its target, "
                    f"so there is no command whose direction it could mean; use "
                    f"{GUARD_ANY!r}, or move the guard to the output stage",
                    owners=owners,
                )
            )

        out.extend(
            Problem(
                ERROR,
                "guard_unknown_target",
                f"{where}: target {target!r} is neither a configured blind nor a zone",
                owners=owners,
            )
            for target in guard.targets
            if target not in config.blinds and target not in config.zones
        )

    return out + _check_guard_reachability(config)


def _check_guard_reachability(config: Config) -> list[Problem]:
    """A guard an earlier unconditional one already answers for can never fire.

    Guards resolve first-match-wins, the same way rules do (`MODELS.md` §3),
    so the same dead-row question `_check_unreachable_within` asks of a rule
    list has to be asked here -- and it is the only referee guards have.
    Order is deliberately the whole conflict-resolution mechanism (no numeric
    priorities), which makes "this guard is written after one that already
    covers it" the single way a guard silently stops existing.

    Shadowing is judged per blind, not per written target, so a guard naming
    a zone and a later one naming a blind inside that zone are correctly seen
    as overlapping. Three things have to line up before an earlier guard is
    said to swallow a later one, and all three are conservative:

    - the earlier guard has no `when`, so it matches unconditionally;
    - it runs at the same `stage` -- an `input` guard removing a target and
      an `output` guard overriding a decision are asked at different moments,
      and neither hides the other;
    - its `applies_to` covers the later one's direction.
    """
    out: list[Problem] = []
    covered: dict[tuple[str, str], set[str]] = {}

    for index, guard in enumerate(config.guards):
        mine = guard_blinds(config, guard)
        if mine:
            shadow = {
                blind
                for (stage, direction), blinds in covered.items()
                if stage == guard.stage and _direction_covers(direction, guard.applies_to)
                for blind in blinds
            }
            if mine <= shadow:
                out.append(
                    Problem(
                        WARNING,
                        "guard_unreachable",
                        f"{_guard_label(index, guard)} can never fire; an earlier guard "
                        f"with no condition already covers every blind it names",
                        owners=frozenset({_guard_owner(index)}),
                    )
                )
        if guard.when is None:
            covered.setdefault((guard.stage, guard.applies_to), set()).update(mine)

    return out


def _check_ownership(config: Config) -> list[Problem]:
    out: list[Problem] = []
    owner: dict[str, str] = {}

    for zone_id, zone in config.zones.items():
        for entity in zone.members:
            if entity not in config.blinds:
                out.append(
                    Problem(
                        ERROR,
                        "zone_member_unknown",
                        f"zone {zone_id!r} refers to unknown blind {entity!r}",
                    )
                )
            if entity in owner:
                out.append(
                    Problem(
                        ERROR,
                        "blind_in_two_zones",
                        f"blind {entity!r} is owned by {owner[entity]!r} and {zone_id!r}",
                    )
                )
            else:
                owner[entity] = zone_id

    # WARNING, not ERROR, since 2026-09-03: an `ERROR` here refused the whole
    # entry, so one incomplete blind stopped the house deciding about all the
    # others. `engine.resolve_ownership` skips it instead and
    # `__init__._check_orphan_blinds` raises a repair issue -- loud without
    # being fatal. See docs/rationale.md, "Why an orphan blind is skipped
    # rather than fatal".
    out.extend(
        Problem(
            WARNING,
            "blind_without_zone",
            f"blind {entity!r} belongs to no zone, so no rule decides it and it will "
            f"not be commanded; put it in a zone or remove it",
        )
        for entity in config.blinds
        if entity not in owner
    )
    return out


def _check_modes(config: Config) -> list[Problem]:
    out: list[Problem] = []
    fallbacks = [i for i, m in enumerate(config.modes) if m.when is None]
    if not fallbacks:
        out.append(
            Problem(
                ERROR,
                "no_fallback_mode",
                "no mode without a condition; some states would resolve to no mode",
            )
        )
        return out
    first = fallbacks[0]
    if first != len(config.modes) - 1:
        dead = ", ".join(m.id for m in config.modes[first + 1 :])
        out.append(
            Problem(
                ERROR,
                "fallback_mode_not_last",
                f"mode {config.modes[first].id!r} has no condition but is not last; "
                f"these can never match: {dead}",
            )
        )
    return out


def _check_rule_keys(config: Config) -> list[Problem]:
    out: list[Problem] = []
    mode_ids = {m.id for m in config.modes}
    for key, rules in config.rules.items():
        mode, _, zone = key.partition(".")
        if mode not in mode_ids or (zone != RULE_DEFAULT_ZONE and zone not in config.zones):
            # Every rule filed under this key carries the offending
            # `mode`/`zone` pair in its own subentry data, and each one's own
            # form is where it is repointed at a pair that exists -- so all
            # of them own this, the same way every name on a reference cycle
            # owns that cycle. Without this, a rule left stranded by deleting
            # its mode would block *adding a rule to an unrelated, healthy
            # pair*, a form with no way to reach the stranded one.
            out.append(
                Problem(
                    ERROR,
                    "unknown_rule_key",
                    f"rule key {key!r} names an unknown mode or zone",
                    owners=frozenset(_rule_owner(key, index) for index in range(len(rules))),
                )
            )
    return out


def _check_rule_lists(config: Config) -> list[Problem]:
    """Per (mode, zone): is there anything to decide it, and can every row of it fire.

    A zone's *effective* list, since inheritance landed, is its own rules
    followed by the mode's default rules (`f"{mode}.{RULE_DEFAULT_ZONE}"`,
    see `engine._apply_rules`) -- `missing_rule_list` and `no_catch_all` are
    judged against that concatenation, not the zone's own list in isolation,
    or a zone with no rules of its own but a mode-wide default would wrongly
    warn about having none. Unreachability, though, is checked in three
    separate passes rather than once over the concatenation, to avoid
    reporting the same fact more than once or attributing it to the wrong
    subentry:

    - a default list's own internal unreachability is checked once per
      mode (`_check_unreachable_within(default_key, ...)` below, outside the
      zone loop) -- it is one subentry group's problem regardless of how
      many zones inherit it, so checking it once per zone that does would
      report the identical complaint N times;
    - a zone's own list's internal unreachability is checked per zone
      (unchanged from before inheritance existed);
    - a default row a *specific* zone's own catch-all shadows is collected
      per mode across every zone (`_check_default_rows_shadowed_by_zones` below)
      and reported once per shadowed row, naming every zone that shadows it,
      not once per shadowing zone -- see that function's own docstring for
      why the naive per-zone version this replaced made the warning count
      scale with the number of zones for what is a single dead rule.
    """
    out: list[Problem] = []
    for mode in config.modes:
        default_key = f"{mode.id}.{RULE_DEFAULT_ZONE}"
        default_rules = config.rules.get(default_key)
        already_dead: set[int] = set()
        if default_rules:
            out += _check_unreachable_within(default_key, default_rules)
            already_dead = _unreachable_indices(default_rules)

        shadowed_by: dict[int, list[str]] = {}

        for zone_id in config.zones:
            key = f"{mode.id}.{zone_id}"
            own_rules = config.rules.get(key)
            effective = (own_rules or ()) + (default_rules or ())
            if not effective:
                out.append(
                    Problem(
                        WARNING,
                        "missing_rule_list",
                        f"{key} has no rules; every blind there keeps its position",
                    )
                )
                continue
            if own_rules:
                out += _check_unreachable_within(key, own_rules)
            if default_rules:
                for index in _shadowed_default_indices(own_rules, default_rules):
                    shadowed_by.setdefault(index, []).append(key)
            if not any(r.when is None and r.events is None for r in effective):
                out.append(
                    Problem(
                        WARNING,
                        "no_catch_all",
                        f"{key} has no final rule without a condition; "
                        f"some states fall through to keep/keep silently",
                    )
                )

        out += _check_default_rows_shadowed_by_zones(default_key, shadowed_by, already_dead)
    return out


def _unreachable_indices(rules) -> set[int]:
    """Indices in `rules` an earlier unconditional rule in the same list already makes dead.

    The one traversal both `_check_unreachable_within` (a list checked
    against itself) and `_check_rule_lists` (a mode's default list checked
    against itself, once, before any zone-shadowing is even considered) read
    -- so "is this row already dead on its own account" is answered the same
    way everywhere it is asked, never re-derived.
    """
    out: set[int] = set()
    catch_all_scopes: list[frozenset | None] = []

    for index, rule in enumerate(rules):
        for scope in catch_all_scopes:
            if scope is None or (rule.events is not None and rule.events <= scope):
                out.add(index)
                break
        if rule.when is None:
            catch_all_scopes.append(rule.events)

    return out


def _check_unreachable_within(key: str, rules) -> list[Problem]:
    """A rule with no `if` swallows everything after it in the same event scope, within one list.

    The single traversal every reachability check in this module runs, so
    that a rule list checked for internal unreachability -- a zone's own
    list, or a mode's shared default list -- is walked the same way whether
    inheritance is involved for that particular (mode, zone) or not. See
    `_shadowed_default_indices` for the other half: a default row a
    *specific zone's* own catch-all shadows, which is not internal to either
    list and so cannot be found by walking one list alone.
    """
    return [
        Problem(
            WARNING,
            "unreachable_rule",
            f"{key}#{index} can never fire; an earlier rule "
            f"with no condition already matches everything",
        )
        for index in sorted(_unreachable_indices(rules))
    ]


def _shadowed_default_indices(own_rules: tuple | None, default_rules: tuple) -> set[int]:
    """Indices into `default_rules` this zone's own catch-all(s) already make unreachable.

    See `_check_default_rows_shadowed_by_zones` for why this returns indices
    to be collected across every zone before any `Problem` is built, rather
    than reporting here directly: a default row shadowed by three zones must
    become one `Problem` naming all three, not three identical-looking ones,
    or the warning count would scale with the number of zones for what is a
    single dead rule -- exactly the multiplication this pair of functions
    replaces `_check_default_shadowed_by_zone` to avoid. This is the *zone's*
    rule shadowing the mode's shared default, not a mistake in the default
    itself: a different zone with no catch-all of its own, or a narrower
    one, may still reach the identical default row.
    """
    catch_all_scopes = [rule.events for rule in (own_rules or ()) if rule.when is None]
    if not catch_all_scopes:
        return set()

    out: set[int] = set()
    for index, rule in enumerate(default_rules):
        for scope in catch_all_scopes:
            if scope is None or (rule.events is not None and rule.events <= scope):
                out.add(index)
                break
    return out


def _check_default_rows_shadowed_by_zones(
    default_key: str, shadowed_by: dict[int, list[str]], already_dead: set[int]
) -> list[Problem]:
    """One `Problem` per default row shadowed by at least one zone, naming every shadowing zone.

    `shadowed_by` maps a default row's index to every `(mode.zone)` key whose
    own catch-all(s) make that row unreachable *for that zone* --
    `_check_rule_lists` builds it across all of a mode's zones before
    calling this, instead of calling `_shadowed_default_indices` once per
    zone and reporting immediately, which is what used to turn one dead
    default row into one warning per zone that inherits it (seven zones,
    one broken rule, seven warnings -- the health overview's whole point is
    to say "one thing is wrong here", and a count that grows with the house
    buries that instead of surfacing it).

    `already_dead` -- rows `_unreachable_indices(default_rules)` already
    flags as unreachable on the default list's own account, regardless of
    any zone -- are skipped here: a row already reported dead at the mode
    level is not made "more dead" by also being shadowed by particular
    zones, and reporting it twice under two different codes/messages for
    the same underlying row would be exactly the kind of noise this fix
    exists to remove.
    """
    return [
        Problem(
            WARNING,
            "unreachable_rule",
            f"{default_key}#{index} can never fire for "
            f"{', '.join(repr(k) for k in sorted(keys))}: "
            f"their own rules already have a catch-all",
        )
        for index, keys in sorted(shadowed_by.items())
        if index not in already_dead
    ]


def _check_circular_condition_refs(config: Config) -> list[Problem]:
    """Detect cycles in condition references.

    A cycle is when a condition refers to itself directly or through a chain
    of references. For example: A -> B -> A is a cycle.
    """
    out: list[Problem] = []
    cycles_reported: set[frozenset[str]] = set()

    for cond_name in config.conditions:
        cycle = _find_cycle_from(cond_name, config.conditions)
        if cycle is not None:
            # Normalize cycle to avoid reporting the same cycle multiple times
            # (a cycle found starting from different members is the same set
            # of names, just rotated to a different starting point).
            cycle_set = frozenset(cycle)
            if cycle_set not in cycles_reported:
                cycles_reported.add(cycle_set)
                # `cycle` is already in real traversal order (the order the
                # DFS actually followed the references) -- report it as-is,
                # not re-sorted, so the message names an edge that exists.
                loop = f"{' -> '.join(cycle)} -> {cycle[0]}"
                # Every name on the cycle is a `condition` subentry (the
                # traversal only ever follows `config.conditions`, never a
                # mode's/rule's `when` -- neither can be *part of* a cycle,
                # only refer into one), and editing any single one of them to
                # break its outgoing ref fixes the whole cycle -- so all of
                # them are owners, not just the traversal's start.
                out.append(
                    Problem(
                        ERROR,
                        "circular_condition_ref",
                        f"circular condition reference: {loop}",
                        owners=frozenset(("condition", name) for name in cycle),
                    )
                )

    return out


def _find_cycle_from(start_name: str, registry: dict[str, dict]) -> list[str] | None:
    """Find a cycle reachable from `start_name`, or return `None`.

    See docs/rationale.md -- "Why `_find_cycle_from` is iterative, not
    recursive".
    """
    visited: set[str] = set()
    on_path: set[str] = set()
    path: list[str] = []
    # Each stack frame pairs a node with an iterator over its outgoing
    # references, so resuming a frame after a child is fully explored is a
    # plain next() on that same iterator rather than a recursive call.
    stack: list[tuple[str, Iterator[str]]] = []

    def enter(node: str) -> None:
        visited.add(node)
        on_path.add(node)
        path.append(node)
        stack.append((node, iter(_get_referenced_conditions(node, registry))))

    enter(start_name)
    while stack:
        node, refs = stack[-1]
        ref_name = next(refs, None)
        if ref_name is None:
            # No more outgoing references from this node -- backtrack.
            stack.pop()
            path.pop()
            on_path.discard(node)
            continue
        if ref_name not in visited:
            enter(ref_name)
        elif ref_name in on_path:
            cycle_start_idx = path.index(ref_name)
            return path[cycle_start_idx:]
        # else: already fully explored via another branch -- a legal
        # cross-edge, not a cycle.

    return None


def _walk_condition_nodes(node) -> Iterator[dict]:
    """Yield every condition dict reachable from `node`.

    See docs/rationale.md -- "Why `_walk_condition_nodes` is the single
    traversal".
    """
    if isinstance(node, dict):
        yield node
        for sub_cond in node.get("conditions", []):
            yield from _walk_condition_nodes(sub_cond)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_condition_nodes(item)


def _referenced_condition_names(node) -> set[str]:
    """Return every condition name a `{condition: ref, name: ...}` in `node` refers to."""
    return {
        n.get("name", "") for n in _walk_condition_nodes(node) if n.get("condition") == COND_REF
    }


def _condition_sites(
    config: Config,
) -> Iterator[tuple[dict | list | None, str, tuple[str, str]]]:
    """Yield every top-level condition slot: its body, a label, and its owner.

    The owner is `(subentry_type, id)` -- `id` matching exactly what that
    type's own form would submit as `data[id_key]`, so `subentry_flow._blocks_
    on` can compare it directly against the subentry actually being saved.
    A rule has no such field, so it is named `f"{key}#{index}"` instead --
    see `_rule_owner` for why that shape, and `config_store.rule_owner_ids`
    for the mapping the `rule` flow uses to answer with the same string.
    """
    for cond_name, body in config.conditions.items():
        yield body, f"condition {cond_name!r}", ("condition", cond_name)
    for mode in config.modes:
        yield mode.when, f"mode {mode.id!r}", ("mode", mode.id)
    for key, rules in config.rules.items():
        for index, rule in enumerate(rules):
            yield rule.when, f"rule {key}#{index}", _rule_owner(key, index)
    # A guard's `when` is a condition body like any other, so it is checked
    # by the same two passes rather than by a guard-specific copy of them --
    # a dangling `!ref` or a `condition: nonsense` inside a guard is the same
    # mistake, and reporting it under a different code would mean two things
    # to keep in step for no gain.
    for index, guard in enumerate(config.guards):
        yield guard.when, _guard_label(index, guard), _guard_owner(index)


def _get_referenced_conditions(cond_name: str, registry: dict[str, dict]) -> set[str]:
    """Return the set of condition names directly referenced by cond_name."""
    if cond_name not in registry:
        return set()
    return _referenced_condition_names(registry[cond_name])


def _check_unknown_condition_refs(config: Config) -> list[Problem]:
    """Every `{condition: ref, name: N}` must name a condition that exists.

    See docs/rationale.md -- "Why `_check_unknown_condition_refs` exists
    despite YAML-time checking".
    """
    out: list[Problem] = []
    for node, where, owner in _condition_sites(config):
        if node is None:
            continue
        out.extend(
            Problem(
                ERROR,
                "unknown_condition_ref",
                f"{where} refers to unknown condition {name!r}",
                owners=frozenset({owner}),
            )
            for name in sorted(_referenced_condition_names(node))
            if name not in config.conditions
        )
    return out


# Required keys per condition type. `sun`, `time`, `numeric_state` and
# `sun_hits_target`/`event_targets_zone` need extra "at least one of" or
# "none required" handling beyond a flat required-set, so they are handled
# separately in `_check_condition_shape` -- this only covers the flat case.
# Entries and each tuple's contents are alphabetised by condition/key name;
# lookup is always by key (`_REQUIRED_CONDITION_KEYS[kind]`), never by
# position or iteration order.
_REQUIRED_CONDITION_KEYS: dict[str, tuple[str, ...]] = {
    "and": ("conditions",),
    COND_EVENT_TARGETS_ZONE: (),
    # `direction` is optional -- absent means "moved at all"; see
    # `conditions._manual_move`.
    COND_MANUAL_MOVE: (),
    "not": ("conditions",),
    "numeric_state": ("default", "entity_id"),
    "or": ("conditions",),
    COND_REF: ("name",),
    "state": ("entity_id", "state"),
    COND_SUN: (),
    COND_SUN_HITS_TARGET: (),
    "template": ("value_template",),
    "time": (),
}


def _check_condition_shape(node: dict, where: str, owner: tuple[str, str]) -> list[Problem]:
    """Check one condition dict's own shape; the caller walks its children.

    `owner` is the same `(subentry_type, id)` the whole site (`where`) came
    from -- every node nested inside one site's body lives in that one
    subentry's data blob, so a shape problem anywhere within it is fixed by
    that same subentry's own form, at whatever depth it is found.

    See docs/rationale.md -- "Why `_check_condition_shape` only checks known
    types and required keys".
    """
    kind = node.get("condition")
    owners = frozenset({owner})
    if kind not in _REQUIRED_CONDITION_KEYS:
        return [
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: unknown condition type {kind!r}",
                owners=owners,
            )
        ]

    out: list[Problem] = []
    missing = [key for key in _REQUIRED_CONDITION_KEYS[kind] if key not in node]
    if missing:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} is missing required key(s) {missing}",
                owners=owners,
            )
        )
    if kind == "numeric_state" and "above" not in node and "below" not in node:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} needs at least one of 'above'/'below'",
                owners=owners,
            )
        )
    if kind in {COND_SUN, "time"} and "after" not in node and "before" not in node:
        out.append(
            Problem(
                ERROR,
                "bad_condition_shape",
                f"{where}: condition {kind!r} needs at least one of 'after'/'before'",
                owners=owners,
            )
        )
    if "for" in node and kind != "state":
        # Not "not implemented yet" but "cannot be": see docs/rationale.md,
        # "Why `numeric_state` cannot take `for:`". Saying so in the message
        # matters -- "ignored" reads as a bug someone will fix, and the
        # obvious fix is silently wrong.
        out.append(
            Problem(
                WARNING,
                "for_ignored",
                f"{where}: condition {kind!r} cannot take 'for:' and ignores it. Only 'state' "
                f"takes it, because 'for:' is measured from when the entity last changed state "
                f"-- which for a threshold is the wrong clock: a sensor that keeps rewriting its "
                f"value resets it while the threshold stays crossed. Debounce the trigger that "
                f"writes the entity, or add a 'state' condition on a helper that latches it",
                owners=owners,
            )
        )
    return out


def check_duplicate_rule_order(orders: dict[str, list[tuple[str, int]]]) -> list[Problem]:
    """Flag more than one rule subentry claiming the same `order` in one key.

    `orders` maps a `"<mode id>.<zone id>"` key to a `(owner id, order)` pair
    per rule subentry filed under it -- the owner id being the `_rule_owner`
    string naming that specific rule, so the resulting `Problem` can say
    *which* rules are tied rather than only that some are. Without that, a
    tie left behind anywhere would block every rule save (`subentry_flow.
    _blocks_on` would have nothing finer than the type to go on), including
    adding a rule to an unrelated pair that has no tie at all.

    Not part of `validate()`: rules are first-match-wins, so a subentry
    author's `order` *is* the behaviour, and Home Assistant subentries are a
    flat list with no native reordering -- but once
    `config_store.config_from_subentries` sorts a tie into `Config.rules`'s
    plain tuple, the tie is gone and indistinguishable from a deliberate
    sequence. This must run over the subentry-side grouping, before that
    happens, or a duplicate `order` becomes a silent pick the UI never shows
    as ambiguous. See `config_store.duplicate_rule_order_problems`, the only
    caller.
    """
    return _duplicate_order_problems(
        orders, noun="rule", code="duplicate_rule_order", owner_type="rule"
    )


def check_duplicate_guard_order(orders: list[tuple[str, int]]) -> list[Problem]:
    """Flag more than one guard subentry claiming the same `order`.

    The guard counterpart of `check_duplicate_rule_order`, for the same
    reason: guards are first-match-wins too, so the `order` a subentry author
    types *is* the behaviour -- and once `config_store._ordered_guards` sorts
    a tie into `Config.guards`'s plain tuple, the tie is gone and
    indistinguishable from a deliberate sequence.

    Takes a flat list, not a mapping: guards are one ordered list for the
    whole config, not grouped by `(mode, zone)` the way rules are. See
    `config_store.duplicate_guard_order_problems`, the only caller.
    """
    return _duplicate_order_problems(
        {"": orders}, noun="guard", code="duplicate_guard_order", owner_type="guard"
    )


def _duplicate_order_problems(
    groups: dict[str, list[tuple[str, int]]], *, noun: str, code: str, owner_type: str
) -> list[Problem]:
    """Shared body of the two `check_duplicate_*_order` checks above.

    One implementation rather than two near-copies: tie detection and the
    "every tied item is an owner" rule are the same idea for both, and this
    repo pays repeatedly for two owners of one idea (`MODELS.md` Sec. 9). An
    empty `key` yields an unprefixed message, which is what the ungrouped
    guard list wants.
    """
    out: list[Problem] = []
    for key, items in groups.items():
        by_order: dict[int, list[str]] = {}
        for owner_id, order in items:
            by_order.setdefault(order, []).append(owner_id)
        prefix = f"{key}: " if key else ""
        # One problem per tied `order`, not one per extra item on it: three
        # items sharing an order are a single ambiguity to resolve, and every
        # one of them is an owner because editing any of them is a way to
        # resolve it.
        out.extend(
            Problem(
                ERROR,
                code,
                f"{prefix}more than one {noun} has order={order}",
                owners=frozenset((owner_type, owner_id) for owner_id in owner_ids),
            )
            for order, owner_ids in by_order.items()
            if len(owner_ids) > 1
        )
    return out


def _check_condition_shapes(config: Config) -> list[Problem]:
    """Check every condition body's shape: known type, required keys present.

    See docs/rationale.md -- "Why `_check_condition_shapes` exists as a
    separate check".
    """
    out: list[Problem] = []
    for node, where, owner in _condition_sites(config):
        if node is None:
            continue
        for n in _walk_condition_nodes(node):
            out += _check_condition_shape(n, where, owner)
    return out
