"""`readiness.assess`: which inputs could not be read, and whom that blocks.

Every test here has a counter, because every assertion in this module is
implication-shaped ("a missing entity blocks a blind") and an implication is
satisfied for free by an implementation that blocks everything, always. So each
"is blocked" is paired with an "and this one is not", and each "nothing is
missing" is paired with the same world minus one entity.

Since 2026-08-31 the same is true in the other direction: every "this is not a
fault" is paired with a read the configuration does *not* answer, in the same
world and usually on the same entity, because an implementation that simply
stopped vetoing would satisfy the exemptions on its own.

The gate itself -- nothing reaches the runner -- is `tests/ha/
test_readiness_gate.py`. This module is only about the verdict.
"""

import dataclasses

import pytest

from cover_logic.config_schema import load_config, load_config_file, referenced_entities
from cover_logic.const import READINESS_MAX_NAMED, READINESS_REASON_PREFIX
from cover_logic.readiness import Readiness, assess
from cover_logic.validation import ERROR, validate
from cover_logic.world import World

# Two zones reading two different entities, over one shared mode input. That
# shape is the whole point: `sun.sun` and `input_boolean.rezim` are house-wide
# facts, `binary_sensor.a`/`binary_sensor.b` are one zone's each.
TWO_ZONES = """
blinds:
  - entity: cover.a
  - entity: cover.b
zones:
  za:
    members: [cover.a]
  zb:
    members: [cover.b]
conditions:
  rezim_on:
    condition: state
    entity_id: input_boolean.rezim
    state: "on"
  a_on:
    condition: state
    entity_id: binary_sensor.a
    state: "on"
  b_on:
    condition: state
    entity_id: binary_sensor.b
    state: "on"
modes:
  - id: noc
    when: !ref rezim_on
  - id: den
rules:
  den.za:
    - if: !ref a_on
      then: {position: 0}
    - then: {position: keep, tilt: keep}
  den.zb:
    - if: !ref b_on
      then: {position: 0}
    - then: {position: keep, tilt: keep}
  noc.*:
    - then: {position: keep, tilt: keep}
"""

HEALTHY = {
    "input_boolean.rezim": "off",
    "binary_sensor.a": "off",
    "binary_sensor.b": "off",
}


def config(text=TWO_ZONES):
    """Parse `text`, refusing anything this project would not accept."""
    parsed = load_config(text)
    assert [p for p in validate(parsed) if p.severity == ERROR] == []
    return parsed


def world(states=None, attributes=None):
    """A `World` holding exactly `states`/`attributes` and nothing else."""
    return World(states=dict(states or {}), attributes=dict(attributes or {}))


def healthy_world(cfg, *, value="off", omit=(), unavailable=()):
    """A world in which every entity `cfg` reads is readable, minus `omit`.

    `omit` leaves an entity out of the snapshot entirely (what
    `ha_world.build_world` does with an entity Home Assistant has never had);
    `unavailable` gives it Home Assistant's own `unavailable` string. The two
    are different faults with the same verdict, and both are tested.
    """
    states = {}
    attributes = {}
    for read in referenced_entities(cfg):
        entity = read[0] if isinstance(read, tuple) else read
        if entity in omit:
            continue
        if isinstance(read, tuple):
            attributes[read] = 0
            continue
        states[entity] = "unavailable" if entity in unavailable else value
    return world(states, attributes)


# ---------------------------------------------------------------------------
# The three shapes of "cannot be read", and their counter.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_unknown_and_unavailable_are_both_unready(state: str) -> None:
    """Home Assistant's own two spellings of "there is nothing to tell you"."""
    cfg = config()
    verdict = assess(cfg, world({**HEALTHY, "input_boolean.rezim": state}))
    assert "input_boolean.rezim" in verdict.missing
    assert set(verdict.blocked) == {"cover.a", "cover.b"}


def test_an_entity_absent_from_the_snapshot_is_unready() -> None:
    """`build_world` omits an entity it has never seen -- that is the same fault."""
    cfg = config()
    states = {key: value for key, value in HEALTHY.items() if key != "input_boolean.rezim"}
    assert assess(cfg, world(states)).missing == ("input_boolean.rezim",)


