"""The settle window: never evaluate on a half-applied world.

**This module exists because of one measured morning.** On 2026-08-31, on the
live house, in this order and at these times (local):

    05:34:25   input_boolean.cover_down             on -> off   (fires `svitanie`)
    05:34:26   cover_logic evaluated                -> spalna: tilt 100
    05:34:27   input_boolean.zaluzie_aktivna_spalna on -> off   (`svitanie` resets it)

`svitanie` (`automation.zaluzie_prepocet_a_uplatnenie`, trigger id `svitanie`,
in the house's `/config/automations.yaml`) switches `cover_down` off and *then*
resets the per-room "this room is in use" flags. Those are two separate writes
about one transition, roughly a second apart. The coordinator evaluated between
them -- on a world where the night switch was already off but the bedroom still
claimed to be in use -- and decided to open the parents' bedroom slats while
they were asleep. The old YAML system survives the same event only because its
own trigger carries `for: {seconds: 2}`; the house's `CLAUDE.md` states the rule
outright ("Dve automatizácie na tej istej udalosti = preteky ... daj druhej
krátky `for:` (2 s stačí)").

**And because of a second one, which the window as first written did not
survive.** On 2026-09-01, same event, same bedroom, mode `horucava`:

    05:34:33   input_boolean.cover_down             on -> off   (fires `svitanie`)
    05:34:35   input_boolean.zaluzie_aktivna_spalna on -> off   (`svitanie` resets it)
    05:34:35   cover_logic  SetTilt(100) -> cover.open_cover_tilt, both blinds

The window was 2.0 s and `svitanie`'s own trigger carries `for: {seconds: 2}` --
two reactions to one event with the same delay, which is a coin flip and not an
ordering. The rule that replaced the copied number: the window must be strictly
longer than the longest `for:` on any automation that writes an entity this
integration reads (`const.SETTLE_MUST_OUTLAST_SECONDS`, measured at 5 s on
`kvety`). See `docs/rationale.md` -- "Why the settle window must outlast the
house's own `for:`".

The tests that matter here, and what each would catch:

- `test_svitanie_is_evaluated_on_the_settled_world` reproduces the timeline
  above and asserts the decision is `keep`, not `tilt 100`. It fails against a
  coordinator whose window is shorter than the gap between the two writes --
  which is exactly what the 0.5s debounce this replaced was.
- `test_the_unsettled_world_really_would_have_tilted_it_open` is its counter:
  the same configuration, the same trigger, and the flag deliberately *not*
  reset. If the rule under test had stopped matching for some unrelated reason,
  the test above would go green while proving nothing; this one goes red.
- `test_a_change_inside_the_window_postpones_the_evaluation` is the one a
  fixed-window batcher fails. Both a fixed window and a restarting one absorb
  `svitanie` (the writes are 1s apart, the window is 2s), so the timeline test
  alone does not pin down which was implemented. `svitanie`'s point is to
  evaluate after the *last* write, whenever that lands.
- `test_horucava_morning_is_evaluated_on_the_settled_world` reproduces the
  2026-09-01 timeline at its measured 2 s gap, and fails at any window that
  merely ties `svitanie`'s `for:`. Its counter,
  `test_the_unsettled_horucava_world_really_would_have_tilted_it_open`, is what
  keeps it from passing because rule 5 stopped being reachable.
- `test_the_window_outlasts_every_for_it_must_beat` is the general form: the
  length is not "two seconds", it is "more than the longest `for:` we race".

The two measured-timeline tests, and only those, wait out the *real*
`EVAL_SETTLE_SECONDS`. Everything else here shrinks it, because what those test
is the timer's shape; a test about the length that does not use the length is
not evidence of anything.

Uses `hass_factory` (a real, minimal `HomeAssistant`) for the reason
`test_coordinator.py` gives: `async_track_state_change_event` and
`async_track_point_in_utc_time` both need genuine bus dispatch and genuine
event-loop timers, not a re-implementation of either.
"""

import asyncio
import time

import pytest

pytest.importorskip("homeassistant")

