"""Tests for `coordinator.CoverLogicCoordinator`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv (`.venv/bin/python -m pytest`) -- see `test_ha_world.py`'s own note.

Uses `hass_factory` (a real, minimal `HomeAssistant`) rather than `FakeHass`:
`async_track_state_change_event` and `async_track_point_in_utc_time` both need
genuine bus dispatch and a genuine event loop timer, not a re-implementation of
either -- see `hass_factory`'s own docstring in `tests/ha/conftest.py` for the
full reasoning.

The settle window these tests wait out is `const.EVAL_SETTLE_SECONDS`, and
`tests/ha/test_settle.py` is where its length and its restart-on-change shape
are actually asserted; here it is only a delay to wait past, so it is shrunk to
`conftest.SHORT_SETTLE_SECONDS` for every test in this module.
"""

import asyncio

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import EVENT_STATE_CHANGED, Context

from cover_logic.config_schema import load_config
from cover_logic.coordinator import (
    CoverLogicCoordinator,
    _manual_move_blinds,
    evaluate as real_evaluate,
)
from cover_logic.engine import EngineError

from .conftest import SHORT_SETTLE_SECONDS

# Every test here is about what the coordinator does, not how long it waits
# first; the real 8 s window belongs to `test_settle.py`.
pytestmark = pytest.mark.usefixtures("short_settle_window")

# Comfortably more than one settle window: enough for a pending evaluation to
# have fired if it was going to, or -- in the negative tests -- long enough
# that its *absence* is a real assertion, not a race against the clock.
_WAIT = SHORT_SETTLE_SECONDS + 0.2


def _counting_evaluate(monkeypatch):
    """Wrap `coordinator.evaluate` to record every call while still running the real engine."""
    calls = []
    original = real_evaluate

    def _wrapped(config, world):
        calls.append(world)
        return original(config, world)

    monkeypatch.setattr("cover_logic.coordinator.evaluate", _wrapped)
    return calls


