"""Waiting a `defer` out: the clock, the two timeout answers, and the restart.

Deliberately driven from a real `guards:` configuration through `screen`/
`review` rather than from hand-built `Deferral` objects wherever the point
being made involves the guards at all. Three defects have shipped past a green
suite in this project's history because the test exercised a layer below the
one that broke; a registry test fed hand-made deferrals would keep passing if
`review` stopped producing them.

Every implication-shaped assertion below carries a counter -- "nothing was ever
deferred" satisfies most of them vacuously.
"""

import pytest

from cover_logic.config_schema import load_config
from cover_logic.const import GUARD_DEFAULT_RECHECK, GUARD_TIMEOUT_ABANDON, GUARD_TIMEOUT_PROCEED
from cover_logic.deferrals import HOLD, MIN_RECHECK, DeferralRegistry, verdict
from cover_logic.engine import Decision, evaluate
from cover_logic.guards import Deferral, Guarded, Outcome, review, screen
from cover_logic.model import KEEP, Action
from cover_logic.validation import ERROR, validate
from cover_logic.world import World

TERRACE = "cover.terasa"
BEDROOM = "cover.spalna"

# Two deferring guards, one per stage, so both meanings of `Deferral.held` are
# exercised by the same fixture: the `output` one holds a decided action, the
# `input` one holds nothing and means "ask the engine again".
#
# `on_timeout` differs between them on purpose too -- they are opposites, both
# are in real use in the house, and a fixture that only had one of them could
# not tell "honours on_timeout" from "always proceeds".
CONFIG_TEXT = """
blinds:
  - entity: cover.terasa
  - entity: cover.spalna
zones:
  terasa:
    members: [cover.terasa]
  spalna:
    members: [cover.spalna]
conditions:
  dvere_otvorene:
    condition: state
    entity_id: binary_sensor.dvere
    state: "on"
  sauna_bezi:
    condition: state
    entity_id: binary_sensor.sauna
    state: "on"
modes:
  - id: bezny
rules:
  bezny.terasa:
    - then: {position: 0, tilt: 0}
  bezny.spalna:
    - then: {position: 0, tilt: 0}
guards:
  - name: terrace door
    policy: defer
    applies_to: closing
    targets: [terasa]
    when: !ref dvere_otvorene
    max_wait: 600
    on_timeout: proceed
    recheck_every: 60

  - name: sauna routes elsewhere
    policy: defer
    stage: input
    applies_to: any
    targets: [spalna]
    when: !ref sauna_bezi
    max_wait: 300
    on_timeout: abandon
    recheck_every: 30
"""


@pytest.fixture(scope="module")
def config():
    """The fixture must itself be a configuration this project would accept."""
    parsed = load_config(CONFIG_TEXT)
    assert [p for p in validate(parsed) if p.severity == ERROR] == []
    return parsed


def world(**states) -> World:
    """A snapshot where every entity the fixture reads is quiet unless named."""
    return World(states={"binary_sensor.dvere": "off", "binary_sensor.sauna": "off"} | states)


def run(config, w, positions=None):
    """`(decision, guarded)` for one world -- the whole path a coordinator runs."""
    screening = screen(config, w)
    decision = evaluate(config, w)
    positions = positions if positions is not None else dict.fromkeys(config.blinds, 100)
    return decision, review(config, w, decision, positions, screening)


# ---------------------------------------------------------------------------
# The fixture really does defer, and really does defer differently per stage.
# ---------------------------------------------------------------------------


def test_the_fixture_defers_at_both_stages(config):
    """The counter to every "nothing happened" below: both guards can fire."""
    _decision, guarded = run(config, world(**{"binary_sensor.dvere": "on"}))
    assert set(guarded.deferrals) == {TERRACE}
    assert guarded.deferrals[TERRACE].held == Action(position=0, tilt=0)

    _decision, guarded = run(config, world(**{"binary_sensor.sauna": "on"}))
    assert set(guarded.deferrals) == {BEDROOM}
    # The input stage has no decision to hold back -- this is what makes
    # `proceed` mean "ask the engine again" rather than "perform this".
    assert guarded.deferrals[BEDROOM].held is None