from cover_logic.config_schema import load_config
from cover_logic.const import (
    COMMAND_WOULD_CALL,
    EVAL_SETTLE_MAX_SECONDS,
    EVAL_SETTLE_SECONDS,
    SETTLE_MUST_OUTLAST_SECONDS,
)
from cover_logic.coordinator import (
    SOURCE_GUARD_TIMEOUT,
    CoverLogicCoordinator,
    evaluate as real_evaluate,
)
from cover_logic.model import KEEP
from cover_logic.validation import ERROR, validate

BLIND = "cover.spalna_zaluzia_2"
COVER_DOWN = "input_boolean.cover_down"
ROOM_ACTIVE = "input_boolean.zaluzie_aktivna_spalna"
HEAT = "input_boolean.teplotna_ochrana_dom"

# The gap `svitanie` left between its two writes on 2026-09-01, to the second.
SVITANIE_GAP_SECONDS = 2.0

# The two writes `svitanie` makes, as a configuration: the night switch chooses
# the mode, the room flag chooses the action inside it. Deliberately the same
# shape as the house's own matrix -- a mode resolved from `cover_down`, and a
# first-match rule keyed on the room flag -- because the defect is not about
# these particular values, it is about reading the second one before it has
# been written.
BASE = """
blinds:
  - entity: cover.spalna_zaluzia_2
    travel_time: 0.2
zones:
  spalna:
    members: [cover.spalna_zaluzia_2]
conditions:
  nocny_rezim:
    condition: state
    entity_id: input_boolean.cover_down
    state: "on"
  izba_aktivna:
    condition: state
    entity_id: input_boolean.zaluzie_aktivna_spalna
    state: "on"
modes:
  - id: noc
    when: !ref nocny_rezim
  - id: den
rules:
  noc.spalna:
    - then: {position: keep, tilt: keep}
  den.spalna:
    - name: izba je aktivna
      if: !ref izba_aktivna
      then: {tilt: 100}
    - then: {position: keep, tilt: keep}
"""

# `max_wait` shorter than `recheck_every`, so only the deadline can release
# this: the wait is what proves the recheck timer is not routed through the
# settle window.
DEFER_GUARD = """
guards:
  - name: door open
    policy: defer
    applies_to: any
    targets: [spalna]
    when: !ref izba_aktivna
    max_wait: 1
    on_timeout: proceed
    recheck_every: 5
"""


# The 2026-09-01 morning, as a configuration. Two differences from `BASE`, both
# taken from the live house: the mode `cover_down` hands over to is `horucava`
# (chosen by the heat-protection helper), and the room-flag rule has the
# house's own polarity -- "room *not* active -> keep" first, "the weather says
# open" behind it. That polarity is the defect's shape: a flag read one instant
# too early does not match the protective rule and falls *through* to the
# opening one.
HORUCAVA = """
blinds:
  - entity: cover.spalna_zaluzia_2
    travel_time: 0.2
zones:
  spalna:
    members: [cover.spalna_zaluzia_2]
conditions:
  nocny_rezim:
    condition: state
    entity_id: input_boolean.cover_down
    state: "on"
  horuco:
    condition: state
    entity_id: input_boolean.teplotna_ochrana_dom
    state: "on"
  nie_akt_spalna:
    condition: not
    conditions:
      - condition: state
        entity_id: input_boolean.zaluzie_aktivna_spalna
        state: "on"
modes:
  - id: noc
    when: !ref nocny_rezim
  - id: horucava
    when: !ref horuco
  - id: bezny_den
rules:
  noc.spalna:
    - then: {position: keep, tilt: keep}
  horucava.spalna:
    - name: rule 4 -- izba nie je aktivna
      if: !ref nie_akt_spalna
      then: {position: keep, tilt: keep}
    - name: rule 5 -- pocasie otvorene
      then: {tilt: 100}
  bezny_den.spalna:
    - then: {position: keep, tilt: keep}
"""


def parsed(text):
    """Parse `text`, refusing anything this project would not accept."""
    config_ = load_config(text)
    assert [p for p in validate(config_) if p.severity == ERROR] == []
    return config_


def config(extra=""):
    """Parse `BASE + extra`, refusing anything this project would not accept."""
    return parsed(BASE + extra)