def test_initial_evaluation_happens_at_startup(config, hass_factory, runtime_entry, monkeypatch):
    """`decision` is populated one settle window after `async_setup`, with no state change.

    Startup used to be exempt from the window and is not any more (see
    `coordinator.async_setup`), so the wait is the change here -- the property
    that matters is unchanged: the first evaluation happens on its own, without
    anything having to move.
    """
    calls = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            coordinator = CoverLogicCoordinator(hass, config, runtime_entry())
            await coordinator.async_setup()
            await asyncio.sleep(_WAIT)

            assert len(calls) == 1
            assert coordinator.decision is not None
            assert coordinator.decision.mode == "bezny_den"
            assert coordinator.last_error is None
            assert coordinator.last_success is not None

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_unrelated_entity_triggers_no_evaluation(config, hass_factory, runtime_entry, monkeypatch):
    """Only `referenced_entities(config)` is subscribed -- everything else is silent."""
    calls = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            coordinator = CoverLogicCoordinator(hass, config, runtime_entry())
            await coordinator.async_setup()
            await asyncio.sleep(_WAIT)
            calls.clear()  # drop the startup evaluation counted above

            hass.states.async_set("sensor.unrelated", "should_not_trigger_anything")
            await asyncio.sleep(_WAIT)

            assert calls == []

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_burst_of_changes_produces_one_evaluation(config, hass_factory, runtime_entry, monkeypatch):
    """A rapid burst on a referenced entity coalesces into exactly one evaluation."""
    calls = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            coordinator = CoverLogicCoordinator(hass, config, runtime_entry())
            await coordinator.async_setup()
            calls.clear()

            hass.states.async_set("input_boolean.a", "on")
            hass.states.async_set("input_boolean.a", "off")
            hass.states.async_set("input_boolean.a", "on")
            await asyncio.sleep(_WAIT)

            assert len(calls) == 1

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_evaluation_error_keeps_previous_decision_and_is_recorded(
    config, hass_factory, runtime_entry, monkeypatch, caplog
):
    """A raising `evaluate` is logged and recorded, but never blanks `decision`."""

    async def _run():
        hass = hass_factory()
        try:
            coordinator = CoverLogicCoordinator(hass, config, runtime_entry())
            await coordinator.async_setup()
            await asyncio.sleep(_WAIT)
            previous = coordinator.decision
            assert previous is not None

            def _raise(_config, _world):
                msg = "boom"
                raise EngineError(msg)

            monkeypatch.setattr("cover_logic.coordinator.evaluate", _raise)

            with caplog.at_level("ERROR", logger="cover_logic.coordinator"):
                hass.states.async_set("input_boolean.a", "on")
                await asyncio.sleep(_WAIT)

            assert coordinator.decision is previous
            assert coordinator.last_error == "EngineError: boom"
            assert any(
                record.levelname == "ERROR" and record.exc_info is not None
                for record in caplog.records
            )

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_non_engine_error_is_also_caught_and_recorded(
    config, hass_factory, runtime_entry, monkeypatch, caplog
):
    """The realistic failures are not `EngineError`.

    A typo'd condition type raises `ValueError` from the evaluator, and a broken
    user template raises out of Jinja on purpose. Catching only `EngineError`
    would let those escape into the settle timer's callback: `last_error` would
    stay unset and the sensor would keep showing a stale answer with nothing
    indicating a problem.
    """

    async def _run():
        hass = hass_factory()
        try:
            coordinator = CoverLogicCoordinator(hass, config, runtime_entry())
            await coordinator.async_setup()
            await asyncio.sleep(_WAIT)
            previous = coordinator.decision
            assert previous is not None

            def _raise(_config, _world):
                msg = "unknown condition type: 'sate'"
                raise ValueError(msg)

            monkeypatch.setattr("cover_logic.coordinator.evaluate", _raise)

            with caplog.at_level("ERROR", logger="cover_logic.coordinator"):
                hass.states.async_set("input_boolean.a", "on")
                await asyncio.sleep(_WAIT)

            assert coordinator.decision is previous
            assert coordinator.last_error == "ValueError: unknown condition type: 'sate'"

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_unload_removes_subscription_and_cancels_pending_settle(
    config, hass_factory, runtime_entry, monkeypatch
):
    """Unload drops the bus listener and a call already scheduled never fires."""
    calls = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            before = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)

            coordinator = CoverLogicCoordinator(hass, config, runtime_entry())
            await coordinator.async_setup()
            calls.clear()

            during = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)
            assert during > before

            # Start a settle window, then unload before it elapses --
            # requirement: a pending settle must not survive unload.
            hass.states.async_set("input_boolean.a", "on")
            await coordinator.async_unload()
            await asyncio.sleep(_WAIT)

            assert calls == []

            after = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)
            assert after == before

            # A further change on a referenced entity, after unload, is
            # silent too -- the subscription itself is gone, not just the
            # one pending call.
            hass.states.async_set("input_boolean.a", "off")
            await asyncio.sleep(_WAIT)
            assert calls == []
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Own-context attribution, end to end. `CommandLog.is_own`'s unit tests pin
# the bookkeeping; this pins the half only a real Home Assistant can show --
# that the context this coordinator issues is the one the service call
# actually arrives under, so a state change stamped with it is recognisably
# ours. Without this, `is_own` could be perfectly correct about ids that never
# reach Home Assistant.
# ---------------------------------------------------------------------------

# The module-wide `config` fixture's only rule is `position: keep, tilt: keep`,
# so it commands nothing and could never show a context. This one opens a shut
# blind, which is a real movement rather than a dead band.
_MOVING_CONFIG = """
blinds:
  - entity: cover.a
    travel_time: 0.2
zones:
  za:
    members: [cover.a]
modes:
  - {id: bezny_den}
rules:
  bezny_den.za:
    - {then: {position: 100, tilt: keep}}
"""


