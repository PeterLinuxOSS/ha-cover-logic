"""The readiness gate, counted where it matters: at `hass.services.async_call`.

**This module reproduces one measured minute.** On 2026-08-31, on the live
house (local times, from the log):

    11:45:34.995   cover_logic set up
    11:45:35.0xx   cover_logic[dry_run] cover.kuchyna_zaluzia_1_4 ... SetPosition(100)
                   ... the same SetPosition(100) for all ten blinds
    11:49:31       every blind in the house opened
    11:50:28       ERROR ... cover.kuchyna_zaluzia_1_4 failed on SetPosition(100)

Half a second after setup, every entity the configuration reads was still
missing or `unavailable` -- Home Assistant had not finished restoring state --
mode resolution fell through to the catch-all, and every zone got its daytime
"open" rule. `dry_run` was the only thing between that and the house.

`test_a_coordinator_set_up_on_an_unavailable_house_calls_nothing` is that
minute. It runs with `dry_run` **off**, because the assertion has to be zero
calls at the real service registry rather than zero calls at a brake that is on
anyway -- a test that asserts "nothing moved" while the brake is engaged proves
only that the brake works.

Every "nothing happened" here carries a counter, for the reason `MODELS.md` §9
records: three defects have shipped past a green suite in this project, and
this one shipped past 1060 tests. A gate that blocked everything forever, a
decision that happened to be `keep`, a spy that could never record, an engine
that crashed -- all four make "zero calls" true. So each test also asserts what
the engine decided, and each is paired with the identical world minus the
fault.

Uses `hass_factory` (a real, minimal `HomeAssistant`) for the reason
`test_coordinator.py` gives: the state-change subscription and the settle timer
both need genuine bus dispatch and genuine loop timers.
"""

import asyncio
import logging

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import ServiceRegistry

from cover_logic.config_schema import load_config
from cover_logic.const import COMMAND_WITHHELD, OPT_DRY_RUN, READINESS_REASON_PREFIX
from cover_logic.coordinator import CoverLogicCoordinator, evaluate as real_evaluate
from cover_logic.validation import ERROR, validate

from .conftest import SHORT_SETTLE_SECONDS

# The gate is what is under test, not the window's length; `test_settle.py`
# owns the latter and waits out the real 8 s constant.
pytestmark = pytest.mark.usefixtures("short_settle_window")

_WAIT = SHORT_SETTLE_SECONDS + 0.4

BLIND = "cover.a"
OTHER = "cover.b"
MODE_INPUT = "input_boolean.cover_down"
A_INPUT = "binary_sensor.a"
B_INPUT = "binary_sensor.b"

# The incident's shape, minimised: a night mode chosen by one house-wide
# switch, and a daytime rule per zone that opens the blind. With every input
# unreadable, `noc` cannot match and both zones get `position: 100` -- which is
# exactly what all ten blinds got at 11:45:35.
BASE = """
blinds:
  - entity: cover.a
    travel_time: 0.2
  - entity: cover.b
    travel_time: 0.2
zones:
  za:
    members: [cover.a]
  zb:
    members: [cover.b]
conditions:
  nocny_rezim:
    condition: state
    entity_id: input_boolean.cover_down
    state: "on"
  a_aktivna:
    condition: state
    entity_id: binary_sensor.a
    state: "on"
  b_aktivna:
    condition: state
    entity_id: binary_sensor.b
    state: "on"
modes:
  - id: noc
    when: !ref nocny_rezim
  - id: bezny_den
rules:
  noc.*:
    - then: {position: keep, tilt: keep}
  bezny_den.za:
    - if: !ref a_aktivna
      then: {position: keep, tilt: keep}
    - then: {position: 100, tilt: keep}
  bezny_den.zb:
    - if: !ref b_aktivna
      then: {position: keep, tilt: keep}
    - then: {position: 100, tilt: keep}
"""

