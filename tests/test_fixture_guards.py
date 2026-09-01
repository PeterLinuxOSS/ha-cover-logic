"""The live house's own interlocks, as `fixtures/dom_peter.yaml` now states them.

`tests/test_guards.py` proves the *mechanism* against an invented house;
this module proves the *content* against the real one. Both halves are here
for every guard: it fires when it must, and -- the half that gets forgotten
-- it does not fire when it must not.

Every implication-shaped assertion carries a counter. "For each blind, if the
guard fired then the action is 100" is satisfied by a run in which nothing
fired at all, and a suite that passes because it never exercised the thing is
the failure mode this project has already shipped three times (`MODELS.md`
Sec. 9).

Two facts this module is the standing record of, both verified against the
live house on 2026-08-30:

- `binary_sensor.sauna_running` is a UI template helper and is **fail-closed**
  -- both its sources dead reads as `on`. The guard therefore tests its state
  directly and needs nothing else behind it.
- `cover.obyvacka_zaluzia_dvere_2` sits over a **window**, not over an opening
  leaf, despite its name. No door guard may name it, and
  `test_the_window_blind_named_dvere_is_never_guarded_on_a_door` is what keeps
  the next reader from "fixing" that.
"""

import datetime as dt

import pytest

from cover_logic.config_schema import load_config, load_config_file
from cover_logic.const import (
    GUARD_ANY,
    GUARD_CLOSING,
    GUARD_DEFER,
    GUARD_FORCE,
    GUARD_SKIP,
    GUARD_STAGE_INPUT,
    GUARD_STAGE_OUTPUT,
)
from cover_logic.engine import Decision, evaluate
from cover_logic.guards import NO_GUARD, guard_blinds, review, screen
from cover_logic.model import KEEP, UNSET, Action
from cover_logic.validation import validate
from cover_logic.world import World

NOW = dt.datetime(2026, 8, 30, 13, 0)

LIVING_DOOR = "cover.obyvacka_zaluzia_dvere_1_3"
LIVING_WINDOW = "cover.obyvacka_zaluzia_dvere_2"
BEDROOM_DOOR = "cover.spalna_zaluzia_dvere_1"
BEDROOM_WINDOW = "cover.spalna_zaluzia_2"
FLOWERS = ("cover.kuchyna_zaluzia_1_4", "cover.kuchyna_zaluzia_2_5")

# The guards' positions in the fixture's list. Position *is* a guard's
# identity (it has no id of its own), so naming them here rather than
# repeating integers is the only way a reordering shows up as one failure
# instead of twenty.
WIND = 0
FLOWERS_BY_HAND = 1
TERRACE_DEFER = 2
TERRACE_FORCE = 3
BEDROOM_DEFER = 4
BEDROOM_FORCE = 5

# Every entity the fixture reads, in the state that makes no guard fire: no
# wind, both terrace doors shut, sauna cold, nobody's hand on the flowers.
CALM = {
    "input_boolean.cover_down": "off",
    "alarm_control_panel.alarmo": "disarmed",
    "input_boolean.teplotna_ochrana_dom": "off",
    "input_boolean.lighting_on": "off",
    "input_boolean.kvety_on": "on",
    "input_boolean.kvety_rucny_override": "off",
    "input_boolean.zaluzie_kuchyna_rucne": "off",
    "binary_sensor.is_home": "on",
    "input_boolean.some_sleeping": "off",
    "binary_sensor.peter_home": "on",
    "binary_sensor.mimka_home": "on",
    "binary_sensor.pavel_home": "on",
    "binary_sensor.majka_home": "off",
    "input_boolean.zaluzie_aktivna_peter": "on",
    "input_boolean.zaluzie_aktivna_mimka": "on",
    "input_boolean.zaluzie_aktivna_spalna": "on",
    "sun.sun": "above_horizon",
    "sensor.sun_solar_azimuth": "180",
    "weather.openweathermap": "sunny",
    "input_number.kvety_pozicia_zaluzie": "34",
    "binary_sensor.obyvacka_dvere_senzor": "off",
    "binary_sensor.spalna_dvere_senzor_2": "off",
    "binary_sensor.sauna_running": "off",
    "sensor.netatmo_veterny_senzor_rychlost_vetra": "5",
    "sensor.netatmo_veterny_senzor_sila_narazov": "9",
}

CALM_ATTRS = {("weather.forecast_home", "wind_speed"): 4}