def test_a_readable_state_is_ready() -> None:
    """The counter to all three above: the identical config, nothing missing.

    Without this, an `assess` that reported everything as unready would pass
    every "is blocked" assertion in this file.
    """
    verdict = assess(config(), world(HEALTHY))
    assert verdict.missing == ()
    assert verdict.blocked == {}
    assert verdict.ready is True


def test_an_empty_state_string_is_not_unready() -> None:
    """Only `None`/`unknown`/`unavailable` count -- not every state a reader dislikes.

    A guard against widening `UNREADY_STATES` by reflex: `""`, `"off"` and
    `"0"` are all things a real entity reports and all of them are answers.
    """
    for value in ("", "off", "0", "unavailable_x"):
        verdict = assess(config(), world({**HEALTHY, "binary_sensor.a": value}))
        assert verdict.blocked == {}, value


def test_a_missing_attribute_is_unready_under_its_entity_s_own_name() -> None:
    """An attribute read of an unreadable *entity* blocks, reported as the entity id.

    The fault an attribute read can carry is its entity's: `('cover.a',
    'current_position')` is reported as `cover.a`, because that is the name a
    person goes and looks at. The counter is the entity being readable while the
    attribute is absent -- see
    `test_an_absent_attribute_on_a_readable_entity_is_not_a_fault`.
    """
    text = """
blinds:
  - entity: cover.a
zones:
  za:
    members: [cover.a]
conditions:
  poloha:
    condition: state
    entity_id: cover.a
    attribute: current_position
    state: 100
modes:
  - id: den
rules:
  den.za:
    - if: !ref poloha
      then: {position: 0}
    - then: {position: keep, tilt: keep}
"""
    cfg = config(text)
    assert assess(cfg, world()).missing == ("cover.a",)
    assert assess(cfg, world({"cover.a": "unavailable"})).missing == ("cover.a",)
    # Counter: a readable entity, and the attribute read stops being a fault --
    # so the two lines above are about `cover.a` and not about every read.
    present = world({"cover.a": "open"}, {("cover.a", "current_position"): 100})
    assert assess(cfg, present).blocked == {}


# ---------------------------------------------------------------------------
# What the configuration already answers -- the second measured incident.
# ---------------------------------------------------------------------------

# One dead sensor read twice: once by a condition that states a `default:` and
# once by one that does not. The shape is the live house's (`vietor_silny` reads
# two flat-battery anemometers with `default: 0`), and it is deliberately the
# same entity in both, so an implementation that judges per *entity* instead of
# per *read* cannot pass both halves of any test below.
DEFAULTED = """
blinds:
  - entity: cover.a
  - entity: cover.b
zones:
  za:
    members: [cover.a]
  zb:
    members: [cover.b]
conditions:
  vietor:
    condition: numeric_state
    entity_id: sensor.wind
    above: 40
    default: 0
  vietor_stav:
    condition: state
    entity_id: sensor.wind
    state: "on"
modes:
  - id: den
rules:
  den.za:
    - if: !ref vietor
      then: {position: 100}
    - then: {position: keep, tilt: keep}
  den.zb:
    - if: !ref vietor_stav
      then: {position: 100}
    - then: {position: keep, tilt: keep}
"""


# The same house minus the undefaulted reader: every read of `sensor.wind`
# states a default, which is the live `vietor_ok`/`vietor_silny` situation.
ONLY_DEFAULTED = """
blinds:
  - entity: cover.a
zones:
  za:
    members: [cover.a]
conditions:
  vietor:
    condition: numeric_state
    entity_id: sensor.wind
    above: 40
    default: 0
modes:
  - id: den
rules:
  den.za:
    - if: !ref vietor
      then: {position: 100}
    - then: {position: keep, tilt: keep}
"""