@pytest.fixture(autouse=True)
def _short_motor_settle(monkeypatch):
    """Shrink the *planner's* post-arrival settle, which is a different constant.

    `planner.SETTLE_SECONDS` is a fact about these motors (a tilt command sent
    during travel is discarded); `const.EVAL_SETTLE_SECONDS`, the subject of
    this module, is a fact about how the house writes state. They are two
    unrelated two-second waits and only the second one is under test here.
    """
    monkeypatch.setattr("cover_logic.planner.SETTLE_SECONDS", 0.01)


def _seed(hass, *, cover_down="on", room_active="on", position=0, tilt=0):
    """The world just before `svitanie`: night switch on, room in use, blind shut.

    The slats start at 0, not at 100: a blind already at its target is
    suppressed by the planner's dead band, so seeding it open would make
    "nothing was issued" true for the wrong reason and the counter test below
    would prove nothing.
    """
    hass.states.async_set(COVER_DOWN, cover_down)
    hass.states.async_set(ROOM_ACTIVE, room_active)
    hass.states.async_set(
        BLIND, "closed", {"current_position": position, "current_tilt_position": tilt}
    )


def _counting_evaluate(monkeypatch):
    """Record the `World` of every engine call while still running the real engine.

    The count is the cheap half of the proof: coalescing that does not coalesce
    still produces a correct final answer (the last evaluation of a burst reads
    the settled world either way), so "was the wrong world ever asked about" is
    only visible in how many times the engine ran and what each run saw.
    """
    seen = []

    def _wrapped(config_, world):
        seen.append(world)
        return real_evaluate(config_, world)

    monkeypatch.setattr("cover_logic.coordinator.evaluate", _wrapped)
    return seen


def _would_call(coordinator):
    """The `cover.*` services that would have gone out, oldest first."""
    return [
        entry["would_call"]
        for entry in reversed(coordinator.commands.recent)
        if entry["kind"] == COMMAND_WOULD_CALL and entry["would_call"] != "none"
    ]


# ---------------------------------------------------------------------------
# The measured morning.
# ---------------------------------------------------------------------------


def test_svitanie_is_evaluated_on_the_settled_world(hass_factory, runtime_entry, monkeypatch):
    """`cover_down` off at T, the room flag off at T+1s: one evaluation, and it sees both.

    Fails without the settle window: the 0.5s debounce this replaced fired at
    T+0.5 -- after the mode had changed, before the room flag had -- and
    decided `tilt: 100` for a bedroom whose occupants were asleep.
    """
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass)
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            assert coordinator.decision.mode == "noc"  # counter: the night, before svitanie
            seen.clear()

            hass.states.async_set(COVER_DOWN, "off")
            await asyncio.sleep(1.0)
            hass.states.async_set(ROOM_ACTIVE, "off")
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            await coordinator.runner.async_wait_idle()

            # One evaluation for two writes about one transition -- and the
            # world it read had *both* of them.
            assert len(seen) == 1, [(w.state(COVER_DOWN), w.state(ROOM_ACTIVE)) for w in seen]
            assert seen[0].state(COVER_DOWN) == "off"
            assert seen[0].state(ROOM_ACTIVE) == "off"

            assert coordinator.decision.mode == "den"
            assert coordinator.decision.targets[BLIND].tilt is KEEP
            # And the side effect the owner actually cares about: the slats
            # were never even asked to open.
            assert _would_call(coordinator) == []

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_the_unsettled_world_really_would_have_tilted_it_open(
    hass_factory, runtime_entry, monkeypatch
):
    """The counter to the test above: with the flag left on, `tilt: 100` is what comes out.

    Without this, a rule that quietly stopped matching -- a renamed helper, a
    typo'd zone -- would make the test above green for the wrong reason, which
    is how three defects have shipped past a green suite in this project.
    """
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass)
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()
            seen.clear()

            # Exactly the same trigger, and the room flag deliberately not reset.
            hass.states.async_set(COVER_DOWN, "off")
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            await coordinator.runner.async_wait_idle()

            assert len(seen) == 1
            assert seen[0].state(ROOM_ACTIVE) == "on"
            assert coordinator.decision.targets[BLIND].tilt == 100
            assert _would_call(coordinator) == ["cover.open_cover_tilt"]

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The same morning again, at the gap that beat a 2 s window.
# ---------------------------------------------------------------------------