@pytest.fixture(scope="module")
def config(fixtures_dir):
    return load_config_file(fixtures_dir / "dom_peter.yaml")


def world(attrs=None, **states) -> World:
    """A snapshot of the whole house, quiet unless a named entity says otherwise."""
    return World(
        states=CALM | states,
        attributes=CALM_ATTRS | (attrs or {}),
        now=NOW,
    )


def closing(config, position=0, tilt=0) -> Decision:
    """A decision that lowers every blind: what a directional guard is about."""
    entities = list(config.blinds)
    return Decision(
        mode="bezny_den",
        targets={e: Action(position=position, tilt=tilt) for e in entities},
        trace=dict.fromkeys(entities, "bezny_den.test#0"),
    )


def opening(config) -> Decision:
    return closing(config, position=100, tilt=100)


def tilt_only(config) -> Decision:
    """Position `KEEP`: not a movement of the position axis at all."""
    return closing(config, position=KEEP, tilt=0)


def at(config, value=100):
    """Where every blind currently is."""
    return dict.fromkeys(config.blinds, value)


def run(config, w, decision, positions):
    return review(config, w, decision, positions, screen(config, w))


def fired_on(result, index) -> set[str]:
    """Which blinds a particular guard claimed. The counter for every
    implication below: an empty set means the assertion after it proved
    nothing.
    """
    return {e for e, o in result.outcomes.items() if o.guard == index}


# --------------------------------------------------------------------------
# The block itself.
# --------------------------------------------------------------------------


def test_the_fixture_still_validates_clean_with_guards(config):
    """`validate()` must stay empty of *any* severity, guards included -- an
    `unreachable` guard is a warning, and a warning here means an interlock
    that looks present right up until the day it was needed.
    """
    assert validate(config) == []


def test_the_guard_list_is_the_shape_the_report_describes(config):
    assert len(config.guards) == 6
    assert [g.policy for g in config.guards] == [
        GUARD_FORCE,
        GUARD_SKIP,
        GUARD_DEFER,
        GUARD_FORCE,
        GUARD_DEFER,
        GUARD_FORCE,
    ]
    # Wind first, on purpose: the one interlock that has to work when
    # everything else is broken, and the only reason the others need not
    # each repeat "and it is not blowing a gale".
    assert config.guards[WIND].name.startswith("vietor")
    assert config.guards[WIND].targets == ()


def test_no_guard_runs_at_the_input_stage(config):
    """The house's two input-stage filters (bedroom routing, the flower
    keeper's `zony_kvety`) are both inexpressible here -- one is a property of
    the call path, not of the world. Asserting the absence rather than leaving
    it implied is what stops someone adding one without reading why the
    flowers guard is deliberately `output` (see the fixture's comment on it).
    """
    assert [g for g in config.guards if g.stage == GUARD_STAGE_INPUT] == []
    assert {g.stage for g in config.guards} == {GUARD_STAGE_OUTPUT}

    screening = screen(config, world())
    assert screening.outcomes == {}
    assert screening.remaining == frozenset(config.blinds)


def test_a_calm_house_is_a_house_no_guard_touches(config):
    w = world()
    decision = evaluate(config, w)
    result = run(config, w, decision, at(config))

    assert len(result.outcomes) == 10  # counter: every blind really was judged
    assert {o.reason for o in result.outcomes.values()} == {NO_GUARD}
    assert result.actions == decision.targets
    assert result.deferrals == {}


def test_guards_do_not_move_a_single_decision(config, fixtures_dir):
    """The migration gate's own claim, asserted here rather than assumed:
    `evaluate()` never reads `config.guards`, so the same house with the
    guard list emptied decides identically. If this ever fails, the gate is
    measuring something the house no longer does.
    """
    text = (fixtures_dir / "dom_peter.yaml").read_text(encoding="utf-8")
    head, sep, _ = text.partition("# ---- POISTKY")
    assert sep  # counter: the block really was found and removed
    bare = load_config(head)
    assert bare.guards == ()
    assert config.guards  # counter: the compared config really has guards

    for overrides in ({}, {"input_boolean.cover_down": "on"}, {"binary_sensor.is_home": "off"}):
        w = world(**overrides)
        assert evaluate(bare, w).targets == evaluate(config, w).targets