def test_a_defaulted_read_of_a_dead_sensor_is_not_a_fault() -> None:
    """`default:` is the author answering; re-asking vetoed this house forever.

    Both anemometers have been flat for months, both are read only with an
    explicit `default:`, and on 2026-08-31 the gate blocked all ten blinds on
    them permanently. The counter is in the same world and the same entity:
    `zb` reads `sensor.wind` with no default and is still blocked, which is what
    a "skip every read" implementation fails.
    """
    cfg = config(DEFAULTED)
    verdict = assess(cfg, world({"sensor.wind": "unavailable"}))
    assert verdict.blocked_by("cover.a") == ()
    assert verdict.blocked_by("cover.b") == ("sensor.wind",)


def test_a_defaulted_read_does_not_report_the_entity_as_missing() -> None:
    """The whole-config view must agree with the per-blind one, or the sensor lies.

    `missing` drives `ready`, which is what the coordinator logs and the
    diagnostic shows; a defaulted-only entity in there is a house reported
    unready with nothing to fix. Counter: the undefaulted read of the same
    entity is what puts it back.
    """
    cfg = config(ONLY_DEFAULTED)
    assert "sensor.wind" in {
        read[0] if isinstance(read, tuple) else read for read in referenced_entities(cfg)
    }  # counter: the entity really is read, so `ready` is not true by omission
    assert assess(cfg, world({"sensor.wind": "unavailable"})).ready is True
    # And with the undefaulted reader back, the same world is not ready.
    assert assess(config(DEFAULTED), world({"sensor.wind": "unavailable"})).ready is False


def test_an_absent_attribute_on_a_readable_entity_is_not_a_fault() -> None:
    """Alarmo drops `arm_mode` while disarmed -- the absence *is* the answer.

    Home Assistant has no "attribute unavailable" marker, so an integration
    omitting an attribute means something by it. The counter is the same read
    with the entity itself `unavailable`: then there genuinely is nothing to
    read, and it must still block.
    """
    text = """
blinds:
  - entity: cover.a
zones:
  za:
    members: [cover.a]
conditions:
  vacation:
    condition: state
    entity_id: alarm_control_panel.alarmo
    attribute: arm_mode
    state: armed_vacation
modes:
  - id: dovolenka
    when: !ref vacation
  - id: den
rules:
  den.za:
    - then: {position: keep, tilt: keep}
  dovolenka.za:
    - then: {position: 0}
"""
    cfg = config(text)
    readable = world({"alarm_control_panel.alarmo": "disarmed"})
    assert assess(cfg, readable).missing == ()
    assert assess(cfg, readable).blocked == {}
    dead = world({"alarm_control_panel.alarmo": "unavailable"})
    assert assess(cfg, dead).blocked_by("cover.a") == ("alarm_control_panel.alarmo",)


# ---------------------------------------------------------------------------
# Global vs. per blind.
# ---------------------------------------------------------------------------


def test_a_dead_mode_input_blocks_every_blind() -> None:
    """Mode is a house-wide fact: unreadable, and no blind's rule list is trustworthy.

    This is the measured incident in miniature -- resolution fell through to
    the catch-all and every zone got its daytime rule.
    """
    cfg = config()
    verdict = assess(cfg, world({**HEALTHY, "input_boolean.rezim": "unavailable"}))
    assert set(verdict.blocked) == {"cover.a", "cover.b"}
    assert verdict.blocked_by("cover.a") == ("input_boolean.rezim",)


def test_a_dead_zone_input_blocks_only_that_zone_s_blinds() -> None:
    """The per-blind half: `binary_sensor.a` is read by `za`'s rules and nothing else.

    The second assertion is the counter, and it is the one that fails against a
    global gate: `cover.b` must still be dispatchable.
    """
    cfg = config()
    verdict = assess(cfg, world({**HEALTHY, "binary_sensor.a": "unavailable"}))
    assert verdict.blocked_by("cover.a") == ("binary_sensor.a",)
    assert verdict.blocked_by("cover.b") == ()
    # And the whole-house view still reports it: a blocked-nobody fault would
    # otherwise be indistinguishable from no fault.
    assert verdict.missing == ("binary_sensor.a",)
    assert verdict.ready is False