def _seed_horucava(hass, *, cover_down="on", room_active="on"):
    """The world just before `svitanie` on the morning of 2026-09-01.

    Same as `_seed` plus the heat-protection helper the owner had switched on,
    which is what makes `cover_down` going off land in `horucava` rather than
    in the catch-all.
    """
    _seed(hass, cover_down=cover_down, room_active=room_active)
    hass.states.async_set(HEAT, "on")


def test_horucava_morning_is_evaluated_on_the_settled_world(
    hass_factory, runtime_entry, monkeypatch
):
    """`cover_down` off at T, the room flag off at T+2s: one evaluation, and it sees both.

    **Fails at `EVAL_SETTLE_SECONDS = 2.0`**, which is the point of it: a window
    equal to `svitanie`'s own `for: {seconds: 2}` is a coin flip between two
    reactions to one event, and on the live house it came up `tilt: 100` for a
    bedroom whose occupants were asleep. The real constant is used deliberately
    -- shrink it here and this stops being evidence about the length at all.
    """
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            _seed_horucava(hass)
            coordinator = CoverLogicCoordinator(hass, parsed(HORUCAVA), runtime_entry())
            await coordinator.async_setup()
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            assert coordinator.decision.mode == "noc"  # counter: the night, before svitanie
            seen.clear()

            hass.states.async_set(COVER_DOWN, "off")
            await asyncio.sleep(SVITANIE_GAP_SECONDS)
            hass.states.async_set(ROOM_ACTIVE, "off")
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            await coordinator.runner.async_wait_idle()

            # One evaluation for two writes about one transition -- and the
            # world it read had *both* of them. A window that merely ties the
            # gap gets two, the first of them on a world that never existed.
            assert len(seen) == 1, [(w.state(COVER_DOWN), w.state(ROOM_ACTIVE)) for w in seen]
            assert seen[0].state(COVER_DOWN) == "off"
            assert seen[0].state(ROOM_ACTIVE) == "off"

            # Counter: the mode really did change, so "nothing moved" is not
            # "nothing happened" -- rule 4 matched, which is what keep means.
            assert coordinator.decision.mode == "horucava"
            assert coordinator.decision.targets[BLIND].tilt is KEEP
            assert coordinator.decision.targets[BLIND].position is KEEP
            assert _would_call(coordinator) == []

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_the_unsettled_horucava_world_really_would_have_tilted_it_open(
    hass_factory, runtime_entry, monkeypatch
):
    """The counter: with the flag still on, `horucava` rule 5 opens the slats.

    Without this, the test above would go green on a configuration where rule 5
    had become unreachable -- a renamed helper, a mistyped mode -- and would be
    proving that nothing can move rather than that the settled world was read.
    """
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            _seed_horucava(hass)
            coordinator = CoverLogicCoordinator(hass, parsed(HORUCAVA), runtime_entry())
            await coordinator.async_setup()
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            seen.clear()

            # The same trigger, and the room flag deliberately never reset --
            # i.e. exactly the world a too-short window reads at T+2s.
            hass.states.async_set(COVER_DOWN, "off")
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            await coordinator.runner.async_wait_idle()

            assert len(seen) == 1
            assert seen[0].state(ROOM_ACTIVE) == "on"
            assert coordinator.decision.mode == "horucava"
            assert coordinator.decision.targets[BLIND].tilt == 100
            assert _would_call(coordinator) == ["cover.open_cover_tilt"]

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Restart-on-change, not a fixed window.
# ---------------------------------------------------------------------------


