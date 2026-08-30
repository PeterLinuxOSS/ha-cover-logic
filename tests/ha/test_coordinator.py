"""Tests for `coordinator.CoverLogicCoordinator`.

Imports Home Assistant, so this module only collects under the Python 3.14
venv (`.venv/bin/python -m pytest`) -- see `test_ha_world.py`'s own note.

Uses `hass_factory` (a real, minimal `HomeAssistant`) rather than `FakeHass`:
`async_track_state_change_event` and `Debouncer` both need genuine bus
dispatch and a genuine event loop timer, not a re-implementation of either --
see `hass_factory`'s own docstring in `tests/ha/conftest.py` for the full
reasoning.
"""

import asyncio

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import EVENT_STATE_CHANGED

from cover_logic.coordinator import (
    DEBOUNCE_COOLDOWN,
    CoverLogicCoordinator,
    evaluate as real_evaluate,
)
from cover_logic.engine import EngineError

# Comfortably more than one debounce window: enough for a pending call to
# have fired if it was going to, or -- in the negative tests -- long enough
# that its *absence* is a real assertion, not a race against the clock.
_WAIT = DEBOUNCE_COOLDOWN + 0.2


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
    """`decision` is populated by `async_setup` itself, before any state change."""
    calls = _counting_evaluate(monkeypatch)

    async def _run():
        hass = hass_factory()
        try:
            coordinator = CoverLogicCoordinator(hass, config, runtime_entry())
            await coordinator.async_setup()

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
    would let those escape into the debouncer's callback: `last_error` would
    stay unset and the sensor would keep showing a stale answer with nothing
    indicating a problem.
    """

    async def _run():
        hass = hass_factory()
        try:
            coordinator = CoverLogicCoordinator(hass, config, runtime_entry())
            await coordinator.async_setup()
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


def test_unload_removes_subscription_and_cancels_pending_debounce(
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

            # Schedule a debounced call, then unload before its cooldown
            # elapses -- requirement: a pending debounce must not survive
            # unload.
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
