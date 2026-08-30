"""Guards: the two stages, the three policies, the direction, and first match wins.

The fixture below is the house's real interlock inventory
(`docs/superpowers/specs/2026-08-29-inventura-poistiek.md`, outside this
repository) re-expressed in the `guards:` schema, so these tests are also the
answer to "can the schema actually say what the house already does".

Every implication-shaped assertion here carries a counter: "no guard fired on
any of these worlds" would satisfy most of them vacuously, and a suite that
passes because it never exercised the thing is the failure mode this project
has already shipped three times.
"""

import inspect

import pytest

from cover_logic.config_schema import load_config
from cover_logic.const import (
    GUARD_ANY,
    GUARD_CLOSING,
    GUARD_DEFER,
    GUARD_FORCE,
    GUARD_OPENING,
    GUARD_SKIP,
    GUARD_STAGE_INPUT,
    GUARD_STAGE_OUTPUT,
)
from cover_logic.engine import Decision, EngineError, evaluate
from cover_logic.guards import NO_GUARD, GuardError, Screening, guard_blinds, review, screen
from cover_logic.model import KEEP, Action, Ref
from cover_logic.validation import ERROR, validate
from cover_logic.world import World

LIVING_DOOR = "cover.obyvacka_zaluzia_dvere_1_3"
LIVING_DOOR_2 = "cover.obyvacka_zaluzia_dvere_2"
BEDROOM_DOOR = "cover.spalna_zaluzia_dvere_1"
FLOWERS_A = "cover.kuchyna_zaluzia_1_4"
FLOWERS_B = "cover.kuchyna_zaluzia_2_5"
ALL_BLINDS = (LIVING_DOOR, LIVING_DOOR_2, BEDROOM_DOOR, FLOWERS_A, FLOWERS_B)