def test_a_default_zone_rule_s_input_blocks_every_zone_it_can_decide() -> None:
    """A rule under the `"*"` key decides every zone, so its input is everyone's.

    `engine._apply_rules` falls through to that list; a readiness rule that
    only looked at `f"{mode}.{zone}"` would let a blind be commanded from a
    default rule whose own condition was unreadable.
    """
    text = TWO_ZONES.replace(
        "  noc.*:\n    - then: {position: keep, tilt: keep}\n",
        "  den.*:\n    - if: !ref rezim_on\n      then: {position: 50}\n"
        "  noc.*:\n    - then: {position: keep, tilt: keep}\n",
    )
    cfg = config(text)
    assert "den.*" in cfg.rules  # counter: the replacement really landed
    verdict = assess(cfg, world({**HEALTHY, "binary_sensor.b": "unavailable"}))
    assert verdict.blocked_by("cover.a") == ()
    assert verdict.blocked_by("cover.b") == ("binary_sensor.b",)


def test_a_blind_that_reads_nothing_is_never_blocked() -> None:
    """An unconditional rule under a single fallback mode depends on no entity.

    Deliberate, and stated as a test so nobody "fixes" it: a decision that
    reads nothing cannot have been corrupted by a missing input, and blocking
    it would be a veto with no cause.
    """
    text = """
blinds:
  - entity: cover.a
zones:
  za:
    members: [cover.a]
modes:
  - id: den
rules:
  den.za:
    - then: {position: 0}
"""
    verdict = assess(config(text), world())
    assert verdict.blocked == {}
    assert verdict.ready is True


# ---------------------------------------------------------------------------
# Guards, refs and value helpers -- the three reads that are easy to forget.
# ---------------------------------------------------------------------------


def test_a_guard_s_own_entity_blocks_the_blinds_it_targets() -> None:
    """An unreadable interlock sensor does not raise -- it stands the guard down.

    `conditions._state` against a missing entity evaluates `False`, so the
    sauna/door guard is simply off. That is the failure mode this covers, and
    the second assertion is the counter: a guard aimed at `za` must not block
    `zb`.
    """
    guarded = (
        TWO_ZONES
        + """
guards:
  - name: door open
    policy: skip
    applies_to: closing
    targets: [za]
    when:
      condition: state
      entity_id: binary_sensor.dvere
      state: "on"
"""
    )
    cfg = config(guarded)
    verdict = assess(cfg, world(HEALTHY))
    assert verdict.blocked_by("cover.a") == ("binary_sensor.dvere",)
    assert verdict.blocked_by("cover.b") == ()


def test_a_guard_with_no_targets_blocks_every_blind() -> None:
    """No `targets` means the whole house, and `guard_blinds` is what says so.

    The counter to the test above: read `guard.targets` directly instead of
    through `guards.guard_blinds` and this is the case that goes silently wrong.
    """
    guarded = (
        TWO_ZONES
        + """
guards:
  - name: wind
    policy: skip
    applies_to: any
    when:
      condition: state
      entity_id: binary_sensor.vietor
      state: "on"
"""
    )
    verdict = assess(config(guarded), world(HEALTHY))
    assert set(verdict.blocked) == {"cover.a", "cover.b"}


def test_a_ref_is_followed_into_the_named_condition_it_points_at() -> None:
    """`!ref` is how this house writes every condition; unfollowed, nothing is attributed.

    Counter in the same test: a named condition *nothing* references is
    reported in `missing` and blocks nobody -- which is what distinguishes
    following refs from simply unioning `referenced_entities` per blind.
    """
    text = TWO_ZONES.replace(
        "modes:",
        "  orphan_on:\n    condition: state\n"
        '    entity_id: input_boolean.orphan\n    state: "on"\nmodes:',
    )
    cfg = config(text)
    assert "orphan_on" in cfg.conditions  # counter: the replacement landed

    verdict = assess(cfg, world({**HEALTHY, "binary_sensor.a": "unavailable"}))
    assert verdict.blocked_by("cover.a") == ("binary_sensor.a",)
    # `input_boolean.orphan` is unreadable too, and blocks nobody.
    assert "input_boolean.orphan" in verdict.missing
    assert "input_boolean.orphan" not in verdict.blocked_by("cover.a")
    assert verdict.blocked_by("cover.b") == ()