def test_a_change_inside_the_window_postpones_the_evaluation(
    hass_factory, runtime_entry, monkeypatch
):
    """Three writes, each inside the previous one's window: one evaluation, at the end.

    A fixed window opened by the first write fires in the middle of this burst
    -- reading the second value and missing the third -- and then fires a
    second time for the leftovers. Both halves of that are asserted against
    here: one evaluation, and it saw the last write.

    The window is shrunk for this test only. What is under test is the
    *shape* of the timer (does a new change move the deadline), not its
    length; the length is what `test_svitanie_...` above pins down, using the
    real constant.
    """
    monkeypatch.setattr("cover_logic.coordinator.EVAL_SETTLE_SECONDS", 0.5)
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            # Already past svitanie: the mode is `den`, so the room flag alone
            # decides, and each write below changes the answer.
            _seed(hass, cover_down="off", room_active="on")
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()
            await coordinator.runner.async_wait_idle()
            seen.clear()

            hass.states.async_set(ROOM_ACTIVE, "off")
            await asyncio.sleep(0.35)
            hass.states.async_set(ROOM_ACTIVE, "on")
            await asyncio.sleep(0.35)
            hass.states.async_set(ROOM_ACTIVE, "off")
            await asyncio.sleep(0.5 + 0.4)
            await coordinator.runner.async_wait_idle()

            assert len(seen) == 1, [w.state(ROOM_ACTIVE) for w in seen]
            assert seen[0].state(ROOM_ACTIVE) == "off"
            assert coordinator.decision.targets[BLIND].tilt is KEEP

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_burst_of_five_changes_is_one_evaluation(
    hass_factory, runtime_entry, monkeypatch, short_settle_window
):
    """Five writes in the same event-loop turn produce one engine call, not five.

    Coalescing, not length: the window is shrunk here, and the writes are in
    one loop turn so no plausible window separates them.
    """
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, cover_down="off", room_active="off")
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()
            await coordinator.runner.async_wait_idle()
            seen.clear()

            for value in ("on", "off", "on", "off", "on"):
                hass.states.async_set(ROOM_ACTIVE, value)
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            await coordinator.runner.async_wait_idle()

            assert len(seen) == 1
            assert seen[0].state(ROOM_ACTIVE) == "on"

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The cap: a restarting timer must not be restartable forever.
# ---------------------------------------------------------------------------


def test_a_flapping_entity_cannot_postpone_the_evaluation_forever(
    hass_factory, runtime_entry, monkeypatch
):
    """A sensor changing faster than the window still gets evaluated, at the cap.

    Restart-on-change alone is starvable: an entity that changes every 100ms
    with a 2s window is never quiet, so a purely restarting timer never fires
    and the integration goes deaf for as long as the flapping lasts. This house
    has had exactly such a sensor (a Hue occupancy sensor with
    `occupancy_timeout=0`, `CLAUDE.md`'s "Kreslo senzor cuká").

    Both constants are shrunk, keeping their ratio: what is asserted is that
    *a* cap exists and is measured from the first change of the burst.
    """
    monkeypatch.setattr("cover_logic.coordinator.EVAL_SETTLE_SECONDS", 0.3)
    monkeypatch.setattr("cover_logic.coordinator.EVAL_SETTLE_MAX_SECONDS", 0.9)
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, cover_down="off", room_active="off")
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()
            await coordinator.runner.async_wait_idle()
            seen.clear()

            started = time.monotonic()
            fired_after = None
            # 2.5s of flapping every 100ms: never quiet for the 0.3s window.
            for index in range(25):
                hass.states.async_set(ROOM_ACTIVE, "on" if index % 2 else "off")
                await asyncio.sleep(0.1)
                if seen and fired_after is None:
                    fired_after = time.monotonic() - started

            assert fired_after is not None, (
                "the evaluation never happened while the entity kept changing -- "
                "a restarting timer with no cap is starvable"
            )
            # At the cap, not at the end of the flapping: generous upper bound,
            # but far below the 2.5s the burst lasted.
            assert fired_after < 1.5, fired_after

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_the_cap_is_wider_than_the_window() -> None:
    """A cap at or below the window would make every evaluation fire at the cap."""
    assert EVAL_SETTLE_MAX_SECONDS > EVAL_SETTLE_SECONDS
    # And not merely wider: the cap runs from the *first* change of a burst, so
    # one that permits fewer than two windows is a fixed window in disguise --
    # the shape `_schedule_settle` exists to avoid. The case it must clear is
    # the chained one: a write, a `for:`-delayed write behind it, and a full
    # window after that.
    assert EVAL_SETTLE_MAX_SECONDS >= 2 * EVAL_SETTLE_SECONDS + SETTLE_MUST_OUTLAST_SECONDS