# The house's thirteen interlocks, as far as this schema can say them. The
# numbering in the comments is the inventory's.
#
# Deliberately *not* here, and why:
#   #3  "poistka po reštarte" is not a guard -- it is a watchdog over #2's
#       unlimited wait, and this schema absorbs it into `recheck_every`.
#   #11 writes `zaluzie_aktivna_*`; it moves no cover.
#   #16 only logs, #17 is dead code.
#
# The house implements the door interlock on the *same* blind twice, as a
# `skip` on one call path (`zavriet_bezpecne`) and a `defer` on another
# (Lighting SUN's delayed close). One ordered guard list forces a choice; both
# shapes appear below, on different blinds, which is exactly the collision the
# rewrite is meant to settle.
HOUSE = """
blinds:
  - entity: cover.obyvacka_zaluzia_dvere_1_3
    facade_azimuth: 225
  - entity: cover.obyvacka_zaluzia_dvere_2
    facade_azimuth: 225
  - entity: cover.spalna_zaluzia_dvere_1
    facade_azimuth: 225
  - entity: cover.kuchyna_zaluzia_1_4
    facade_azimuth: 135
  - entity: cover.kuchyna_zaluzia_2_5
    facade_azimuth: 135

zones:
  terasa:
    members: [cover.obyvacka_zaluzia_dvere_1_3, cover.obyvacka_zaluzia_dvere_2]
  spalna:
    members: [cover.spalna_zaluzia_dvere_1]
  kvety:
    members: [cover.kuchyna_zaluzia_1_4, cover.kuchyna_zaluzia_2_5]

conditions:
  obyvacka_dvere:
    condition: state
    entity_id: binary_sensor.obyvacka_dvere_senzor
    state: "on"
  sauna_bezi:
    condition: state
    entity_id: binary_sensor.sauna_running
    state: "on"
  spalna_dvere:
    condition: state
    entity_id: binary_sensor.spalna_dvere_senzor_2
    state: "on"
  vietor:
    condition: numeric_state
    entity_id: sensor.wind_speed
    above: 40
    default: 0
  kvety_on:
    condition: state
    entity_id: input_boolean.kvety_on
    state: "on"
  kvety_rucny_override:
    condition: state
    entity_id: input_boolean.kvety_rucny_override
    state: "on"
  spalna_ma_vlastny_skript:
    condition: state
    entity_id: input_boolean.spalna_routing
    state: "on"
  prave_odzbrojene:
    condition: state
    entity_id: input_boolean.vac_prisiel_domov
    state: "on"

values:
  kvety_pozicia:
    entity: input_number.kvety_pozicia_zaluzie
    default: 34

modes:
  - id: bezny

rules:
  bezny.terasa:
    - then: { position: 100, tilt: 100 }
  bezny.spalna:
    - then: { position: 100, tilt: 100 }
  bezny.kvety:
    - then: { position: 100, tilt: 100 }

guards:
  # #9 -- first on purpose: the one interlock that has to work when everything
  # else is broken. No `targets`, so the whole house.
  - name: wind protection
    policy: force
    applies_to: any
    when: !ref vietor
    then: { position: 100, tilt: 100 }

  # #12 -- the bedroom is routed to its own script; drop it from the question
  # entirely rather than overriding an answer nobody should have asked for.
  - name: bedroom routes to its own script
    policy: skip
    stage: input
    applies_to: any
    targets: [spalna]
    when: !ref spalna_ma_vlastny_skript

  # #11 pausing #10: a hand raised the flowers, the keeper must not pull them
  # back. Written before the keeper, which is the only reason it wins.
  - name: flowers raised by hand
    policy: skip
    applies_to: any
    targets: [kvety]
    when: !ref kvety_rucny_override

  # #10 -- the keeper itself; the position comes from a helper, so `then`
  # carries a ref that has to be resolved before anything can be planned.
  - name: flower keeper
    policy: force
    applies_to: any
    targets: [kvety]
    when: !ref kvety_on
    then: { position: !ref kvety_pozicia }

  # #2/#7 -- do not lower the terrace blinds onto an open door or a hot
  # sauna; close them once it clears, up to three hours later, and give up
  # rather than force it after that. `recheck_every` is #3, absorbed.
  - name: terrace door or sauna
    policy: defer
    applies_to: closing
    targets: [terasa]
    when:
      condition: or
      conditions: [!ref obyvacka_dvere, !ref sauna_bezi]
    max_wait: 10800
    on_timeout: abandon
    recheck_every: 900

  # #1/#14 -- the bedroom half of `zavriet_bezpecne`: no sauna term, and a
  # plain skip rather than a wait.
  - name: bedroom door open
    policy: skip
    applies_to: closing
    targets: [cover.spalna_zaluzia_dvere_1]
    when: !ref spalna_dvere

  # #13 -- coming home opens the terrace door blinds again. In the house this
  # is an alarm-disarm *event*; guards have no `events:` field, so it hangs
  # off a helper the disarm sets.
  - name: home again
    policy: force
    applies_to: any
    targets: [terasa]
    when: !ref prave_odzbrojene
    then: { position: 100 }
"""


@pytest.fixture(scope="module")
def house():
    return load_config(HOUSE)


def world(**states) -> World:
    """A snapshot where every entity the fixture reads is quiet unless named."""
    base = {
        "binary_sensor.obyvacka_dvere_senzor": "off",
        "binary_sensor.sauna_running": "off",
        "binary_sensor.spalna_dvere_senzor_2": "off",
        "sensor.wind_speed": "3",
        "input_boolean.kvety_on": "off",
        "input_boolean.kvety_rucny_override": "off",
        "input_boolean.spalna_routing": "off",
        "input_boolean.vac_prisiel_domov": "off",
        "input_number.kvety_pozicia_zaluzie": "34",
    }
    return World(states=base | states)


def decide(**targets) -> Decision:
    """A `Decision` built by hand: these tests are about guards, not about rules."""
    return Decision(
        mode="bezny",
        targets=dict(targets),
        trace=dict.fromkeys(targets, "bezny.test#0"),
    )


def close_all(position=0) -> Decision:
    return decide(**{entity: Action(position=position, tilt=0) for entity in ALL_BLINDS})


def guarded(config, w, decision, positions):
    return review(config, w, decision, positions, screen(config, w))


def open_positions(value=100):
    return dict.fromkeys(ALL_BLINDS, value)


# --------------------------------------------------------------------------
# The fixture itself has to be a configuration the project would accept.
# --------------------------------------------------------------------------