# --------------------------------------------------------------------------
# #9 -- wind. First in the list, and the whole house.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "attrs"),
    [
        ({"sensor.netatmo_veterny_senzor_rychlost_vetra": "41"}, {}),
        ({"sensor.netatmo_veterny_senzor_sila_narazov": "56"}, {}),
        ({}, {("weather.forecast_home", "wind_speed"): 36}),
    ],
)
def test_any_one_wind_source_alone_opens_the_whole_house(config, states, attrs):
    w = world(attrs=attrs, **states)
    result = run(config, w, closing(config), at(config, 0))

    assert fired_on(result, WIND) == set(config.blinds)  # counter and claim in one
    assert len(result.actions) == 10
    for entity, action in result.actions.items():
        assert action == Action(position=100, tilt=KEEP), entity


def test_wind_does_not_fire_below_every_threshold(config):
    """The thresholds are the ones the live automation *triggers* on
    (40/55/35), not the lower ones it calms down at (40/55/30) which
    `vietor_ok` holds for the matrix. `above` is strict, so the threshold
    value itself is calm.
    """
    w = world(
        attrs={("weather.forecast_home", "wind_speed"): 35},
        **{
            "sensor.netatmo_veterny_senzor_rychlost_vetra": "40",
            "sensor.netatmo_veterny_senzor_sila_narazov": "55",
        },
    )
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, WIND) == set()
    assert len(result.outcomes) == 10  # counter: it was asked about all ten


def test_a_dead_wind_sensor_is_no_information_not_a_gale(config):
    """The earlier version of this test asserted the opposite, and the live
    house paid for it on 2026-08-31: both Netatmo sensors are flat, `default:
    999` read as "always a gale", and the wind guard forced all ten blinds
    open on every evaluation. See docs/rationale.md, 2026-08-31.

    The instinct behind 999 was sound -- an interlock must not be switched off
    by an unplugged sensor. The consequence was not weighed: an interlock
    permanently *on* is not a safe direction either, it is the house held open
    in a heatwave with nothing able to correct it.
    """
    states = dict(CALM)
    del states["sensor.netatmo_veterny_senzor_rychlost_vetra"]
    del states["sensor.netatmo_veterny_senzor_sila_narazov"]
    w = World(states=states, attributes=CALM_ATTRS, now=NOW)
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, WIND) == set()
    assert len(result.outcomes) == 10  # counter: it was asked about all ten


def test_the_live_forecast_still_carries_the_wind_guard_alone(config):
    """The other half, and the half that makes the test above safe to keep:
    with both anemometers dead, a forecast above the trigger threshold must
    still fire the guard on every blind. Otherwise "no information" would have
    quietly disarmed the one interlock that must work when all else is broken.
    """
    states = dict(CALM)
    del states["sensor.netatmo_veterny_senzor_rychlost_vetra"]
    del states["sensor.netatmo_veterny_senzor_sila_narazov"]
    attrs = dict(CALM_ATTRS)
    attrs[("weather.forecast_home", "wind_speed")] = 42
    w = World(states=states, attributes=attrs, now=NOW)
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, WIND) == set(config.blinds)


def test_wind_outranks_a_hand_on_the_flowers(config):
    """Order is the referee, and the only reason it is: both guards cover the
    kitchen blinds and both would fire. Written first, wind wins -- which is
    also why the flowers guard must stay at the *output* stage (an input-stage
    claim is never re-judged, so it would outrank wind wherever it were
    written).
    """
    # The raise has to be *real* here, or this test passes because the flowers
    # guard never fired -- which is what happened when its condition moved from
    # the helper to the blind's own position and this world still set the
    # helper. Both guards must actually claim the kitchen for order to be the
    # thing under test.
    w = world(
        attrs={("cover.kuchyna_zaluzia_1_4", "current_position"): 96},
        **{"sensor.netatmo_veterny_senzor_rychlost_vetra": "60"},
    )
    result = run(config, w, closing(config), at(config, 96))

    assert set(FLOWERS) <= fired_on(result, WIND)
    assert fired_on(result, FLOWERS_BY_HAND) == set()  # shadowed, on purpose
    for entity in FLOWERS:
        assert result.actions[entity] == Action(position=100, tilt=KEEP)


# --------------------------------------------------------------------------
# #11 -- a hand raised the flowers; the keeper must not pull them back.
# --------------------------------------------------------------------------