def test_the_context_a_command_is_issued_under_is_recognised_as_our_own(
    hass_factory, runtime_entry
):
    """The step manual-intervention detection is built on (spec, step 1).

    Registers a real `cover.*` handler, lets the coordinator dispatch through
    it, and asks the log about the context **the handler actually saw** -- not
    the one the coordinator says it sent.
    """
    seen = []

    async def _run():
        hass = hass_factory()
        try:

            async def _handler(call):
                seen.append(call.context)

            for service in ("open_cover", "set_cover_position", "set_cover_tilt_position"):
                hass.services.async_register("cover", service, _handler)
            hass.states.async_set(
                "cover.a", "closed", {"current_position": 0, "current_tilt_position": 0}
            )

            coordinator = CoverLogicCoordinator(
                hass, load_config(_MOVING_CONFIG), runtime_entry({"dry_run": False})
            )
            await coordinator.async_setup()
            await asyncio.sleep(_WAIT)
            await coordinator.runner.async_wait_idle()

            assert coordinator.dry_run is False
            assert seen, "the coordinator dispatched nothing, so there is nothing to attribute"
            for context in seen:
                assert coordinator.commands.is_own(context.id) is True
            # The counter: a context we did not issue must not be ours, or the
            # loop above would pass for an `is_own` that returns True for
            # anything at all.
            assert coordinator.commands.is_own("not-a-context-we-issued") is False
            # And the premise the whole design rests on: our own calls carry no
            # `user_id`, which is why "look for a user_id" happens to work
            # today and why it is the wrong thing to rely on -- see
            # `CommandLog.is_own`.
            assert all(context.user_id is None for context in seen)

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Manual-move detection, through the public path: fire a real state change and
# read the `Event` that reached the evaluation. Every rejection below is a
# measured failure mode, not a hypothetical -- 14 days of this house's logbook
# says 53 of 92 movements of the bedroom blinds carried no `user_id`, and 8 of
# the 39 that did happened during one of its own script runs. See the spec in
# the operator's config repo.
# ---------------------------------------------------------------------------

_DETECT_CONFIG = """
blinds:
  - entity: cover.a
zones:
  za:
    members: [cover.a]
modes:
  - {id: bezny_den}
rules:
  bezny_den.za:
    # Asking about manual moves is what subscribes the coordinator to the
    # blind at all -- see `coordinator._manual_move_blinds`. Without it this
    # config watches nothing and no evaluation ever happens.
    - {if: {condition: manual_move}, then: {position: keep, tilt: keep}}
    - {then: {position: keep, tilt: keep}}
manual_detection:
  ignore_while_on: [script.mover]
"""


def _moved(hass, *, context, position=100, tilt=0, entity="cover.a"):
    """One cover movement, attributed to `context` the way Home Assistant does."""
    hass.states.async_set(
        entity,
        "open" if position else "closed",
        {"current_position": position, "current_tilt_position": tilt},
        context=context,
    )


async def _event_after(hass, coordinator, worlds):
    """The `Event` the next evaluation saw, once the settle window has run."""
    await asyncio.sleep(_WAIT)
    assert worlds, "no evaluation happened, so there is no event to inspect"
    return worlds[-1].event


def _detect_case(hass_factory, runtime_entry, monkeypatch, body):
    """Shared harness: a coordinator on `_DETECT_CONFIG`, with evaluations captured."""
    worlds = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            hass.states.async_set(
                "cover.a", "closed", {"current_position": 0, "current_tilt_position": 0}
            )
            coordinator = CoverLogicCoordinator(hass, load_config(_DETECT_CONFIG), runtime_entry())
            await coordinator.async_setup()
            await asyncio.sleep(_WAIT)
            worlds.clear()
            await body(hass, coordinator, worlds)
            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_persons_move_on_our_blind_reaches_the_evaluation(
    hass_factory, runtime_entry, monkeypatch
):

    async def _body(hass, coordinator, worlds):
        _moved(hass, context=Context(user_id="u1"))
        event = await _event_after(hass, coordinator, worlds)
        assert event.kind == "manual_move"
        assert event.blind == "cover.a"
        assert event.direction == "opening"

    _detect_case(hass_factory, runtime_entry, monkeypatch, _body)