def test_the_house_inventory_is_expressible_and_validates(house):
    assert [p for p in validate(house) if p.severity == ERROR] == []
    assert len(house.guards) == 7
    # Order is meaning: wind first, because it is the one that must work when
    # everything else is broken.
    assert house.guards[0].name == "wind protection"


def test_the_fixture_exercises_every_policy_and_both_stages(house):
    assert {g.policy for g in house.guards} == {GUARD_SKIP, GUARD_FORCE, GUARD_DEFER}
    assert {g.stage for g in house.guards} == {GUARD_STAGE_INPUT, GUARD_STAGE_OUTPUT}
    assert {g.applies_to for g in house.guards} == {GUARD_ANY, GUARD_CLOSING}


# --------------------------------------------------------------------------
# No guards at all must behave exactly as no guards at all.
# --------------------------------------------------------------------------


def test_a_config_without_guards_changes_nothing(house):
    bare = load_config(HOUSE.split("guards:", maxsplit=1)[0])
    assert bare.guards == ()
    w = world()
    decision = evaluate(bare, w)
    assert decision.targets  # the engine really did decide something
    result = guarded(bare, w, decision, open_positions())
    assert result.actions == decision.targets
    assert result.deferrals == {}
    assert {o.reason for o in result.outcomes.values()} == {NO_GUARD}
    assert all(o.guard is None and o.policy is None for o in result.outcomes.values())


def test_every_decided_blind_gets_exactly_one_outcome(house):
    w = world(**{"binary_sensor.obyvacka_dvere_senzor": "on", "input_boolean.kvety_on": "on"})
    result = guarded(house, w, close_all(), open_positions())
    assert set(result.outcomes) == set(ALL_BLINDS)
    assert all(entity == outcome.entity for entity, outcome in result.outcomes.items())


# --------------------------------------------------------------------------
# Stages.
# --------------------------------------------------------------------------


def test_screen_cannot_be_handed_a_decision_or_positions():
    # The structural half of "a caller cannot run one stage at the other's
    # moment": there is no parameter through which it could.
    assert list(inspect.signature(screen).parameters) == ["config", "world"]
    assert list(inspect.signature(review).parameters) == [
        "config",
        "world",
        "decision",
        "positions",
        "screening",
    ]


def test_review_cannot_run_without_a_screening(house):
    w = world()
    with pytest.raises(TypeError):
        review(house, w, close_all(), open_positions())  # type: ignore[call-arg]


def test_an_input_guard_claims_its_blinds_before_the_engine_is_asked(house):
    quiet = screen(house, world())
    assert quiet.outcomes == {}
    assert quiet.remaining == frozenset(ALL_BLINDS)

    routed = screen(house, world(**{"input_boolean.spalna_routing": "on"}))
    assert set(routed.outcomes) == {BEDROOM_DOOR}
    assert routed.outcomes[BEDROOM_DOOR].stage == GUARD_STAGE_INPUT
    assert routed.outcomes[BEDROOM_DOOR].action is None
    assert BEDROOM_DOOR not in routed.remaining
    assert routed.remaining == frozenset(ALL_BLINDS) - {BEDROOM_DOOR}


def test_a_blind_claimed_at_the_input_stage_is_never_judged_again(house):
    # Both stages want this blind: the input router and the closing-door skip.
    # The input stage ran first, so the output guard must not touch it -- and
    # the counter-half proves the output guard really would have.
    w = world(
        **{
            "input_boolean.spalna_routing": "on",
            "binary_sensor.spalna_dvere_senzor_2": "on",
        }
    )
    result = guarded(house, w, close_all(), open_positions())
    outcome = result.outcomes[BEDROOM_DOOR]
    assert outcome.stage == GUARD_STAGE_INPUT
    assert outcome.guard == 1

    without_routing = guarded(
        house,
        world(**{"binary_sensor.spalna_dvere_senzor_2": "on"}),
        close_all(),
        open_positions(),
    )
    assert without_routing.outcomes[BEDROOM_DOOR].stage == GUARD_STAGE_OUTPUT
    assert without_routing.outcomes[BEDROOM_DOOR].guard == 5