def test_a_circular_condition_reference_terminates() -> None:
    """`_seen` breaks the cycle rather than trusting the config to be acyclic.

    `conditions.evaluate_condition` raises on a cycle at evaluation time; this
    walk runs *before* anything has been evaluated, so it must survive one.
    """
    cfg = config()
    looping = dict(cfg.conditions)
    looping["a_on"] = {"condition": "and", "conditions": [{"condition": "ref", "name": "a_on"}]}
    verdict = assess(dataclasses.replace(cfg, conditions=looping), world(HEALTHY))
    assert verdict.blocked == {}


def test_a_value_helper_blocks_the_blind_whose_action_reads_it() -> None:
    """A `!ref` position falls back to its `default` -- which is the house moving.

    `engine._resolve_value`'s fallback is designed and correct; on a
    half-loaded world it means "send 34 % to everything", which is exactly the
    class of movement this gate exists to stop.
    """
    text = """
blinds:
  - entity: cover.a
  - entity: cover.b
zones:
  za:
    members: [cover.a]
  zb:
    members: [cover.b]
values:
  kvety:
    entity: input_number.kvety
    default: 34
modes:
  - id: den
rules:
  den.za:
    - then: {position: !ref kvety}
  den.zb:
    - then: {position: 0}
"""
    cfg = config(text)
    verdict = assess(cfg, world())
    assert verdict.blocked_by("cover.a") == ("input_number.kvety",)
    # Counter: `zb`'s literal action reads nothing, so it is not blocked.
    assert verdict.blocked_by("cover.b") == ()


# ---------------------------------------------------------------------------
# Truncation, and the reason line.
# ---------------------------------------------------------------------------


def test_the_reason_names_the_entities_and_truncates_a_long_list() -> None:
    """Ten names and a count of the rest, not a hundred names."""
    names = tuple(f"binary_sensor.s{index:02d}" for index in range(25))
    verdict = Readiness(missing=names, blocked={"cover.a": names})

    reason = verdict.reason("cover.a")
    assert reason.startswith(f"{READINESS_REASON_PREFIX}: binary_sensor.s00,")
    assert reason.endswith(f"(+{25 - READINESS_MAX_NAMED} more)")
    assert reason.count("binary_sensor.") == READINESS_MAX_NAMED

    attributes = verdict.as_attributes()
    assert len(attributes["missing"]) == READINESS_MAX_NAMED
    assert attributes["missing_count"] == 25
    assert len(attributes["blocked"]["cover.a"]) == READINESS_MAX_NAMED


def test_a_short_list_is_not_truncated() -> None:
    """The counter: no `(+N more)` when there is no more, and every name shown."""
    cfg = config()
    verdict = assess(cfg, world({**HEALTHY, "input_boolean.rezim": "unavailable"}))
    assert verdict.reason("cover.a") == f"{READINESS_REASON_PREFIX}: input_boolean.rezim"
    assert "more)" not in verdict.reason("cover.a")


def test_a_dispatchable_blind_s_reason_says_nothing_rather_than_lying() -> None:
    """`reason` on an unblocked blind must not read like a fault."""
    verdict = assess(config(), world(HEALTHY))
    assert verdict.reason("cover.a").endswith("nothing")


# ---------------------------------------------------------------------------
# The live house.
# ---------------------------------------------------------------------------

# Every entity the real fixture reads only through a condition that states a
# `default:`: two anemometers whose battery has been flat for months, and the
# forecast they hand over to (`vietor_ok`, `vietor_silny`).
DEFAULTED_IN_THE_HOUSE = {
    "sensor.netatmo_veterny_senzor_rychlost_vetra",
    "sensor.netatmo_veterny_senzor_sila_narazov",
    "weather.forecast_home",
}