def test_a_hand_on_the_flowers_suppresses_both_kitchen_blinds(config):
    """The raise is read off the blind now, not off a helper.

    96 is where somebody actually left it on 2026-09-01 at 08:47. Nothing in
    this configuration ever sends the flowers above `kvety_poz` (34), so a
    position over 45 is by construction somebody else's.
    """
    w = world(attrs={("cover.kuchyna_zaluzia_1_4", "current_position"): 96})
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, FLOWERS_BY_HAND) == set(FLOWERS)
    for entity in FLOWERS:
        assert result.outcomes[entity].action is None
        assert result.outcomes[entity].policy == GUARD_SKIP
    # A skip is not a deferral: nothing is waiting to happen later.
    assert result.deferrals == {}


def test_a_hand_on_the_flowers_leaves_the_other_eight_blinds_alone(config):
    w = world(attrs={("cover.kuchyna_zaluzia_1_4", "current_position"): 96})
    result = run(config, w, closing(config), at(config))

    untouched = set(config.blinds) - set(FLOWERS)
    assert len(untouched) == 8  # counter
    for entity in untouched:
        assert result.outcomes[entity].reason == NO_GUARD
        assert result.actions[entity] == Action(position=0, tilt=0)


def test_the_flowers_guard_does_not_fire_with_no_hand_on_them(config):
    """`kvety_rucny_override` is a different flag from the matrix's own
    `zaluzie_kuchyna_rucne`; the fixture reads both, and only this one is a
    guard.
    """
    w = world(**{"input_boolean.zaluzie_kuchyna_rucne": "on"})
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, FLOWERS_BY_HAND) == set()
    assert len(result.outcomes) == 10  # counter


# --------------------------------------------------------------------------
# #1/#5/#6/#7/#8/#14 -- do not drive a door blind down onto an open door or a
# running sauna. Seven independent copies in the house, one row here.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "states",
    [
        {"binary_sensor.obyvacka_dvere_senzor": "on"},
        {"binary_sensor.sauna_running": "on"},
        {"binary_sensor.obyvacka_dvere_senzor": "unavailable"},
    ],
)
def test_the_terrace_close_is_deferred_by_the_door_or_the_sauna(config, states):
    w = world(**states)
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, TERRACE_DEFER) == {LIVING_DOOR}
    deferral = result.deferrals[LIVING_DOOR]
    assert deferral.max_wait == 10800  # the live `timeout: {hours: 3}`
    assert deferral.on_timeout == "abandon"  # the live `continue_on_timeout: false`
    assert deferral.recheck_every == 900  # #3, the restart watchdog, absorbed
    assert result.outcomes[LIVING_DOOR].action is None


def test_an_unavailable_door_sensor_defers_rather_than_closing(config):
    """The one place this fixture deliberately differs from the live config:
    `zavriet_bezpecne` asks `is_state(...,'on')`, so an `unavailable` sensor
    lets the blind down. The guard asks "is it *closed*", so it does not.
    """
    fine = run(config, world(), closing(config), at(config))
    dead = world(**{"binary_sensor.obyvacka_dvere_senzor": "unknown"})
    broken = run(config, dead, closing(config), at(config))

    assert fired_on(fine, TERRACE_DEFER) == set()  # counter: the pair really differs
    assert fired_on(broken, TERRACE_DEFER) == {LIVING_DOOR}


def test_a_shut_door_and_a_cold_sauna_let_the_terrace_close(config):
    w = world()
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, TERRACE_DEFER) == set()
    assert result.actions[LIVING_DOOR] == Action(position=0, tilt=0)  # counter


def test_raising_the_terrace_blind_is_never_deferred(config):
    """`applies_to: closing` is a decreasing position and nothing else --
    every one of the house's door interlocks lets it up without asking.
    """
    w = world(**{"binary_sensor.sauna_running": "on"})
    result = run(config, w, opening(config), at(config, 0))

    assert fired_on(result, TERRACE_DEFER) == set()
    assert result.actions[LIVING_DOOR] == Action(position=100, tilt=100)  # counter


def test_moving_only_the_slats_is_never_deferred(config):
    """A `KEEP` position axis is not a movement of the position axis, however
    far the slats travel. Nine of the house's thirteen interlocks have always
    allowed the tilt through.
    """
    w = world(**{"binary_sensor.sauna_running": "on"})
    result = run(config, w, tilt_only(config), at(config))

    assert fired_on(result, TERRACE_DEFER) == set()
    assert result.actions[LIVING_DOOR] == Action(position=KEEP, tilt=0)  # counter