def test_a_move_with_no_user_context_is_not_a_person(hass_factory, runtime_entry, monkeypatch):
    """The measured majority: 53 of 92 movements carried no `user_id` at all.

    Accepting these is what would misread roughly four movements a day as
    somebody's hand, which is why "not our own command" cannot stand alone.
    """

    async def _body(hass, coordinator, worlds):
        _moved(hass, context=Context())
        event = await _event_after(hass, coordinator, worlds)
        assert event.kind == "state_change"
        assert event.blind is None

    _detect_case(hass_factory, runtime_entry, monkeypatch, _body)


def test_our_own_command_is_not_a_person_even_with_a_user_context(
    hass_factory, runtime_entry, monkeypatch
):
    """What `CommandLog.is_own` buys: it holds even when a user context is attached."""

    async def _body(hass, coordinator, worlds):
        ours = Context(user_id="u1")
        coordinator.commands.dispatched("open_cover", {"entity_id": "cover.a"}, context_id=ours.id)
        _moved(hass, context=ours)
        assert (await _event_after(hass, coordinator, worlds)).kind == "state_change"

        # One hop below ours is still ours -- Home Assistant chains contexts.
        worlds.clear()
        _moved(hass, context=Context(user_id="u1", parent_id=ours.id), position=50)
        assert (await _event_after(hass, coordinator, worlds)).kind == "state_change"

    _detect_case(hass_factory, runtime_entry, monkeypatch, _body)


def test_a_move_while_a_declared_mover_runs_is_not_a_person(
    hass_factory, runtime_entry, monkeypatch
):
    """8 of the 39 user-context movements happened during a script run.

    A Home Assistant script inherits the caller's context, `user_id` and all,
    so without this a script a person started is indistinguishable from that
    person still pressing buttons. The second half is the counter: with the
    mover off, the very same movement *is* detected.
    """

    async def _body(hass, coordinator, worlds):
        hass.states.async_set("script.mover", "on")
        _moved(hass, context=Context(user_id="u1"))
        assert (await _event_after(hass, coordinator, worlds)).kind == "state_change"

        hass.states.async_set("script.mover", "off")
        worlds.clear()
        _moved(hass, context=Context(user_id="u1"), position=50)
        assert (await _event_after(hass, coordinator, worlds)).kind == "manual_move"

    _detect_case(hass_factory, runtime_entry, monkeypatch, _body)


def test_a_tilt_only_move_is_still_a_movement(hass_factory, runtime_entry, monkeypatch):
    """A blind already at the top can only be moved by its slats."""

    async def _body(hass, coordinator, worlds):
        _moved(hass, context=Context(user_id="u1"), position=100, tilt=0)
        worlds.clear()
        _moved(hass, context=Context(user_id="u1"), position=100, tilt=100)
        event = await _event_after(hass, coordinator, worlds)
        assert event.kind == "manual_move"
        assert event.direction == "opening"

    _detect_case(hass_factory, runtime_entry, monkeypatch, _body)


def test_the_event_does_not_survive_into_the_next_evaluation(
    hass_factory, runtime_entry, monkeypatch
):
    """An event describes one moment; reporting it twice would be a second movement."""

    async def _body(hass, coordinator, worlds):
        _moved(hass, context=Context(user_id="u1"))
        assert (await _event_after(hass, coordinator, worlds)).kind == "manual_move"

        worlds.clear()
        hass.states.async_set("script.mover", "off")  # any unrelated change
        assert (await _event_after(hass, coordinator, worlds)).kind == "state_change"

    _detect_case(hass_factory, runtime_entry, monkeypatch, _body)


def test_blinds_are_only_watched_when_the_config_asks_about_manual_moves(config):
    """The subscription follows the question -- see `_manual_move_blinds`.

    Subscribing to every cover unconditionally would make the layer that
    decides listen to the layer that moves, and a 55-second travel would feed
    back as recomputes for its whole duration. So a config that never mentions
    `manual_move` must watch no blind at all, and this house's own does not
    mention it today.
    """
    assert _manual_move_blinds(config) == set()
    assert _manual_move_blinds(load_config(_DETECT_CONFIG)) == {"cover.a"}
