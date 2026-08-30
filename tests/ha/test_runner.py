"""The parts of `runner.py` that need a real event loop: queues, cancellation, dry run.

Imports Home Assistant, so this module only collects under the Python 3.14
venv (`.venv/bin/python -m pytest`). Everything testable without `hass` --
the translation table, the arrival predicate, `_arbitrate`, `_carry_over_tilt`,
the log line -- is in `tests/test_runner.py` instead, and is not repeated here.

Uses `hass_factory` (a real, minimal `HomeAssistant`) for the same reason
`test_coordinator.py` does: `async_track_state_change_event` needs genuine bus
dispatch, and re-implementing that in a fake is exactly the subtle
incorrectness a fake is meant to avoid.

Two conventions worth stating, because several assertions depend on them:

- The recording caller `await asyncio.sleep(0)`s, the way a real service call
  suspends. Without that yield, a sequence would run to completion inside one
  scheduling slot and the interleaving tests below would prove nothing.
- Blinds here are configured with `travel_time=0.2`, so an arrival wait is
  0.3s rather than 90s. That is the blind's own configuration, not a runner
  constant -- `planner.ARRIVAL_TIMEOUT_FACTOR` is untouched.
"""

import asyncio
import logging

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError

from cover_logic.const import OPT_DRY_RUN
from cover_logic.model import KEEP, Action, Blind
from cover_logic.runner import CoverRunner, Priority

_LOGGER_NAME = "cover_logic.runner"

# No arrival wait: `plan()` only inserts one when both axes move *and* the
# blind wants its tilt after arrival. These blinds ask for the tilt straight
# away, which keeps the queue tests down to two commands each.
FAST = Blind(entity="cover.a", travel_time=0.2, tilt_after_arrival=False)
# The full four-command shape: position, arrival wait, settle, tilt.
SLOW = Blind(entity="cover.a", travel_time=0.2, tilt_after_arrival=True)

CLOSE = Action(position=0, tilt=0)
OPEN = Action(position=100, tilt=100)


class _Entry:
    """The one attribute `CoverRunner` reads off a config entry.

    A plain dict rather than the real `MappingProxyType`, so a test can flip
    the switch mid-run the way `async_update_entry` does in production -- which
    is the whole point of the option living in `entry.options` at all.
    """

    def __init__(self, options=None):
        self.options = dict(options or {})


@pytest.fixture(autouse=True)
def _short_settle(monkeypatch):
    """Shrink the post-arrival settle from 2s to 10ms for the whole module.

    `planner.SETTLE_SECONDS` is a fact about these motors (they report arrival
    slightly before they stop), not about the runner: the runner's behaviour is
    identical either way, it just would not be worth two seconds per sequence
    to observe. Patched on `planner`, where `plan()` reads it.
    """
    monkeypatch.setattr("cover_logic.planner.SETTLE_SECONDS", 0.01)


def _motor(hass, calls, *, arrives=True):
    """A recording cover caller that optionally moves the blind it is told to move.

    `arrives=False` is a blind whose position never changes -- a frozen cover,
    the case every arrival wait exists for.
    """

    async def _call(service, data):
        calls.append((service, dict(data)))
        await asyncio.sleep(0)
        if not arrives:
            return
        entity = data["entity_id"]
        state = hass.states.get(entity)
        attributes = dict(state.attributes) if state is not None else {}
        if service == "close_cover":
            attributes["current_position"] = 0
        elif service == "open_cover":
            attributes["current_position"] = 100
        elif service == "set_cover_position":
            attributes["current_position"] = data["position"]
        elif service == "close_cover_tilt":
            attributes["current_tilt_position"] = 0
        elif service == "open_cover_tilt":
            attributes["current_tilt_position"] = 100
        elif service == "set_cover_tilt_position":
            attributes["current_tilt_position"] = data["tilt_position"]
        hass.states.async_set(entity, "open", attributes)

    return _call


def _services(calls):
    return [service for service, _data in calls]


def _live_runner(hass, calls, **kwargs):
    return CoverRunner(hass, _Entry({OPT_DRY_RUN: False}), _motor(hass, calls, **kwargs))


# ---------------------------------------------------------------------------
# One entity per call.
# ---------------------------------------------------------------------------


def test_every_service_call_names_exactly_one_entity(hass_factory):
    """Never a list. A call naming five covers cannot be cancelled for one of them."""
    calls = []

    async def _run():
        hass = hass_factory()
        try:
            runner = _live_runner(hass, calls)
            await runner.async_apply(FAST, CLOSE, priority=Priority.SCHEDULED, source="t")
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "close_cover_tilt"]
    for _service, data in calls:
        assert data["entity_id"] == "cover.a"
        assert isinstance(data["entity_id"], str)