def test_an_output_guard_never_runs_at_the_input_stage(house):
    # Every output-stage condition in the fixture is true at once. `screen`
    # must still claim nothing: none of them is an input guard.
    w = world(
        **{
            "sensor.wind_speed": "80",
            "binary_sensor.obyvacka_dvere_senzor": "on",
            "binary_sensor.spalna_dvere_senzor_2": "on",
            "input_boolean.kvety_on": "on",
            "input_boolean.kvety_rucny_override": "on",
            "input_boolean.vac_prisiel_domov": "on",
        }
    )
    assert screen(house, w).outcomes == {}
    # ...and the counter: the same world does fire output guards.
    result = review(house, w, close_all(), open_positions(), screen(house, w))
    assert all(o.guard is not None for o in result.outcomes.values())


# --------------------------------------------------------------------------
# Direction. `closing` is a decreasing position and never the slats.
# --------------------------------------------------------------------------

DIRECTION_ONLY = """
blinds:
  - entity: cover.a
zones:
  z: { members: [cover.a] }
modes:
  - id: m
rules:
  m.z:
    - then: { position: 100 }
guards:
  - name: directional
    policy: skip
    applies_to: {direction}
"""


def direction_config(direction: str):
    return load_config(DIRECTION_ONLY.replace("{direction}", direction))


def fires(direction: str, action: Action, current):
    config = direction_config(direction)
    w = World(states={})
    result = review(
        config, w, decide(**{"cover.a": action}), {"cover.a": current}, screen(config, w)
    )
    return result.outcomes["cover.a"].guard is not None


@pytest.mark.parametrize("direction", [GUARD_ANY, GUARD_CLOSING, GUARD_OPENING])
def test_direction_matches_a_hand_written_oracle_over_the_whole_grid(direction):
    fired = missed = 0
    for current in [None, 0, 3, 34, 50, 51, 100]:
        for position in [0, 3, 34, 50, 51, 100, KEEP]:
            action = Action(position=position, tilt=0)
            if direction == GUARD_ANY:
                expected = True
            elif position is KEEP:
                expected = False
            elif current is None:
                expected = True
            elif direction == GUARD_CLOSING:
                expected = position < current
            else:
                expected = position > current
            got = fires(direction, action, current)
            assert got is expected, (direction, current, position)
            fired += got
            missed += not got
    assert fired > 0, "the grid never fired the guard"
    if direction != GUARD_ANY:
        assert missed > 0, "the grid never failed to fire the guard"


def test_a_closing_guard_ignores_the_slats(house):
    # The load-bearing reading. An action that only shuts the slats is not a
    # close, and nine of the house's thirteen interlocks depend on it: they
    # have always let tilt commands through.
    w = world(**{"binary_sensor.spalna_dvere_senzor_2": "on"})
    tilt_only = decide(**{BEDROOM_DOOR: Action(position=KEEP, tilt=0)})
    result = review(house, w, tilt_only, {BEDROOM_DOOR: 100}, screen(house, w))
    assert result.outcomes[BEDROOM_DOOR].guard is None
    assert result.actions[BEDROOM_DOOR] == Action(position=KEEP, tilt=0)

    # Counter: the same guard, same world, on an action that does lower it.
    lowering = decide(**{BEDROOM_DOOR: Action(position=0, tilt=0)})
    blocked = review(house, w, lowering, {BEDROOM_DOOR: 100}, screen(house, w))
    assert blocked.outcomes[BEDROOM_DOOR].guard == 5
    assert blocked.actions == {}


def test_a_closing_guard_ignores_a_falling_tilt_at_an_unchanged_position(house):
    # Position identical to where the blind already is, slats driven shut: the
    # position axis is not decreasing, so the door guard has nothing to say.
    w = world(**{"binary_sensor.spalna_dvere_senzor_2": "on"})
    action = Action(position=100, tilt=0)
    result = review(
        house, w, decide(**{BEDROOM_DOOR: action}), {BEDROOM_DOOR: 100}, screen(house, w)
    )
    assert result.outcomes[BEDROOM_DOOR].guard is None