def test_an_unreadable_cover_position_still_defers(config):
    """The direction cannot be computed, so the guard fires. Opposite polarity
    to `planner.plan`, and deliberately: an interlock silenced by a dead cover
    is worse than one blocking a command it need not have.
    """
    positions = at(config)
    positions[LIVING_DOOR] = None
    w = world(**{"binary_sensor.sauna_running": "on"})
    result = run(config, w, closing(config), positions)

    assert fired_on(result, TERRACE_DEFER) == {LIVING_DOOR}


# --------------------------------------------------------------------------
# #2/#4 -- while the door is open, the door blind is up.
# --------------------------------------------------------------------------


def test_an_open_terrace_door_holds_the_living_room_blind_up(config):
    w = world(**{"binary_sensor.obyvacka_dvere_senzor": "on"})
    result = run(config, w, opening(config), at(config, 0))

    assert fired_on(result, TERRACE_FORCE) == {LIVING_DOOR}
    assert result.actions[LIVING_DOOR] == Action(position=100, tilt=KEEP)
    assert result.outcomes[LIVING_DOOR].policy == GUARD_FORCE


def test_an_open_door_at_night_still_holds_the_blind_up(config):
    """Mode `noc` decides `keep/keep` for everything; the guard overrides it.
    This is the live `Coverdown` branch of `automation.obyvacka_dvere_auto`,
    which force-opens exactly here.
    """
    w = world(**{"input_boolean.cover_down": "on", "binary_sensor.obyvacka_dvere_senzor": "on"})
    decision = evaluate(config, w)

    assert decision.mode == "noc"  # counter: the engine really said keep/keep
    assert decision.targets[LIVING_DOOR] == Action()
    result = run(config, w, decision, at(config, 0))
    assert result.actions[LIVING_DOOR] == Action(position=100, tilt=KEEP)


def test_a_running_sauna_stops_the_force_but_not_the_deferral(config):
    """The live automation's own condition (`sauna_running == off`, replacing
    the 40 degree threshold on 2026-08-29) and the hole its description
    records: during a real session nobody holds this blind up. The deferral
    above is what stops it being driven down instead.
    """
    w = world(**{"binary_sensor.obyvacka_dvere_senzor": "on", "binary_sensor.sauna_running": "on"})

    up = run(config, w, opening(config), at(config, 0))
    assert fired_on(up, TERRACE_FORCE) == set()
    assert up.actions[LIVING_DOOR] == Action(position=100, tilt=100)  # counter: it was judged

    down = run(config, w, closing(config), at(config))
    assert fired_on(down, TERRACE_DEFER) == {LIVING_DOOR}


def test_a_shut_door_does_not_force_anything_up(config):
    w = world()
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, TERRACE_FORCE) == set()
    assert fired_on(result, BEDROOM_FORCE) == set()
    assert len(result.outcomes) == 10  # counter


# --------------------------------------------------------------------------
# The bedroom: the same rule, its own sensor, and no sauna term.
# --------------------------------------------------------------------------


def test_the_bedroom_door_guards_key_on_the_bedroom_sensor_alone(config):
    w = world(**{"binary_sensor.spalna_dvere_senzor_2": "on"})

    down = run(config, w, closing(config), at(config))
    assert fired_on(down, BEDROOM_DEFER) == {BEDROOM_DOOR}
    assert fired_on(down, TERRACE_DEFER) == set()  # the living room is unaffected

    up = run(config, w, opening(config), at(config, 0))
    assert fired_on(up, BEDROOM_FORCE) == {BEDROOM_DOOR}
    assert up.actions[BEDROOM_DOOR] == Action(position=100, tilt=KEEP)


def test_a_running_sauna_does_not_touch_the_bedroom(config):
    """`zavriet_bezpecne` ties the sauna to the living-room blind only -- the
    bedroom terrace has no sauna next to it. `teplotna_ochrana_zatvor` (the
    orphaned fourth copy) never learnt the bedroom half at all; one list is
    what ends that.
    """
    w = world(**{"binary_sensor.sauna_running": "on"})
    result = run(config, w, closing(config), at(config))

    assert fired_on(result, BEDROOM_DEFER) == set()
    assert fired_on(result, TERRACE_DEFER) == {LIVING_DOOR}  # counter