# ---------------------------------------------------------------------------
# Serialisation per blind, and planning at the moment of starting.
# ---------------------------------------------------------------------------


def test_a_queued_request_is_planned_only_once_the_blind_has_finished_moving(hass_factory):
    """The queue's whole reason: a plan computed at request time is stale by the time it runs.

    The blind starts fully open. Request 1 closes it. Request 2 asks for
    `position 0, tilt 100`, and it is the *position* that discriminates: at
    request time the blind reports 100, so a plan computed then would emit
    `close_cover` a second time. Planned when it actually starts, the blind
    already reports 0 and the dead band drops the position entirely -- leaving
    a single tilt command.
    """
    calls = []

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 100, "current_tilt_position": 100}
            )
            runner = _live_runner(hass, calls)
            await runner.async_apply(FAST, CLOSE, priority=Priority.SCHEDULED, source="one")
            await runner.async_apply(
                FAST,
                Action(position=0, tilt=100),
                priority=Priority.SCHEDULED,
                source="two",
            )
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "close_cover_tilt", "open_cover_tilt"]
    # The counter to the assertion above: a runner that planned at request time
    # would have driven the position twice.
    assert _services(calls).count("close_cover") == 1


def test_an_identical_request_at_the_same_standing_never_runs_twice(hass_factory):
    calls = []

    async def _run():
        hass = hass_factory()
        try:
            runner = _live_runner(hass, calls)
            await runner.async_apply(FAST, CLOSE, priority=Priority.SCHEDULED, source="one")
            await runner.async_apply(FAST, CLOSE, priority=Priority.SCHEDULED, source="two")
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "close_cover_tilt"]


def test_the_waiting_slot_holds_one_request_and_the_newest_wins(hass_factory):
    """Depth 1: a queue of depth k makes the blind perform k-1 stale intentions."""
    calls = []

    async def _run():
        hass = hass_factory()
        try:
            runner = _live_runner(hass, calls)
            await runner.async_apply(FAST, CLOSE, priority=Priority.SCHEDULED, source="one")
            await runner.async_apply(
                FAST, Action(position=50, tilt=50), priority=Priority.SCHEDULED, source="two"
            )
            await runner.async_apply(FAST, OPEN, priority=Priority.SCHEDULED, source="three")
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "close_cover_tilt", "open_cover", "open_cover_tilt"]
    # The middle request was overwritten in the slot, not performed and then
    # undone -- which would have been a visible, pointless movement.
    assert "set_cover_position" not in _services(calls)


# ---------------------------------------------------------------------------
# Cancellation.
# ---------------------------------------------------------------------------


def test_a_guard_cancels_a_running_scheduled_close_at_a_command_boundary(hass_factory):
    """The closing tilt must never go out; the opening one must.

    The running sequence is parked in its arrival wait (the blind never
    reports), which is where a cancellation has to be able to land. Cancelling
    with `Task.cancel()` in the middle of the service call instead would be
    invisible here -- what this asserts is the *effect*: exactly one command
    from the abandoned sequence, and a complete sequence from its successor.
    """
    calls = []

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 50, "current_tilt_position": 50}
            )
            runner = _live_runner(hass, calls, arrives=False)
            await runner.async_apply(SLOW, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await asyncio.sleep(0.05)
            await runner.async_apply(SLOW, OPEN, priority=Priority.GUARD, source="wind")
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "open_cover", "open_cover_tilt"]
    assert "close_cover_tilt" not in _services(calls)


def test_an_abandoned_tilt_is_finished_by_the_successor_that_keeps_the_slat_axis(hass_factory):
    """The 2026-08-21 incident, end to end.

    The guard only names a position (`tilt=KEEP`), so nobody owns the slats;
    the tilt the cancelled sequence never sent is therefore carried onto the
    successor and does go out. Without the hand-over the blind ends this run
    moved and with untouched slats, which is exactly what happened that day.
    """
    calls = []

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 50, "current_tilt_position": 50}
            )
            runner = _live_runner(hass, calls, arrives=False)
            await runner.async_apply(SLOW, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await asyncio.sleep(0.05)
            await runner.async_apply(
                SLOW,
                Action(position=100, tilt=KEEP),
                priority=Priority.GUARD,
                source="door",
            )
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "open_cover", "close_cover_tilt"]