def test_an_unreadable_position_makes_a_directional_guard_fire(house):
    w = world(**{"binary_sensor.spalna_dvere_senzor_2": "on"})
    action = Action(position=100, tilt=100)  # would be an *opening* if readable
    decision = decide(**{BEDROOM_DOOR: action})

    known = review(house, w, decision, {BEDROOM_DOOR: 0}, screen(house, w))
    assert known.outcomes[BEDROOM_DOOR].guard is None, "a rise must not trip a closing guard"

    unknown = review(house, w, decision, {BEDROOM_DOOR: None}, screen(house, w))
    assert unknown.outcomes[BEDROOM_DOOR].guard == 5

    missing = review(house, w, decision, {}, screen(house, w))
    assert missing.outcomes[BEDROOM_DOOR].guard == 5, "a missing key means the same as None"


def test_a_decision_that_still_holds_a_ref_is_refused_not_guessed(house):
    w = world(**{"binary_sensor.spalna_dvere_senzor_2": "on"})
    unresolved = decide(**{BEDROOM_DOOR: Action(position=Ref("input_number.x", 50))})
    with pytest.raises(GuardError, match="unresolved"):
        review(house, w, unresolved, {BEDROOM_DOOR: 100}, screen(house, w))


# --------------------------------------------------------------------------
# First match wins, in written order.
# --------------------------------------------------------------------------


def test_only_the_first_matching_guard_answers_for_a_blind(house):
    # Both the manual-override skip (#2) and the flower keeper (#3) match.
    # The skip is written first, so the keeper must not also run -- and the
    # counter-half proves the keeper would have, on its own.
    both = world(
        **{
            "input_boolean.kvety_on": "on",
            "input_boolean.kvety_rucny_override": "on",
        }
    )
    result = guarded(house, both, close_all(), open_positions())
    for entity in (FLOWERS_A, FLOWERS_B):
        assert result.outcomes[entity].guard == 2
        assert result.outcomes[entity].policy == GUARD_SKIP
        assert result.outcomes[entity].action is None

    keeper_only = guarded(
        house, world(**{"input_boolean.kvety_on": "on"}), close_all(), open_positions()
    )
    for entity in (FLOWERS_A, FLOWERS_B):
        assert keeper_only.outcomes[entity].guard == 3
        assert keeper_only.outcomes[entity].policy == GUARD_FORCE
        assert keeper_only.outcomes[entity].action == Action(position=34, tilt=KEEP)


def test_wind_is_written_first_so_it_beats_every_other_guard(house):
    # The rationale's own worked collision: the disarm branch force-opening
    # the terrace while wind protection is force-closing it, with nothing
    # refereeing. Here the list order is the referee.
    storm = world(
        **{
            "sensor.wind_speed": "80",
            "binary_sensor.obyvacka_dvere_senzor": "on",
            "input_boolean.kvety_on": "on",
            "input_boolean.vac_prisiel_domov": "on",
        }
    )
    result = guarded(house, storm, close_all(), open_positions(0))
    assert {o.guard for o in result.outcomes.values()} == {0}
    assert all(o.action == Action(position=100, tilt=100) for o in result.outcomes.values())

    # Counter: without the wind, four different guards answer instead, so the
    # assertion above is not passing because nothing else could ever fire.
    calm = world(
        **{
            "binary_sensor.obyvacka_dvere_senzor": "on",
            "binary_sensor.spalna_dvere_senzor_2": "on",
            "input_boolean.kvety_on": "on",
            "input_boolean.vac_prisiel_domov": "on",
        }
    )
    calm_result = guarded(house, calm, close_all(), open_positions(50))
    assert {o.guard for o in calm_result.outcomes.values()} == {3, 4, 5}


def test_a_later_guard_still_answers_for_a_blind_the_earlier_one_does_not_name(house):
    w = world(**{"input_boolean.vac_prisiel_domov": "on"})
    result = guarded(house, w, close_all(), open_positions(0))
    # The terrace pair is claimed by "home again" (#6); the bedroom and the
    # flowers, which it does not name, fall through to no guard at all.
    assert result.outcomes[LIVING_DOOR].guard == 6
    assert result.outcomes[LIVING_DOOR_2].guard == 6
    assert result.outcomes[BEDROOM_DOOR].reason == NO_GUARD
    assert result.outcomes[FLOWERS_A].reason == NO_GUARD