def test_a_quiet_world_defers_nothing(config):
    _decision, guarded = run(config, world())
    assert guarded.deferrals == {}
    assert set(guarded.actions) == {TERRACE, BEDROOM}


# ---------------------------------------------------------------------------
# `verdict`: the deadline, and nothing but the deadline.
# ---------------------------------------------------------------------------


def _deferral(**overrides):
    base = {
        "guard": 0,
        "name": "g",
        "stage": "output",
        "max_wait": 600,
        "on_timeout": GUARD_TIMEOUT_PROCEED,
        "recheck_every": 60,
        "held": Action(position=0, tilt=0),
    }
    return Deferral(**(base | overrides))


@pytest.mark.parametrize(
    ("waited", "expected"),
    [
        (0, HOLD),
        (599, HOLD),
        (599.999, HOLD),
        (600, GUARD_TIMEOUT_PROCEED),
        (10_000, GUARD_TIMEOUT_PROCEED),
    ],
)
def test_verdict_holds_until_the_deadline_and_then_answers(waited, expected):
    assert verdict(_deferral(), waited) == expected


def test_verdict_returns_the_guards_own_on_timeout_not_a_fixed_answer(config):
    """The counter to a `verdict` hard-coded to `proceed`.

    Both spellings come out of the same function on the same elapsed time,
    decided only by what the guard says.
    """
    assert verdict(_deferral(on_timeout=GUARD_TIMEOUT_PROCEED), 601) == GUARD_TIMEOUT_PROCEED
    assert verdict(_deferral(on_timeout=GUARD_TIMEOUT_ABANDON), 601) == GUARD_TIMEOUT_ABANDON


def test_a_null_max_wait_holds_forever():
    """`max_wait: null` is "wait indefinitely" -- a value, not an omission."""
    assert verdict(_deferral(max_wait=None), 10**9) == HOLD


# ---------------------------------------------------------------------------
# The registry: one clock per wait, started once.
# ---------------------------------------------------------------------------


def test_the_clock_starts_once_and_keeps_running_across_evaluations(config):
    registry = DeferralRegistry()
    w = world(**{"binary_sensor.dvere": "on"})

    decision, guarded = run(config, w)
    assert registry.sync(guarded, decision, now=1000.0).proceed == {}

    # A second evaluation 300s later must not restart the clock -- if it did,
    # a house whose sensors report every few minutes would never time out.
    decision, guarded = run(config, w)
    elapsed = registry.sync(guarded, decision, now=1300.0)
    assert elapsed.proceed == {}
    assert registry.pending[TERRACE].since == 1000.0
    assert registry.as_attributes(1300.0)[TERRACE]["waited"] == 300

    decision, guarded = run(config, w)
    elapsed = registry.sync(guarded, decision, now=1600.0)
    assert elapsed.proceed == {TERRACE: Action(position=0, tilt=0)}


def test_a_released_guard_drops_the_wait_and_the_engines_answer_is_dispatched(config):
    registry = DeferralRegistry()
    open_door = world(**{"binary_sensor.dvere": "on"})

    decision, guarded = run(config, open_door)
    registry.sync(guarded, decision, now=1000.0)
    assert TERRACE in registry.pending  # counter: it really was waiting

    decision, guarded = run(config, world())
    elapsed = registry.sync(guarded, decision, now=1100.0)
    assert TERRACE not in registry.pending
    assert elapsed.proceed == {}
    assert elapsed.abandoned == ()
    # Nothing special happens here on purpose: the blind is simply back in the
    # ordinary decision, which is what the caller dispatches.
    assert guarded.actions[TERRACE] == Action(position=0, tilt=0)


def test_a_new_wait_after_a_release_starts_a_fresh_clock(config):
    registry = DeferralRegistry()
    open_door = world(**{"binary_sensor.dvere": "on"})

    decision, guarded = run(config, open_door)
    registry.sync(guarded, decision, now=1000.0)

    decision, guarded = run(config, world())
    registry.sync(guarded, decision, now=1100.0)

    decision, guarded = run(config, open_door)
    registry.sync(guarded, decision, now=1200.0)
    assert registry.pending[TERRACE].since == 1200.0
    # And it therefore does not time out at what would have been the first
    # wait's deadline.
    decision, guarded = run(config, open_door)
    assert registry.sync(guarded, decision, now=1600.0).proceed == {}


