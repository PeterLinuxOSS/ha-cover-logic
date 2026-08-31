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

The three tests that matter here, and what each would catch:

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
from cover_logic.const import COMMAND_WOULD_CALL, EVAL_SETTLE_MAX_SECONDS, EVAL_SETTLE_SECONDS
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


def config(extra=""):
    """Parse `BASE + extra`, refusing anything this project would not accept."""
    parsed = load_config(BASE + extra)
    assert [p for p in validate(parsed) if p.severity == ERROR] == []
    return parsed


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


def test_a_burst_of_five_changes_is_one_evaluation(hass_factory, runtime_entry, monkeypatch):
    """Five writes in the same event-loop turn produce one engine call, not five."""
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
    # The measured gap between `svitanie`'s two writes is one second; a window
    # shorter than that is the defect this module exists for.
    assert EVAL_SETTLE_SECONDS >= 2.0


# ---------------------------------------------------------------------------
# What the settle window must NOT delay.
# ---------------------------------------------------------------------------


def test_startup_evaluates_without_waiting_for_the_window(hass_factory, runtime_entry, monkeypatch):
    """`async_setup` evaluates inline: `decision` is populated when it returns.

    The settle window is about *state-change-driven* evaluation. Routing the
    first evaluation through it would leave the diagnostic sensor blank for two
    seconds after every reload, and -- worse -- leave a pending deferral's
    recheck timer unarmed for that long.
    """
    seen = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass)
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()

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