# `max_wait` shorter than `recheck_every`, so only the deadline releases it --
# the shape `tests/ha/test_settle.py` uses for the same reason.
DEFER_GUARD = """
guards:
  - name: door open
    policy: defer
    applies_to: any
    targets: [za]
    when: !ref a_aktivna
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
    """Shrink the *planner's* post-arrival settle -- a different constant entirely.

    `planner.SETTLE_SECONDS` is a fact about these motors;
    `const.EVAL_SETTLE_SECONDS` is a fact about how the house writes state.
    """
    monkeypatch.setattr("cover_logic.planner.SETTLE_SECONDS", 0.01)


def _seed_unavailable(hass):
    """The world at 11:45:35: every input `unavailable`, the blinds readable and shut.

    The blinds themselves report a position, deliberately. If they were
    unreadable too, "nothing was issued" would also be true because the planner
    had nothing to compare against -- and this test would pass for the wrong
    reason.
    """
    for entity in (MODE_INPUT, A_INPUT, B_INPUT):
        hass.states.async_set(entity, "unavailable")
    _seed_blinds(hass)


def _seed_healthy(hass, *, night="off", a_active="off", b_active="off"):
    """The same world with every input readable."""
    hass.states.async_set(MODE_INPUT, night)
    hass.states.async_set(A_INPUT, a_active)
    hass.states.async_set(B_INPUT, b_active)
    _seed_blinds(hass)


def _seed_blinds(hass, *, position=0):
    """Both blinds shut, so `position: 100` is a real movement and not a dead band."""
    for entity in (BLIND, OTHER):
        hass.states.async_set(
            entity, "closed", {"current_position": position, "current_tilt_position": position}
        )


def _spy_on_services(monkeypatch):
    """Record every `hass.services.async_call`; nothing else in these tests calls one.

    Patched on the class, not the instance (`ServiceRegistry` has `__slots__`),
    exactly as `tests/ha/test_wiring.py` does -- which is what makes the
    returned list this integration's entire output, and so what lets a test
    assert *zero*.
    """
    calls = []

    async def _record(_self, domain, service, service_data=None, blocking=False, **_kwargs):
        calls.append({"service": service, "data": dict(service_data or {})})

    monkeypatch.setattr(ServiceRegistry, "async_call", _record)
    return calls


def _services_for(calls, entity):
    """The service names aimed at one entity, in order."""
    return [call["service"] for call in calls if call["data"].get("entity_id") == entity]


def _withheld(coordinator):
    """Every `withheld` entry whose reason is a readiness verdict, oldest first."""
    return [
        entry
        for entry in reversed(coordinator.commands.recent)
        if entry["kind"] == COMMAND_WITHHELD
        and str(entry["reason"]).startswith(READINESS_REASON_PREFIX)
    ]


async def _settle(coordinator):
    """Let the settle window, the evaluation and every queued sequence finish."""
    await asyncio.sleep(_WAIT)
    await coordinator.runner.async_wait_idle()


def _live(runtime_entry):
    """A config entry with the brake off -- the only way to count real service calls."""
    return runtime_entry({OPT_DRY_RUN: False})


# ---------------------------------------------------------------------------
# The measured minute.
# ---------------------------------------------------------------------------


def test_a_coordinator_set_up_on_an_unavailable_house_calls_nothing(
    hass_factory, runtime_entry, monkeypatch
):
    """11:45:35, reproduced: brake off, every input `unavailable`, zero service calls.

    The three counters are what stop this being vacuous, and each removes one
    way of passing without a gate:

    - the engine really did decide `position: 100` for both blinds, so the zero
      is the gate's doing and not a decision that happened to be `keep`;
    - the evaluation succeeded (`last_error is None`) and was published, so the
      zero is not a crash;
    - the withholding was recorded and names the entities, so the zero is not
      silence.
    """

    async def _run():
        hass = hass_factory()
        calls = _spy_on_services(monkeypatch)
        try:
            _seed_unavailable(hass)
            coordinator = CoverLogicCoordinator(hass, config(), _live(runtime_entry))
            await coordinator.async_setup()
            await _settle(coordinator)

            assert coordinator.dry_run is False
            assert calls == []

            # Counter 1: the decision it would have dispatched was house-wide open.
            assert coordinator.decision.mode == "bezny_den"
            assert coordinator.decision.targets[BLIND].position == 100
            assert coordinator.decision.targets[OTHER].position == 100
            # Counter 2: it evaluated cleanly and published -- no crash, no blank.
            assert coordinator.last_error is None
            assert coordinator.last_success is not None
            # Counter 3: it said so, per blind, naming the cause.
            assert coordinator.readiness.ready is False
            assert set(coordinator.readiness.blocked) == {BLIND, OTHER}
            reasons = {entry["blind"]: entry["reason"] for entry in _withheld(coordinator)}
            assert set(reasons) == {BLIND, OTHER}
            assert MODE_INPUT in reasons[BLIND]

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_the_same_house_with_its_inputs_present_really_does_open_both_blinds(
    hass_factory, runtime_entry, monkeypatch
):
    """The counter to the test above: same config, same spy, inputs readable.

    Without this, a spy that could never record anything -- or a coordinator
    that never dispatches at all -- would satisfy every line of the
    reproduction.
    """

    async def _run():
        hass = hass_factory()
        calls = _spy_on_services(monkeypatch)
        try:
            _seed_healthy(hass)
            coordinator = CoverLogicCoordinator(hass, config(), _live(runtime_entry))
            await coordinator.async_setup()
            await _settle(coordinator)

            assert _services_for(calls, BLIND) == ["open_cover"]
            assert _services_for(calls, OTHER) == ["open_cover"]
            assert coordinator.readiness.ready is True
            assert _withheld(coordinator) == []

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_once_the_entities_appear_the_next_evaluation_dispatches(
    hass_factory, runtime_entry, monkeypatch
):
    """The veto lifts by itself: unavailable at setup, readable a moment later.

    A gate that blocks and never unblocks is a house that never moves again --
    a worse defect than the one being fixed, and one every "nothing happened"
    assertion in this file would happily accept.
    """

    async def _run():
        hass = hass_factory()
        calls = _spy_on_services(monkeypatch)
        try:
            _seed_unavailable(hass)
            coordinator = CoverLogicCoordinator(hass, config(), _live(runtime_entry))
            await coordinator.async_setup()
            await _settle(coordinator)
            assert calls == []

            # Home Assistant finishes restoring state.
            _seed_healthy(hass)
            await _settle(coordinator)

            assert _services_for(calls, BLIND) == ["open_cover"]
            assert coordinator.readiness.blocked == {}

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Mid-day: the four silent minutes of 2026-08-06, from the other side.
# ---------------------------------------------------------------------------


def test_an_input_that_dies_after_a_healthy_start_stops_dispatch(
    hass_factory, runtime_entry, monkeypatch
):
    """A healthy house, one entity goes `unavailable`, and the next decision is not acted on.

    This is the house's own `CLAUDE.md` entry for 2026-08-06 --
    `sensor.zaluzie_cielovy_stav` `unavailable` for four minutes -- with the
    difference that the old system's silence was accidental and this one's is
    recorded.

    The first half is the counter, in the same test: the identical
    configuration dispatches normally while the entity is alive, so the second
    half is about the entity dying and nothing else.
    """

    async def _run():
        hass = hass_factory()
        calls = _spy_on_services(monkeypatch)
        try:
            # Night: nothing moves, and every input is readable.
            _seed_healthy(hass, night="on")
            coordinator = CoverLogicCoordinator(hass, config(), _live(runtime_entry))
            await coordinator.async_setup()
            await _settle(coordinator)
            assert coordinator.decision.mode == "noc"
            assert calls == []

            # Counter: morning, healthy -- this is what dispatch looks like.
            hass.states.async_set(MODE_INPUT, "off")
            await _settle(coordinator)
            assert _services_for(calls, BLIND) == ["open_cover"]
            calls.clear()

            # The mode input dies. `noc` can no longer match, so the engine
            # still decides open -- and now nothing may act on it.
            _seed_blinds(hass)
            hass.states.async_set(MODE_INPUT, "unavailable")
            await _settle(coordinator)

            assert calls == []
            assert coordinator.decision.targets[BLIND].position == 100
            assert coordinator.readiness.blocked_by(BLIND) == (MODE_INPUT,)
            assert [entry["blind"] for entry in _withheld(coordinator)] == [BLIND, OTHER]

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_blind_whose_own_inputs_are_readable_is_still_commanded(
    hass_factory, runtime_entry, monkeypatch
):
    """Per blind, not per house: `za`'s sensor dies, `zb` keeps working.

    This is the test a global veto fails, and the whole reason readiness is
    attributed per blind -- a sensor dead for a day must not stop the house for
    a day.
    """

    async def _run():
        hass = hass_factory()
        calls = _spy_on_services(monkeypatch)
        try:
            _seed_healthy(hass, night="on")
            coordinator = CoverLogicCoordinator(hass, config(), _live(runtime_entry))
            await coordinator.async_setup()
            await _settle(coordinator)
            assert calls == []

            # Morning, and only `za`'s own room sensor is unreadable.
            hass.states.async_set(A_INPUT, "unavailable")
            hass.states.async_set(MODE_INPUT, "off")
            await _settle(coordinator)

            assert _services_for(calls, BLIND) == []
            assert _services_for(calls, OTHER) == ["open_cover"]
            assert coordinator.readiness.blocked_by(BLIND) == (A_INPUT,)
            assert coordinator.readiness.blocked_by(OTHER) == ()

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_guard_timeout_does_not_bypass_the_gate(hass_factory, runtime_entry, monkeypatch):
    """A `defer` running out of patience is not a reason to trust a bad decision.

    The expired deadline goes in at `Priority.GUARD`, which outranks everything
    -- so it is the one dispatch path most likely to be written around a gate
    placed anywhere but the funnel every command goes through.
    """

    async def _run():
        hass = hass_factory()
        calls = _spy_on_services(monkeypatch)
        try:
            # `a_aktivna` on holds `za` back; then it dies, which both releases
            # the guard's condition and makes the world unreadable.
            _seed_healthy(hass, a_active="on")
            coordinator = CoverLogicCoordinator(hass, config(DEFER_GUARD), _live(runtime_entry))
            await coordinator.async_setup()
            await _settle(coordinator)
            assert _services_for(calls, BLIND) == []  # deferred, as configured

            hass.states.async_set(A_INPUT, "unavailable")
            await asyncio.sleep(1.0 + _WAIT)
            await coordinator.runner.async_wait_idle()

            assert _services_for(calls, BLIND) == []
            assert coordinator.readiness.blocked_by(BLIND) == (A_INPUT,)

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_standing_outage_is_recorded_once_not_once_per_recompute(
    hass_factory, runtime_entry, monkeypatch
):
    """An hour of `unavailable` must not turn `last_command` into a clock.

    The counter is in the same test: once the *set* of missing entities
    changes, it is recorded again -- so this is deduping on the reason, not
    recording only the first one ever.
    """

    async def _run():
        hass = hass_factory()
        _spy_on_services(monkeypatch)
        try:
            _seed_healthy(hass)
            hass.states.async_set(MODE_INPUT, "unavailable")
            coordinator = CoverLogicCoordinator(hass, config(), _live(runtime_entry))
            await coordinator.async_setup()
            await _settle(coordinator)
            assert len(_withheld(coordinator)) == 2  # one per blind

            # Three more recomputes, same outage, nothing new recorded.
            for value in ("on", "off", "on"):
                hass.states.async_set(B_INPUT, value)
                await _settle(coordinator)
            assert len(_withheld(coordinator)) == 2

            # Counter: a *different* fault is a different reason, so it speaks.
            hass.states.async_set(A_INPUT, "unavailable")
            await _settle(coordinator)
            reasons = {entry["reason"] for entry in _withheld(coordinator)}
            assert any(A_INPUT in reason for reason in reasons)

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Startup is no longer exempt from the settle window.
# ---------------------------------------------------------------------------


def test_startup_goes_through_the_settle_window(hass_factory, runtime_entry):
    """`async_setup` arms the window instead of evaluating inline.

    The exemption this replaces was justified as "there is no transition at
    startup" -- and startup is the largest burst of state writes the house
    ever has. The cost is stated in `async_setup`'s docstring and asserted
    here: `decision` is `None` when setup returns, and populated one window
    later.
    """

    async def _run():
        hass = hass_factory()
        try:
            _seed_healthy(hass, night="on")
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()

            assert coordinator.decision is None
            assert coordinator.readiness is None

            await _settle(coordinator)
            assert coordinator.decision is not None
            assert coordinator.decision.mode == "noc"

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_state_change_during_startup_postpones_the_first_evaluation(
    hass_factory, runtime_entry, monkeypatch
):
    """State restore writing mid-window moves the deadline, as any other burst does.

    This is what the exemption cost in practice: the first evaluation used to
    read whatever had been restored by the time `async_setup` ran, which on
    2026-08-31 was nothing at all.
    """
    seen = []

    def _wrapped(config_, world):
        seen.append(world)
        return real_evaluate(config_, world)

    monkeypatch.setattr("cover_logic.coordinator.evaluate", _wrapped)

    async def _run():
        hass = hass_factory()
        try:
            # Setup happens on the unrestored world, as it did in the incident.
            _seed_unavailable(hass)
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()
            assert seen == []

            # Restore lands inside the window.
            await asyncio.sleep(SHORT_SETTLE_SECONDS / 2)
            _seed_healthy(hass, night="on")
            await _settle(coordinator)

            # One evaluation, and it read the restored world -- not the empty one.
            assert len(seen) == 1, [w.state(MODE_INPUT) for w in seen]
            assert seen[0].state(MODE_INPUT) == "on"

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The second defect from the same log: a live dispatch left no line.
# ---------------------------------------------------------------------------


def test_a_live_command_is_logged_at_info_like_a_dry_run_one(
    hass_factory, runtime_entry, monkeypatch, caplog
):
    """1468 `[dry_run]` lines and no `[live]` line, on the day blinds actually moved.

    `runner._log_fields` logged a live command at `DEBUG` and a suppressed one
    at `INFO`, so the only record of what the house was told to do was the one
    thing that never reached a motor. Both are `INFO` now.

    The counter is the second half: the same coordinator, brake on, still
    writes its line -- so this is not "moved everything to a level that happens
    to be captured", it is both states being visible at the level Home
    Assistant logs by default.
    """

    async def _run():
        hass = hass_factory()
        _spy_on_services(monkeypatch)
        try:
            _seed_healthy(hass)
            entry = _live(runtime_entry)
            coordinator = CoverLogicCoordinator(hass, config(), entry)
            with caplog.at_level(logging.INFO, logger="cover_logic.runner"):
                await coordinator.async_setup()
                await _settle(coordinator)

                live = [
                    record
                    for record in caplog.records
                    if "cover_logic[live]" in record.getMessage()
                ]
                assert live, [record.getMessage() for record in caplog.records]
                assert {record.levelno for record in live} == {logging.INFO}
                assert any(BLIND in record.getMessage() for record in live)

                # Counter: the dry-run line is still there, at the same level.
                caplog.clear()
                entry.options[OPT_DRY_RUN] = True
                _seed_blinds(hass)
                hass.states.async_set(A_INPUT, "on")
                await _settle(coordinator)
                hass.states.async_set(A_INPUT, "off")
                await _settle(coordinator)

                dry = [
                    record
                    for record in caplog.records
                    if "cover_logic[dry_run]" in record.getMessage()
                ]
                assert dry, [record.getMessage() for record in caplog.records]
                assert {record.levelno for record in dry} == {logging.INFO}

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())