# ---------------------------------------------------------------------------
# The two timeout answers, end to end from the configuration.
# ---------------------------------------------------------------------------


def test_proceed_performs_the_action_the_guard_held(config):
    registry = DeferralRegistry()
    w = world(**{"binary_sensor.dvere": "on"})

    decision, guarded = run(config, w)
    registry.sync(guarded, decision, now=0.0)
    decision, guarded = run(config, w)
    elapsed = registry.sync(guarded, decision, now=600.0)

    assert elapsed.proceed == {TERRACE: decision.targets[TERRACE]}
    assert elapsed.abandoned == ()
    assert registry.pending[TERRACE].resolved == GUARD_TIMEOUT_PROCEED


def test_abandon_performs_nothing_and_says_so(config):
    """The counter to `test_proceed_...`: the same machinery, the opposite guard."""
    registry = DeferralRegistry()
    w = world(**{"binary_sensor.sauna": "on"})

    decision, guarded = run(config, w)
    registry.sync(guarded, decision, now=0.0)
    decision, guarded = run(config, w)
    elapsed = registry.sync(guarded, decision, now=300.0)

    assert elapsed.proceed == {}
    assert elapsed.abandoned == (BEDROOM,)
    assert registry.pending[BEDROOM].resolved == GUARD_TIMEOUT_ABANDON


def test_an_input_stage_proceed_asks_the_engine_again(config):
    """`held` is `None` at the input stage, so `proceed` uses the fresh decision.

    Built by flipping the input guard's own `on_timeout` to `proceed`: the
    fixture's is `abandon`, and this is the branch that would otherwise never
    run.
    """
    proceeding = load_config(CONFIG_TEXT.replace("on_timeout: abandon", "on_timeout: proceed"))
    registry = DeferralRegistry()
    w = world(**{"binary_sensor.sauna": "on"})

    decision, guarded = run(proceeding, w)
    assert guarded.deferrals[BEDROOM].held is None  # counter: there is nothing held
    registry.sync(guarded, decision, now=0.0)

    decision, guarded = run(proceeding, w)
    elapsed = registry.sync(guarded, decision, now=300.0)
    assert elapsed.proceed == {BEDROOM: decision.targets[BEDROOM]}
    assert elapsed.proceed[BEDROOM] == Action(position=0, tilt=0)


def test_an_input_stage_proceed_with_no_engine_answer_abandons_instead():
    """A blind the decision does not name has nothing to proceed *to*.

    Unreachable from `evaluate` (it is total over `config.blinds`), so it is
    built directly -- the point is that the fallback is "do nothing", never an
    invented movement.
    """
    registry = DeferralRegistry()
    deferral = Deferral(
        guard=0,
        name="input",
        stage="input",
        max_wait=0,
        on_timeout=GUARD_TIMEOUT_PROCEED,
        recheck_every=10,
        held=None,
    )
    guarded = _guarded_with(deferral, TERRACE)
    empty = Decision(mode="bezny", targets={}, trace={})
    elapsed = registry.sync(guarded, empty, now=0.0)
    assert elapsed.proceed == {}
    assert elapsed.abandoned == (TERRACE,)


def _guarded_with(deferral, entity):
    """One blind, deferred by `deferral` -- the shape `review` would have produced."""
    return Guarded(
        outcomes={
            entity: Outcome(
                entity=entity,
                action=None,
                reason="guard #0: defer",
                guard=0,
                policy="defer",
                stage=deferral.stage,
                deferral=deferral,
            )
        }
    )


# ---------------------------------------------------------------------------
# A resolved wait answers once, not once per `max_wait`.
# ---------------------------------------------------------------------------