def test_the_window_outlasts_every_for_it_must_beat() -> None:
    """Strictly longer than the longest `for:` on any automation that writes what we read.

    Equality is not enough, and that is the whole finding of 2026-09-01: the
    window was 2.0 s, `svitanie`'s trigger is `for: {seconds: 2}`, and two
    reactions to one event with the same delay are decided by whichever timer
    the loop reaches first. Strictly greater is an ordering; equal is a toss.
    """
    assert EVAL_SETTLE_SECONDS > SETTLE_MUST_OUTLAST_SECONDS
    # Counter: the number being beaten is the measured 5 s (`kvety`,
    # `input_number.kvety_pozicia_zaluzie`), not something small enough that
    # any window at all would clear it -- and it is strictly greater than the
    # 2 s that ten of that automation's triggers use, so it really is the
    # longest of the colliding ones.
    assert SETTLE_MUST_OUTLAST_SECONDS == 5.0
    assert SETTLE_MUST_OUTLAST_SECONDS > 2.0
    # The window must also leave room for the writing automation's own run
    # time -- the write lands at `for:` plus a template recompute and a service
    # call, not at `for:` exactly.
    assert EVAL_SETTLE_SECONDS - SETTLE_MUST_OUTLAST_SECONDS >= 1.0


# ---------------------------------------------------------------------------
# What the settle window must NOT delay.
# ---------------------------------------------------------------------------


def test_startup_is_not_exempt_from_the_window(hass_factory, runtime_entry, monkeypatch):
    """`async_setup` arms the window; it does not evaluate inline.

    This test used to assert the exact opposite, and the exemption's stated
    reason -- "there is no transition at startup, only a world that already is
    what it is" -- was false: startup is the largest burst of state writes the
    house ever has. On 2026-08-31 at 11:45:35 the first evaluation ran 0.5 s
    after setup, on a world where nothing had been restored, and decided "open"
    for all ten blinds. What that costs is one window's delay before the
    diagnostic sensor has anything to show; see `coordinator.async_setup`'s
    docstring, and `tests/ha/test_readiness_gate.py` for the gate that makes
    such a decision unactionable rather than merely late.
    """
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass)
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()

            assert seen == []
            assert coordinator.decision is None

            # Counter: it is armed, not skipped -- one window later it has run.
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.5)
            assert len(seen) == 1
            assert coordinator.decision is not None

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_the_recheck_timer_is_not_delayed_by_the_window(hass_factory, runtime_entry):
    """A `defer` with `max_wait: 1` times out at ~1s, not at 1s + the settle window.

    Nothing changes state here at all, so only `deferrals.next_recheck` can
    wake this up. Routing that timer through the settle window would add the
    whole window to every guard deadline in the house -- silently, and always
    in the direction of "the interlock released later than it said".
    """

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, cover_down="off", room_active="on")
            coordinator = CoverLogicCoordinator(hass, config(DEFER_GUARD), runtime_entry())
            await coordinator.async_setup()
            # The first evaluation is what arms the deferral, and it now waits
            # out the settle window like any other -- so the deadline is
            # counted from here, not from `async_setup`.
            await asyncio.sleep(EVAL_SETTLE_SECONDS + 0.3)
            assert _would_call(coordinator) == []  # counter: held back by the guard

            # `max_wait` plus a slack strictly smaller than one settle window:
            # the wait must be over by now, and it would *not* be if this
            # timer went through the window (that would land at 1.0 +
            # EVAL_SETTLE_SECONDS, a second later than this sleep ends).
            await asyncio.sleep(1.0 + min(0.9, EVAL_SETTLE_SECONDS - 0.1))
            await coordinator.runner.async_wait_idle()

            would = _would_call(coordinator)
            assert would, coordinator.commands.recent
            assert {
                entry["src"]
                for entry in reversed(coordinator.commands.recent)
                if entry["kind"] == COMMAND_WOULD_CALL
            } == {SOURCE_GUARD_TIMEOUT}

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())