def test_the_real_house_blocks_every_blind_on_an_empty_world(fixtures_dir) -> None:
    """The bedroom case, stated against the configuration actually running.

    A Home Assistant that has not finished restoring state is this world. Every
    one of the ten blinds must be blocked -- not nine, and not "the mode input
    is missing" alone.
    """
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    verdict = assess(cfg, world())
    assert set(verdict.blocked) == set(cfg.blinds)
    assert len(cfg.blinds) == 10
    assert verdict.ready is False
    # The regression that caused the second incident, stated on the real
    # configuration: exempting a defaulted read must not exempt the mode
    # conditions, which state no default and are what block all ten here. The
    # counter is the line below -- the three defaulted entities are exempt on
    # this same world, so the ten above are not blocked by "everything blocks".
    assert set(verdict.missing).isdisjoint(DEFAULTED_IN_THE_HOUSE)
    assert "input_boolean.cover_down" in verdict.missing


def test_the_real_house_blocks_nobody_when_every_input_is_readable(fixtures_dir) -> None:
    """The counter, and the one that would catch a gate that never lifts.

    A veto that cannot be lifted is a house that never moves again, which is a
    worse defect than the one being fixed.
    """
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    verdict = assess(cfg, healthy_world(cfg))
    assert verdict.blocked == {}
    assert verdict.ready is True


def test_the_real_house_is_ready_on_the_world_that_blocked_it_forever(fixtures_dir) -> None:
    """The measured live world of 2026-08-31, entity for entity.

    `ready=False` with `['alarm_control_panel.alarmo', two anemometers]` was the
    live verdict, and it could never lift: the batteries are flat and Alarmo
    publishes `arm_mode` only while armed. Both faults are reproduced here --
    the two sensors `unavailable`, `arm_mode` absent with the panel readable at
    `disarmed` -- and the three asserts before the verdict are the counter: they
    prove this world really carries them, so `ready is True` is not passing on a
    world that was quietly healthy.
    """
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    dead = {
        "sensor.netatmo_veterny_senzor_rychlost_vetra",
        "sensor.netatmo_veterny_senzor_sila_narazov",
    }
    base = healthy_world(cfg, unavailable=dead)
    states = {**base.states, "alarm_control_panel.alarmo": "disarmed"}
    attributes = {key: value for key, value in base.attributes.items() if key[1] != "arm_mode"}
    live = world(states, attributes)

    assert {live.state(entity) for entity in dead} == {"unavailable"}
    assert live.attribute("alarm_control_panel.alarmo", "arm_mode") is None
    assert ("alarm_control_panel.alarmo", "arm_mode") in referenced_entities(cfg)

    verdict = assess(cfg, live)
    assert verdict.missing == ()
    assert verdict.blocked == {}
    assert verdict.ready is True


def test_the_real_house_blocks_everything_on_the_measured_incident_s_world(
    fixtures_dir,
) -> None:
    """One dead mode input on an otherwise healthy house still stops all ten."""
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    verdict = assess(cfg, healthy_world(cfg, unavailable={"input_boolean.cover_down"}))
    assert set(verdict.blocked) == set(cfg.blinds)
    assert verdict.missing == ("input_boolean.cover_down",)


def test_every_blocked_entity_is_also_reported_as_missing(fixtures_dir) -> None:
    """`blocked` must never name something `referenced_entities` does not.

    Not a tautology: the two are computed by different walks -- `blocked`
    follows `!ref` and reads actions' `Ref` axes, `missing` unions
    `referenced_entities`. If this ever fails it means `referenced_entities`
    under-covers, i.e. the coordinator is not subscribed to something a
    decision reads, which is a worse bug than the one this module gates.
    """
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    verdict = assess(cfg, world())
    blocked = {name for names in verdict.blocked.values() for name in names}
    assert blocked, "the empty world must block something, or this proves nothing"
    assert blocked <= set(verdict.missing)