def test_a_resolved_wait_does_not_fire_again_while_its_guard_still_holds(config):
    """The sauna stays on; the blind must not be re-closed every ten minutes.

    Dropping the record on resolution would look correct in a single-shot test
    and would make the house move on a timer for as long as the condition
    lasted -- "it moved and nobody could say why", with a clock attached.
    """
    registry = DeferralRegistry()
    w = world(**{"binary_sensor.dvere": "on"})

    decision, guarded = run(config, w)
    registry.sync(guarded, decision, now=0.0)
    decision, guarded = run(config, w)
    assert registry.sync(guarded, decision, now=600.0).proceed  # counter: it did fire once

    for later in (601.0, 1200.0, 1201.0, 60_000.0):
        decision, guarded = run(config, w)
        elapsed = registry.sync(guarded, decision, now=later)
        assert elapsed.proceed == {}, f"fired again at {later}"
        assert elapsed.abandoned == ()


def test_a_resolved_wait_is_released_when_its_guard_stops_firing(config):
    registry = DeferralRegistry()
    open_door = world(**{"binary_sensor.dvere": "on"})

    decision, guarded = run(config, open_door)
    registry.sync(guarded, decision, now=0.0)
    decision, guarded = run(config, open_door)
    registry.sync(guarded, decision, now=600.0)
    assert registry.pending[TERRACE].resolved == GUARD_TIMEOUT_PROCEED

    decision, guarded = run(config, world())
    registry.sync(guarded, decision, now=700.0)
    assert registry.pending == {}

    # ...and a later wait is a real wait again, not a permanently spent one.
    decision, guarded = run(config, open_door)
    registry.sync(guarded, decision, now=800.0)
    decision, guarded = run(config, open_door)
    assert registry.sync(guarded, decision, now=1400.0).proceed == {
        TERRACE: Action(position=0, tilt=0)
    }


def test_a_different_guard_taking_over_starts_a_new_wait(config):
    """Guards are first-match-wins; a higher one taking over is not a continuation.

    Built by hand because producing a takeover from the fixture would need a
    third guard whose only purpose is this test.
    """
    registry = DeferralRegistry()
    first = Deferral(
        guard=0,
        name="a",
        stage="output",
        max_wait=600,
        on_timeout=GUARD_TIMEOUT_PROCEED,
        recheck_every=60,
        held=Action(position=0, tilt=0),
    )
    second = Deferral(
        guard=1,
        name="b",
        stage="output",
        max_wait=600,
        on_timeout=GUARD_TIMEOUT_PROCEED,
        recheck_every=60,
        held=Action(position=0, tilt=0),
    )
    empty = Decision(mode="bezny", targets={}, trace={})

    registry.sync(_guarded_with(first, TERRACE), empty, now=0.0)
    assert registry.pending[TERRACE].since == 0.0
    registry.sync(_guarded_with(second, TERRACE), empty, now=500.0)
    assert registry.pending[TERRACE].since == 500.0
    # The first guard's deadline passes with the second one in charge: nothing
    # fires, because the second one's own clock started later.
    assert registry.sync(_guarded_with(second, TERRACE), empty, now=900.0).proceed == {}


# ---------------------------------------------------------------------------
# Restart resilience: the wait is a derived fact, not an in-flight `await`.
# ---------------------------------------------------------------------------


def test_a_brand_new_registry_re_derives_the_wait(config):
    """What a restart looks like to this module: a fresh object, the same world.

    Nothing is carried over and nothing needs to be. The guard is still in the
    configuration and its condition is still true, so `review` produces the
    deferral again and the new registry adopts it -- which is the entire
    difference between this and a `wait_for_trigger`, whose intent a restart
    destroys outright.
    """
    w = world(**{"binary_sensor.dvere": "on"})

    before = DeferralRegistry()
    decision, guarded = run(config, w)
    before.sync(guarded, decision, now=0.0)
    assert TERRACE in before.pending  # counter: something was actually waiting

    after = DeferralRegistry()  # the restart
    decision, guarded = run(config, w)
    after.sync(guarded, decision, now=5.0)
    assert TERRACE in after.pending
    assert after.next_recheck(5.0) is not None

    # And it still times out, on its own timer, without any watched entity
    # changing after the restart.
    decision, guarded = run(config, w)
    assert after.sync(guarded, decision, now=605.0).proceed == {TERRACE: Action(position=0, tilt=0)}