def test_the_successors_own_tilt_wins_over_the_abandoned_one(hass_factory):
    """The counter to the test above: a successor that names a tilt owns the axis."""
    calls = []

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 50, "current_tilt_position": 50}
            )
            runner = _live_runner(hass, calls, arrives=False)
            await runner.async_apply(SLOW, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await asyncio.sleep(0.05)
            await runner.async_apply(
                SLOW, Action(position=100, tilt=100), priority=Priority.GUARD, source="door"
            )
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert "close_cover_tilt" not in _services(calls)
    assert "open_cover_tilt" in _services(calls)


def test_a_dead_banded_successor_tilt_still_beats_the_abandoned_one(hass_factory):
    """The whole reason the hand-over asks the `Action` and not the `Plan`.

    The guard names `tilt: 50` and the blind already reports 50, so `plan()`
    emits no `SetTilt` at all. An implementation that decided "the successor's
    plan contains no tilt, therefore nobody owns the slats" would resurrect the
    abandoned `SetTilt(0)` and shut the slats for no reason -- the 2026-08-27
    class of unexplained movement. No tilt command may go out here at all.
    """
    calls = []

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 50, "current_tilt_position": 50}
            )
            runner = _live_runner(hass, calls, arrives=False)
            await runner.async_apply(SLOW, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await asyncio.sleep(0.05)
            await runner.async_apply(
                SLOW, Action(position=100, tilt=50), priority=Priority.GUARD, source="door"
            )
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "open_cover"]
    assert not [service for service in _services(calls) if service.endswith("tilt")]


# ---------------------------------------------------------------------------
# Arrival waits.
# ---------------------------------------------------------------------------


def test_a_blind_that_never_reports_arrival_still_gets_its_tilt(hass_factory, caplog):
    """`continue_on_timeout`, in the house's own words: a frozen cover must not block the tilt.

    And it is not silent: exactly one WARNING, naming the entity and the
    position it never reached -- the number whose right response is to check
    that node's `last_seen`, not to lengthen the timeout.
    """
    calls = []
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 100, "current_tilt_position": 100}
            )
            runner = _live_runner(hass, calls, arrives=False)
            await runner.async_apply(SLOW, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "close_cover_tilt"]
    timeouts = [r for r in caplog.records if "did not reach" in r.message]
    assert len(timeouts) == 1
    assert "cover.a" in timeouts[0].getMessage()


def test_a_blind_already_at_target_does_not_wait_for_an_event_that_never_comes(hass_factory):
    """The predicate is evaluated on entry, not only on `state_changed`.

    The motor here reports arrival synchronously, inside the service call --
    before the listener is attached. A runner that only listened would sit out
    the whole timeout, so this is timed: the sequence must finish in well under
    the 0.3s arrival timeout this blind is configured for.
    """
    calls = []
    elapsed = []

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 100, "current_tilt_position": 100}
            )
            runner = _live_runner(hass, calls)
            started = asyncio.get_running_loop().time()
            await runner.async_apply(SLOW, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_wait_idle()
            elapsed.append(asyncio.get_running_loop().time() - started)
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "close_cover_tilt"]
    assert elapsed[0] < 0.2, f"waited {elapsed[0]:.3f}s for an arrival that had already happened"


# ---------------------------------------------------------------------------
# Parallelism between blinds.
# ---------------------------------------------------------------------------


def test_ten_blinds_move_together_rather_than_in_waves(hass_factory):
    """2026-08-05: the house moved in waves for minutes and woke two people.

    Ten sequences start in the same tick. With one queue per blind every
    position command goes out before any tilt command does; a single global
    lock would produce `pos, tilt, pos, tilt, ...` instead.
    """
    calls = []
    blinds = [
        Blind(entity=f"cover.b{i}", travel_time=0.2, tilt_after_arrival=False) for i in range(10)
    ]

    async def _run():
        hass = hass_factory()
        try:
            runner = _live_runner(hass, calls, arrives=False)
            for blind in blinds:
                await runner.async_apply(blind, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    services = _services(calls)
    assert len(calls) == 20
    positions = [i for i, service in enumerate(services) if service == "close_cover"]
    tilts = [i for i, service in enumerate(services) if service == "close_cover_tilt"]
    assert len(positions) == len(tilts) == 10
    assert max(positions) < min(tilts), f"blinds moved in waves: {services}"


# ---------------------------------------------------------------------------
# dry_run.
# ---------------------------------------------------------------------------


def test_dry_run_is_on_by_default_and_issues_nothing(hass_factory, caplog):
    """An integration that has never had hands must not be handed a pair silently.

    The entry carries no options at all here -- which is what an entry created
    before the runner existed looks like after a restart.
    """
    calls = []
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    async def _run():
        hass = hass_factory()
        try:
            runner = CoverRunner(hass, _Entry(), _motor(hass, calls))
            await runner.async_apply(
                FAST, CLOSE, priority=Priority.SCHEDULED, source="matrix", mode="vecer"
            )
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert calls == []
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("cover_logic[")]
    assert len(lines) == 2
    assert all("would_call=" in line for line in lines)
    assert any("would_call=cover.close_cover " in line for line in lines)
    assert any("would_call=cover.close_cover_tilt " in line for line in lines)
    assert all("src=matrix" in line and "mode=vecer" in line for line in lines)