def test_the_bedroom_window_blind_shares_a_zone_but_not_the_guard(config):
    """Zone `spalna` holds both bedroom blinds, so the guards name the entity
    rather than the zone. `cover.spalna_zaluzia_2` is over a window and must
    keep closing with the door wide open.
    """
    assert BEDROOM_WINDOW in config.zones["spalna"].members  # counter
    w = world(**{"binary_sensor.spalna_dvere_senzor_2": "on"})
    result = run(config, w, closing(config), at(config))

    assert result.outcomes[BEDROOM_WINDOW].reason == NO_GUARD
    assert result.actions[BEDROOM_WINDOW] == Action(position=0, tilt=0)


def test_the_bedroom_wait_is_indefinite_and_says_so(config):
    """`max_wait: null` is a value, not an omission -- the live automation
    waits without a limit on purpose ("kym su dvere otvorene, zaluzia zostava
    hore", 2026-08-29). `UNSET` would be the author never having said.
    """
    guard = config.guards[BEDROOM_DEFER]
    assert guard.max_wait is None
    assert guard.max_wait is not UNSET
    assert guard.on_timeout == "abandon"

    w = world(**{"binary_sensor.spalna_dvere_senzor_2": "on"})
    result = run(config, w, closing(config), at(config))
    assert result.deferrals[BEDROOM_DOOR].max_wait is None


# --------------------------------------------------------------------------
# The one that must never be "tidied up".
# --------------------------------------------------------------------------


def test_the_window_blind_named_dvere_is_never_guarded_on_a_door(config):
    """`cover.obyvacka_zaluzia_dvere_2` hangs over a window, not over an
    opening leaf (verified with the owner, 2026-08-30). Its name says `dvere`
    and it appears in Lighting SUN and in wind protection, and neither of
    those is evidence that anything guards it on a door sensor -- the house
    briefly had it in `zaluzie_zavriet`'s exclusion list on 30. 8. for exactly
    that bad reason, which only meant it could not be closed all summer.

    Static half: no door guard names it. Behavioural half: with both door
    sensors open and the sauna running, it still closes.
    """
    for index in (TERRACE_DEFER, TERRACE_FORCE, BEDROOM_DEFER, BEDROOM_FORCE):
        assert LIVING_WINDOW not in guard_blinds(config, config.guards[index])

    w = world(
        **{
            "binary_sensor.obyvacka_dvere_senzor": "on",
            "binary_sensor.spalna_dvere_senzor_2": "on",
            "binary_sensor.sauna_running": "on",
        }
    )
    result = run(config, w, closing(config), at(config))

    assert result.outcomes[LIVING_DOOR].action is None  # counter: the real door blind held
    assert result.outcomes[LIVING_WINDOW].reason == NO_GUARD
    assert result.actions[LIVING_WINDOW] == Action(position=0, tilt=0)


def test_every_guard_in_the_fixture_can_actually_fire(config):
    """The list's own reachability, measured rather than reasoned about: each
    guard claims at least one blind in at least one of the worlds above.
    `validation._check_guard_reachability` only sees an *unconditional*
    earlier guard, so a conditional shadow (the flowers under wind, say) is
    invisible to it and visible here.
    """
    worlds = [
        (world(**{"sensor.netatmo_veterny_senzor_sila_narazov": "99"}), closing(config)),
        (world(attrs={("cover.kuchyna_zaluzia_1_4", "current_position"): 96}), closing(config)),
        (world(**{"binary_sensor.sauna_running": "on"}), closing(config)),
        (world(**{"binary_sensor.obyvacka_dvere_senzor": "on"}), opening(config)),
        (world(**{"binary_sensor.spalna_dvere_senzor_2": "on"}), closing(config)),
        (world(**{"binary_sensor.spalna_dvere_senzor_2": "on"}), opening(config)),
    ]
    seen: set[int] = set()
    for w, decision in worlds:
        result = run(config, w, decision, at(config, 50))
        seen |= {o.guard for o in result.outcomes.values() if o.guard is not None}

    assert seen == set(range(len(config.guards)))


def test_the_fixture_uses_every_policy_and_both_directions(config):
    assert {g.policy for g in config.guards} == {GUARD_SKIP, GUARD_DEFER, GUARD_FORCE}
    assert {g.applies_to for g in config.guards} == {GUARD_ANY, GUARD_CLOSING}