# ---------------------------------------------------------------------------
# `next_recheck`: when the caller must come back even if nothing happens.
# ---------------------------------------------------------------------------


def test_nothing_pending_asks_for_no_timer(config):
    registry = DeferralRegistry()
    decision, guarded = run(config, world())
    registry.sync(guarded, decision, now=0.0)
    assert registry.next_recheck(0.0) is None


def test_the_timer_is_the_guards_own_recheck_when_the_deadline_is_far(config):
    registry = DeferralRegistry()
    decision, guarded = run(config, world(**{"binary_sensor.dvere": "on"}))
    registry.sync(guarded, decision, now=0.0)
    assert registry.next_recheck(0.0) == 60  # the guard's `recheck_every`


def test_the_timer_shortens_to_the_deadline_when_that_comes_first(config):
    """A fifteen-minute recheck must not make a two-minute `max_wait` late.

    Without this the fixture's 600s deadline would be examined at 60s
    intervals and land on time by luck; here the recheck is deliberately
    longer than what is left.
    """
    registry = DeferralRegistry()
    decision, guarded = run(config, world(**{"binary_sensor.dvere": "on"}))
    registry.sync(guarded, decision, now=0.0)
    assert registry.next_recheck(570.0) == 30.0
    assert registry.next_recheck(599.5) == MIN_RECHECK
    # Never negative, never zero: an overdue wait still asks for a real delay.
    assert registry.next_recheck(9_000.0) == MIN_RECHECK


def test_a_resolved_wait_asks_for_no_timer(config):
    registry = DeferralRegistry()
    w = world(**{"binary_sensor.dvere": "on"})
    decision, guarded = run(config, w)
    registry.sync(guarded, decision, now=0.0)
    assert registry.next_recheck(0.0) is not None  # counter
    decision, guarded = run(config, w)
    registry.sync(guarded, decision, now=600.0)
    assert registry.next_recheck(600.0) is None


def test_a_guard_with_no_recheck_still_gets_one():
    """`recheck_every: None` must not mean "re-examine never"."""
    registry = DeferralRegistry()
    deferral = Deferral(
        guard=0,
        name="g",
        stage="output",
        max_wait=None,
        on_timeout=GUARD_TIMEOUT_PROCEED,
        recheck_every=None,
        held=Action(position=KEEP, tilt=KEEP),
    )
    empty = Decision(mode="bezny", targets={}, trace={})
    registry.sync(_guarded_with(deferral, TERRACE), empty, now=0.0)
    assert registry.next_recheck(0.0) == GUARD_DEFAULT_RECHECK


# ---------------------------------------------------------------------------
# The sensor's view.
# ---------------------------------------------------------------------------


def test_attributes_name_the_guard_that_deferred_and_how_long_it_has_waited(config):
    registry = DeferralRegistry()
    decision, guarded = run(config, world(**{"binary_sensor.dvere": "on"}))
    registry.sync(guarded, decision, now=1000.0)

    view = registry.as_attributes(1450.0)
    assert set(view) == {TERRACE}
    assert view[TERRACE] == {
        "guard": 0,
        "name": "terrace door",
        "policy": "defer",
        "stage": "output",
        "state": "waiting",
        "waited": 450,
        "max_wait": 600,
        "on_timeout": GUARD_TIMEOUT_PROCEED,
        "recheck_every": 60,
    }


def test_attributes_say_when_a_wait_is_over(config):
    registry = DeferralRegistry()
    w = world(**{"binary_sensor.dvere": "on"})
    decision, guarded = run(config, w)
    registry.sync(guarded, decision, now=0.0)
    decision, guarded = run(config, w)
    registry.sync(guarded, decision, now=600.0)
    assert registry.as_attributes(600.0)[TERRACE]["state"] == GUARD_TIMEOUT_PROCEED


def test_the_pending_view_is_a_copy(config):
    registry = DeferralRegistry()
    decision, guarded = run(config, world(**{"binary_sensor.dvere": "on"}))
    registry.sync(guarded, decision, now=0.0)
    view = registry.pending
    view.clear()
    assert TERRACE in registry.pending