# --------------------------------------------------------------------------
# The three policies.
# --------------------------------------------------------------------------


def test_skip_suppresses_the_whole_action_not_half_of_it(house):
    w = world(**{"binary_sensor.spalna_dvere_senzor_2": "on"})
    result = guarded(house, w, close_all(), open_positions())
    outcome = result.outcomes[BEDROOM_DOOR]
    assert outcome.policy == GUARD_SKIP
    assert outcome.action is None
    assert BEDROOM_DOOR not in result.actions
    assert outcome.deferral is None


def test_force_replaces_the_action_and_resolves_its_refs(house):
    w = world(**{"input_boolean.kvety_on": "on", "input_number.kvety_pozicia_zaluzie": "41.9"})
    result = guarded(house, w, close_all(), open_positions())
    outcome = result.outcomes[FLOWERS_A]
    assert outcome.policy == GUARD_FORCE
    # Truncated, not rounded -- the engine's rule, shared, not re-implemented.
    assert outcome.action == Action(position=41, tilt=KEEP)
    assert result.actions[FLOWERS_A] == Action(position=41, tilt=KEEP)
    assert outcome.deferral is None


def test_a_defer_carries_its_whole_deadline_not_just_a_verdict(house):
    w = world(**{"binary_sensor.obyvacka_dvere_senzor": "on"})
    decision = close_all()
    result = guarded(house, w, decision, open_positions())

    outcome = result.outcomes[LIVING_DOOR]
    assert outcome.policy == GUARD_DEFER
    assert outcome.action is None, "a deferred action must not be performed now"

    deferral = outcome.deferral
    assert deferral is not None
    assert deferral.guard == 4
    assert deferral.name == "terrace door or sauna"
    assert deferral.stage == GUARD_STAGE_OUTPUT
    assert deferral.max_wait == 10800
    assert deferral.on_timeout == "abandon"
    assert deferral.recheck_every == 900
    # What "proceed" would mean: the action the guard is holding back.
    assert deferral.held == decision.targets[LIVING_DOOR]

    assert set(result.deferrals) == {LIVING_DOOR, LIVING_DOOR_2}


def test_a_defer_at_the_input_stage_holds_nothing_because_nothing_was_decided():
    config = load_config("""
blinds:
  - entity: cover.a
zones:
  z: { members: [cover.a] }
modes:
  - id: m
rules:
  m.z:
    - then: { position: 0 }
guards:
  - name: wait for the door
    policy: defer
    stage: input
    applies_to: any
    when:
      condition: state
      entity_id: binary_sensor.door
      state: "on"
    max_wait: null
    on_timeout: proceed
""")
    w = World(states={"binary_sensor.door": "on"})
    screening = screen(config, w)
    deferral = screening.outcomes["cover.a"].deferral
    assert deferral is not None
    assert deferral.stage == GUARD_STAGE_INPUT
    assert deferral.held is None, "no decision was ever made, so proceeding means asking again"
    assert deferral.max_wait is None, "null is a value: wait indefinitely"
    assert deferral.on_timeout == "proceed"
    assert deferral.recheck_every == 900, "the parser fills the watchdog interval in"

    # Counter: with the door shut the guard does not fire at all.
    assert screen(config, World(states={"binary_sensor.door": "off"})).outcomes == {}


def test_every_outcome_says_which_guard_and_which_policy(house):
    w = world(
        **{
            "binary_sensor.obyvacka_dvere_senzor": "on",
            "binary_sensor.spalna_dvere_senzor_2": "on",
            "input_boolean.kvety_on": "on",
        }
    )
    result = guarded(house, w, close_all(), open_positions())
    explained = 0
    for entity, outcome in result.outcomes.items():
        if outcome.guard is None:
            assert outcome.reason == NO_GUARD
            assert outcome.policy is None
            assert outcome.stage is None
            continue
        explained += 1
        guard = house.guards[outcome.guard]
        assert outcome.policy == guard.policy
        assert outcome.stage == guard.stage
        assert outcome.reason == f"{guard.label(outcome.guard)}: {guard.policy}"
        assert f"#{outcome.guard}" in outcome.reason
        assert entity in guard_blinds(house, guard)
        assert (outcome.deferral is not None) is (guard.policy == GUARD_DEFER)
        assert (outcome.action is None) is (guard.policy != GUARD_FORCE)
    assert explained == len(ALL_BLINDS)