def test_turning_dry_run_off_at_runtime_takes_effect_on_the_next_sequence(hass_factory):
    """The emergency path: "turn it off now" must not need a reload.

    A runner that read `entry.options` once at construction would still be
    silent on the second sequence.
    """
    calls = []
    entry = _Entry()

    async def _run():
        hass = hass_factory()
        try:
            runner = CoverRunner(hass, entry, _motor(hass, calls))
            await runner.async_apply(FAST, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_wait_idle()
            assert calls == []

            entry.options = {OPT_DRY_RUN: False}

            await runner.async_apply(FAST, OPEN, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["open_cover", "open_cover_tilt"]


def test_dry_run_does_not_skip_the_waits(hass_factory):
    """A dry run that skipped its waits would never exercise queue arbitration at all.

    The sequence must take at least the arrival timeout it was planned with --
    nothing is moving, so the wait genuinely expires.
    """
    calls = []
    elapsed = []

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 100, "current_tilt_position": 100}
            )
            runner = CoverRunner(hass, _Entry(), _motor(hass, calls))
            started = asyncio.get_running_loop().time()
            await runner.async_apply(SLOW, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_wait_idle()
            elapsed.append(asyncio.get_running_loop().time() - started)
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert calls == []
    assert elapsed[0] >= SLOW.travel_time * 1.5


def test_a_suppressed_command_is_logged_with_its_reason(hass_factory, caplog):
    """A blind the dead band leaves alone is still reported, with its reason.

    "Nothing happened" and "nothing was logged" must not look the same. The
    most valuable finding of a dry-run day is where this runner stands still
    and the old script moves; without this line that case is invisible.
    """
    calls = []
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 32, "current_tilt_position": 50}
            )
            runner = CoverRunner(hass, _Entry(), _motor(hass, calls))
            await runner.async_apply(
                FAST,
                Action(position=34, tilt=50),
                priority=Priority.SCHEDULED,
                source="matrix",
                mode="vecer",
            )
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("cover_logic[")]
    assert len(lines) == 2
    assert all("cmd=none" in line and "step=-/-" in line for line in lines)
    assert any("axis=position reason=dead_band target=34 pos=32" in line for line in lines)
    assert any("axis=tilt reason=dead_band target=50 pos=50" in line for line in lines)


# ---------------------------------------------------------------------------
# Failure containment and shutdown.
# ---------------------------------------------------------------------------


def test_one_blind_failing_neither_stops_the_others_nor_hides_the_lost_tilt(hass_factory, caplog):
    """`continue_on_error`, one queue wide -- and the rest of the sequence named out loud."""
    calls = []
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    other = Blind(entity="cover.other", travel_time=0.2, tilt_after_arrival=False)

    async def _call(service, data):
        calls.append((service, dict(data)))
        await asyncio.sleep(0)
        if data["entity_id"] == "cover.a":
            msg = "motor offline"
            raise HomeAssistantError(msg)

    async def _run():
        hass = hass_factory()
        try:
            runner = CoverRunner(hass, _Entry({OPT_DRY_RUN: False}), _call)
            await runner.async_apply(FAST, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_apply(other, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_wait_idle()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert ("close_cover_tilt", {"entity_id": "cover.a"}) not in calls
    assert ("close_cover_tilt", {"entity_id": "cover.other"}) in calls
    abandoned = [r.getMessage() for r in caplog.records if "never issued" in r.getMessage()]
    assert len(abandoned) == 1
    assert "SetTilt(0)" in abandoned[0]
    assert "cover.a" in abandoned[0]


def test_shutdown_lets_a_sequence_finish_when_it_can(hass_factory, caplog):
    calls = []
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def _run():
        hass = hass_factory()
        try:
            runner = _live_runner(hass, calls)
            await runner.async_apply(FAST, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await runner.async_shutdown()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover", "close_cover_tilt"]
    assert [r for r in caplog.records if "never issued" in r.getMessage()] == []


def test_shutdown_names_every_command_it_could_not_send(hass_factory, caplog):
    """A tilt lost to a restart cannot be prevented -- but it must be visible.

    Repairing it is a person's job, and a person can only do that if the log
    says which blind and which command. Cancelling silently instead would leave
    a blind down with untouched slats and nothing to find.
    """
    calls = []
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "open", {"current_position": 100, "current_tilt_position": 100}
            )
            runner = _live_runner(hass, calls, arrives=False)
            await runner.async_apply(SLOW, CLOSE, priority=Priority.SCHEDULED, source="matrix")
            await asyncio.sleep(0.05)
            await runner.async_shutdown(grace=0)
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())

    assert _services(calls) == ["close_cover"]
    abandoned = [r.getMessage() for r in caplog.records if "never issued" in r.getMessage()]
    assert any("SetTilt(0)" in message for message in abandoned)
    assert all("cover.a" in message for message in abandoned)