# --------------------------------------------------------------------------
# What the module refuses rather than guesses at.
# --------------------------------------------------------------------------

MINIMAL = """
blinds:
  - entity: cover.a
zones:
  z: { members: [cover.a] }
modes:
  - id: m
rules:
  m.z:
    - then: { position: 0 }
guards:
  - {body}
"""


def broken(body: str):
    return load_config(MINIMAL.replace("{body}", body))


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("{ policy: nonsense }", "unknown policy"),
        ("{ policy: skip, stage: halfway }", "unknown stage"),
        ("{ policy: skip, applies_to: sideways }", "unknown 'applies_to'"),
        ("{ policy: skip, stage: input, applies_to: closing }", "cannot name a direction"),
        ("{ policy: force }", "no 'then'"),
        ("{ policy: defer, on_timeout: proceed }", "without 'max_wait'"),
        ("{ policy: defer, max_wait: 60 }", "unusable 'on_timeout'"),
        ("{ policy: defer, max_wait: 60, on_timeout: whenever }", "unusable 'on_timeout'"),
    ],
)
def test_a_guard_that_cannot_be_honoured_stops_the_evaluation(body, match):
    config = broken(body)
    w = World(states={})
    with pytest.raises(GuardError, match=match):
        screen(config, w)
    with pytest.raises(GuardError, match=match):
        review(config, w, decide(**{"cover.a": Action(0, 0)}), {"cover.a": 100}, _empty_screening())


def _empty_screening():
    return Screening(outcomes={}, remaining=frozenset())


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("{ policy: nonsense }", "guard_unknown_policy"),
        ("{ policy: skip, stage: halfway }", "guard_bad_stage"),
        ("{ policy: skip, applies_to: sideways }", "guard_bad_direction"),
        ("{ policy: skip, stage: input, applies_to: closing }", "guard_input_direction"),
        ("{ policy: force }", "guard_force_needs_action"),
        ("{ policy: defer, max_wait: 60 }", "guard_defer_needs_timeout"),
    ],
)
def test_everything_guards_py_refuses_is_also_a_validation_error(body, code):
    # A guard that raises at evaluation time but validates clean would be a
    # configuration the UI happily saves and the house then cannot decide with.
    problems = [p for p in validate(broken(body)) if p.severity == ERROR]
    assert code in {p.code for p in problems}, problems


def test_an_input_guard_may_still_say_any(house):
    # The converse of the refusal above: `stage: input` is not itself the
    # problem, only pairing it with a direction is.
    config = broken("{ policy: skip, stage: input, applies_to: any }")
    assert [p.code for p in validate(config) if p.code == "guard_input_direction"] == []
    assert screen(config, World(states={})).outcomes["cover.a"].guard == 0


def test_a_guard_naming_a_blind_no_zone_owns_is_the_engines_error_not_a_silent_pass():
    config = load_config("""
blinds:
  - entity: cover.a
  - entity: cover.orphan
zones:
  z: { members: [cover.a] }
modes:
  - id: m
rules:
  m.z:
    - then: { position: 0 }
""")
    with pytest.raises(EngineError, match="owned by no zone"):
        screen(config, World(states={}))


def test_guards_never_invent_a_blind(house):
    w = world(**{"sensor.wind_speed": "99"})
    result = guarded(house, w, close_all(), open_positions())
    assert set(result.outcomes) <= set(house.blinds)
    assert set(result.actions) <= set(house.blinds)


def test_a_zone_target_covers_its_members_and_nothing_else(house):
    by_name = {g.name: g for g in house.guards}
    assert guard_blinds(house, by_name["terrace door or sauna"]) == {LIVING_DOOR, LIVING_DOOR_2}
    assert guard_blinds(house, by_name["bedroom door open"]) == {BEDROOM_DOOR}
    assert guard_blinds(house, by_name["wind protection"]) == set(ALL_BLINDS)
