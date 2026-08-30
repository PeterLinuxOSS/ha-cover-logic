"""The whole path, end to end: world -> guards -> engine -> guards -> runner.

Every other test in this repository exercises one layer. This one drives the
real `CoverLogicCoordinator` against a real (minimal) `HomeAssistant` and asks
what actually came out of the executor -- because a suite that only tests the
layers can be entirely green while the wire between two of them is missing,
which is the failure this project has shipped three times.

Two things are asserted here that are asserted nowhere else:

- **Nothing reaches Home Assistant, even with `dry_run` off.** The runner's
  service caller is bound to `command_log.CommandLog`, an object with no
  `homeassistant` import at all, so the last wire is unconnected as a fact
  about the object graph rather than as a switch. `test_no_service_call_...`
  proves it by making `hass.services.async_call` record and finding nothing.
  **That test dies with `tests/test_no_movement.py` in task 5**, in the same
  commit -- it is the same promise, checked from the other end.
- **A deferral survives a restart.** Not by being persisted, but by being a
  derived fact plus a timer: `test_a_restart_re_arms_a_pending_deferral` tears
  the coordinator down mid-wait, builds a fresh one, and lets the deadline pass
  without touching a single watched entity.

Uses `hass_factory` (a real, minimal `HomeAssistant`) for the reason
`test_coordinator.py` gives: `async_track_state_change_event`, `Debouncer` and
`async_call_later` all need genuine bus dispatch and genuine loop timers.
"""

import asyncio

import pytest

pytest.importorskip("homeassistant")

from cover_logic.config_schema import load_config
from cover_logic.const import (
    COMMAND_DISPATCHED,
    COMMAND_SUPPRESSED,
    COMMAND_WITHHELD,
    COMMAND_WOULD_CALL,
    OPT_DRY_RUN,
)
from cover_logic.coordinator import (
    DEBOUNCE_COOLDOWN,
    SOURCE_GUARD_TIMEOUT,
    CoverLogicCoordinator,
    _entity_ids,
)
from cover_logic.guards import GuardError
from cover_logic.model import KEEP, Action
from cover_logic.sensor import CoverLogicModeSensor
from cover_logic.validation import ERROR, validate

_WAIT = DEBOUNCE_COOLDOWN + 0.3

BLIND = "cover.a"
OTHER = "cover.b"

# One blind per zone so a guard can be aimed at exactly one of them, and
# `travel_time` short enough that an arrival wait is 0.3s rather than 90s --
# that is the blind's own configuration, not a runner constant.
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
  dvere:
    condition: state
    entity_id: binary_sensor.dvere
    state: "on"
  zavri:
    condition: state
    entity_id: input_boolean.zavri
    state: "on"
modes:
  - id: bezny
rules:
  bezny.za:
    - if: !ref zavri
      then: {position: 0, tilt: 0}
    - then: {position: keep, tilt: keep}
  bezny.zb:
    - then: {position: keep, tilt: keep}
"""

SKIP_GUARD = """
guards:
  - name: door open
    policy: skip
    applies_to: closing
    targets: [za]
    when: !ref dvere
"""

FORCE_GUARD = """
guards:
  - name: wind
    policy: force
    applies_to: any
    targets: [za]
    when: !ref dvere
    then: {position: 100, tilt: 100}
"""

# One second, so a real timer can be waited out in a test. `recheck_every` is
# deliberately *longer* than `max_wait`: `next_recheck` must shorten itself to
# the deadline, or this deferral would time out five seconds late.
DEFER_GUARD = """
guards:
  - name: door open
    policy: defer
    applies_to: closing
    targets: [za]
    when: !ref dvere
    max_wait: 1
    on_timeout: proceed
    recheck_every: 5
"""

DIRECTIONAL_GUARD = """
guards:
  - name: door open
    policy: skip
    applies_to: closing
    targets: [za]
    when: !ref dvere
"""

ANY_GUARD = """
guards:
  - name: door open
    policy: skip
    applies_to: any
    targets: [za]
    when: !ref dvere
"""


def config(extra=""):
    """Parse `BASE + extra`, refusing anything this project would not accept."""
    parsed = load_config(BASE + extra)
    assert [p for p in validate(parsed) if p.severity == ERROR] == []
    return parsed


@pytest.fixture(autouse=True)
def _short_settle(monkeypatch):
    """Shrink the post-arrival settle from 2s to 10ms, as `tests/ha/test_runner.py` does.

    `planner.SETTLE_SECONDS` is a fact about these motors, not about the
    wiring: the path behaves identically either way, it just would not be
    worth two seconds per sequence to watch.
    """
    monkeypatch.setattr("cover_logic.planner.SETTLE_SECONDS", 0.01)


def _seed(hass, *, door="off", zavri="off", position=100):
    """The world every test starts from: doors shut, both blinds up and readable."""
    hass.states.async_set("binary_sensor.dvere", door)
    hass.states.async_set("input_boolean.zavri", zavri)
    for entity in (BLIND, OTHER):
        hass.states.async_set(
            entity, "open", {"current_position": position, "current_tilt_position": position}
        )


async def _settle(coordinator):
    """Let the debounce, the evaluation and every queued sequence finish."""
    await asyncio.sleep(_WAIT)
    await coordinator.runner.async_wait_idle()


def _spy_on_apply(coordinator):
    """Record every `(entity, priority, source)` the coordinator hands to the runner.

    The queue itself cannot answer this: a `Plan` with no commands finishes in
    the same event-loop turn it starts, logs nothing and leaves no trace in
    `in_flight`, so "was this blind queued at all" is invisible downstream. The
    handover is the layer under test, so it is the layer watched.
    """
    asked = []
    original = coordinator.runner.async_apply

    async def _record(blind, action, *, priority, source, mode=""):
        asked.append((blind.entity, priority.name, source))
        await original(blind, action, priority=priority, source=source, mode=mode)

    coordinator.runner.async_apply = _record
    return asked


def _kinds(coordinator, kind):
    """Every entry of one kind, **oldest first**.

    `CommandLog.recent` is newest-first because the sensor wants the newest
    one; a test reads better in the order things happened.
    """
    return [entry for entry in reversed(coordinator.commands.recent) if entry["kind"] == kind]


def _services(coordinator, kind=COMMAND_WOULD_CALL):
    """Just the `cover.*` calls, dropping the waits and settles that call nothing.

    Every command is logged, arrival waits included, and those carry
    `would_call=none`. Filtering here rather than in each test keeps "which
    services would go out" a set of services.
    """
    key = "would_call" if kind == COMMAND_WOULD_CALL else "called"
    return [entry for entry in _kinds(coordinator, kind) if entry[key] != "none"]


# ---------------------------------------------------------------------------
# The wire that is deliberately left unconnected.
# ---------------------------------------------------------------------------


def test_no_service_call_reaches_home_assistant_even_with_dry_run_off(
    hass_factory, runtime_entry, monkeypatch
):
    """The last wire is an object, not a switch. DELETED WITH `test_no_movement.py`.

    `dry_run` is switched **off** here on purpose: with it on, the runner never
    reaches its service caller at all and this test would pass without proving
    anything. Off, every command really is dispatched -- and lands in the
    `CommandLog`, which is where `cover_logic` currently ends.
    """

    async def _run():
        hass = hass_factory()
        try:
            # Patched on the class: `ServiceRegistry` has `__slots__`, so the
            # instance attribute cannot be replaced -- and the class is what
            # every spelling of the call (`hass.services.async_call`,
            # `self.hass.services.async_call`, an aliased receiver) goes
            # through anyway.
            called = []

            async def _record(_self, *args, **kwargs):
                called.append((args, kwargs))

            monkeypatch.setattr("homeassistant.core.ServiceRegistry.async_call", _record)

            _seed(hass, zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry({OPT_DRY_RUN: False}))
            await coordinator.async_setup()
            await _settle(coordinator)

            # Counter: the executor really did get all the way to dispatching.
            dispatched = _kinds(coordinator, COMMAND_DISPATCHED)
            assert dispatched, coordinator.commands.recent
            assert dispatched[0]["service"].startswith("cover.")
            assert dispatched[0]["reached_home_assistant"] is False

            assert called == []

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# coordinator -> runner.
# ---------------------------------------------------------------------------


def test_a_decided_action_reaches_the_runner(hass_factory, runtime_entry):
    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()
            await _settle(coordinator)

            would = _services(coordinator)
            assert [entry["blind"] for entry in would] == [BLIND, BLIND]
            assert [entry["would_call"] for entry in would] == [
                "cover.close_cover",
                "cover.close_cover_tilt",
            ]
            # `src`/`prio` are what answer "why did it move".
            assert {entry["src"] for entry in would} == {"coordinator"}
            assert {entry["prio"] for entry in would} == {"SCHEDULED"}
            assert {entry["mode"] for entry in would} == {"bezny"}

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_an_action_that_asks_for_nothing_is_never_queued(hass_factory, runtime_entry):
    """`Action(KEEP, KEEP)` is a complete decision with nothing to execute.

    Queueing it is not free: a blind's waiting slot holds exactly one request,
    so a no-op arriving mid-movement would evict a real one.

    Watched at the handover rather than at the log, because a no-op *is*
    invisible downstream -- it plans to an empty sequence that finishes in the
    same turn it starts. An implementation that queues no-ops would show two
    blinds here and log nothing either way.
    """

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass)
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            recorded = _spy_on_apply(coordinator)
            await coordinator.async_setup()
            await _settle(coordinator)

            # Counter: the engine really did decide both blinds, and decided
            # `keep` for both -- this is not an empty decision.
            assert set(coordinator.decision.targets) == {BLIND, OTHER}
            assert coordinator.decision.targets[BLIND] == Action(KEEP, KEEP)
            assert coordinator.decision.targets[OTHER] == Action(KEEP, KEEP)
            assert recorded == []

            # ...and the same coordinator does hand `cover.a` over the moment
            # its rule asks for something, so this is not a spy that never fires.
            hass.states.async_set("input_boolean.zavri", "on")
            await _settle(coordinator)
            assert [entity for entity, _prio, _src in recorded] == [BLIND]

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_skip_guard_withholds_the_action_and_names_itself(hass_factory, runtime_entry):
    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(SKIP_GUARD), runtime_entry())
            recorded = _spy_on_apply(coordinator)
            await coordinator.async_setup()
            await _settle(coordinator)

            assert coordinator.decision.targets[BLIND] == Action(0, 0)
            assert recorded == []
            assert _kinds(coordinator, COMMAND_WOULD_CALL) == []

            withheld = _kinds(coordinator, COMMAND_WITHHELD)
            assert [entry["blind"] for entry in withheld] == [BLIND]
            assert withheld[0]["policy"] == "skip"
            assert withheld[0]["guard"] == 0
            assert "door open" in withheld[0]["reason"]

            # Counter, in this same test: shut the door and the identical
            # decision goes straight through. Without it, a coordinator that
            # dispatched nothing at all would satisfy every line above.
            hass.states.async_set("binary_sensor.dvere", "off")
            await _settle(coordinator)
            assert [entity for entity, _prio, _src in recorded] == [BLIND]
            assert _kinds(coordinator, COMMAND_WOULD_CALL)

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_force_guard_replaces_the_engines_action_on_the_way_to_the_runner(
    hass_factory, runtime_entry
):
    """The counter to "guards are computed and then ignored"."""

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on", position=50)
            coordinator = CoverLogicCoordinator(hass, config(FORCE_GUARD), runtime_entry())
            await coordinator.async_setup()
            await _settle(coordinator)

            assert coordinator.decision.targets[BLIND] == Action(0, 0)
            services = [entry["would_call"] for entry in _services(coordinator)]
            assert services == ["cover.open_cover", "cover.open_cover_tilt"]

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_standing_suppression_is_recorded_once_not_once_per_recompute(
    hass_factory, runtime_entry
):
    """A guard that stays true for hours must not turn `last_command` into a clock."""

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(SKIP_GUARD), runtime_entry())
            await coordinator.async_setup()
            await _settle(coordinator)
            assert len(_kinds(coordinator, COMMAND_WITHHELD)) == 1

            for _ in range(3):
                hass.states.async_set("input_boolean.zavri", "on", force_update=True)
                await _settle(coordinator)

            # Counter: those recomputes really happened.
            assert coordinator.last_success is not None
            assert len(_kinds(coordinator, COMMAND_WITHHELD)) == 1

            # ...and the *next* change of reason is recorded again.
            hass.states.async_set("binary_sensor.dvere", "off")
            await _settle(coordinator)
            hass.states.async_set("binary_sensor.dvere", "on")
            await _settle(coordinator)
            assert len(_kinds(coordinator, COMMAND_WITHHELD)) == 2

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_guard_that_cannot_be_honoured_dispatches_nothing(
    hass_factory, runtime_entry, monkeypatch, caplog
):
    """An unusable interlock means move nothing -- never fall back to the raw decision.

    Falling back is precisely the movement the guard existed to stop.
    """

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()
            await _settle(coordinator)
            assert _kinds(coordinator, COMMAND_WOULD_CALL)  # counter: it does move normally
            previous = coordinator.decision

            def _raise(_config, _world):
                msg = "guard #0: unknown policy 'skipp'"
                raise GuardError(msg)

            monkeypatch.setattr("cover_logic.coordinator.screen", _raise)
            before = len(coordinator.commands.recent)

            with caplog.at_level("ERROR", logger="cover_logic.coordinator"):
                hass.states.async_set("input_boolean.zavri", "off")
                await _settle(coordinator)

            assert coordinator.decision is previous
            assert coordinator.last_error.startswith("GuardError:")
            assert len(coordinator.commands.recent) == before

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_an_unreadable_cover_still_gets_its_position_asked_for(hass_factory, runtime_entry):
    """`review` must be handed a position for every blind, `None` included.

    A directional guard silenced by a dead sensor is the failure the schema
    exists to prevent, so "unavailable" has to arrive as `None` rather than as
    a missing key that a caller forgot.
    """

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, zavri="on")
            hass.states.async_set(BLIND, "unavailable")
            coordinator = CoverLogicCoordinator(hass, config(), runtime_entry())
            await coordinator.async_setup()

            positions = coordinator._positions()  # noqa: SLF001
            assert set(positions) == {BLIND, OTHER}
            assert positions[BLIND] is None
            assert positions[OTHER] == 100

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Deferrals: the clock, the timer, and the restart.
# ---------------------------------------------------------------------------


def test_a_deferred_blind_waits_and_says_which_guard_is_holding_it(hass_factory, runtime_entry):
    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(DEFER_GUARD), runtime_entry())
            recorded = _spy_on_apply(coordinator)
            await coordinator.async_setup()
            await asyncio.sleep(0)

            assert coordinator.decision.targets[BLIND] == Action(0, 0)  # counter
            assert recorded == []
            assert _kinds(coordinator, COMMAND_WOULD_CALL) == []
            deferred = coordinator.pending["deferred"]
            assert set(deferred) == {BLIND}
            assert deferred[BLIND]["name"] == "door open"
            assert deferred[BLIND]["state"] == "waiting"
            assert deferred[BLIND]["on_timeout"] == "proceed"

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_released_guard_lets_the_action_through(hass_factory, runtime_entry):
    """The ordinary way a wait ends: the condition clears and the blind moves."""

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(DEFER_GUARD), runtime_entry())
            await coordinator.async_setup()
            assert coordinator.pending["deferred"]  # counter: it really waited

            hass.states.async_set("binary_sensor.dvere", "off")
            await _settle(coordinator)

            assert coordinator.pending["deferred"] == {}
            assert {entry["src"] for entry in _kinds(coordinator, COMMAND_WOULD_CALL)} == {
                "coordinator"
            }

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_the_recheck_timer_times_a_wait_out_with_nothing_else_happening(
    hass_factory, runtime_entry
):
    """No state changes at all after setup: only `recheck_every` can wake this.

    This is the whole reason `recheck_every` exists. Remove the timer and the
    deferral waits for an unrelated event that may never come -- which is what
    a `wait_for_trigger` does after a restart, in the one direction that has
    already cost this house a night.
    """

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(DEFER_GUARD), runtime_entry())
            await coordinator.async_setup()
            assert _kinds(coordinator, COMMAND_WOULD_CALL) == []  # counter: held back

            await asyncio.sleep(1.0 + _WAIT)
            await coordinator.runner.async_wait_idle()

            would = _kinds(coordinator, COMMAND_WOULD_CALL)
            assert would, coordinator.commands.recent
            assert {entry["src"] for entry in would} == {SOURCE_GUARD_TIMEOUT}
            # A guard's own deadline outranks a routine recompute, and a person.
            assert {entry["prio"] for entry in would} == {"GUARD"}
            assert coordinator.pending["deferred"][BLIND]["state"] == "proceed"

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_an_abandoning_guard_times_out_without_moving_anything(hass_factory, runtime_entry):
    """The counter to a timeout hard-coded to `proceed`: same wait, opposite answer."""

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            abandoning = config(DEFER_GUARD.replace("on_timeout: proceed", "on_timeout: abandon"))
            coordinator = CoverLogicCoordinator(hass, abandoning, runtime_entry())
            await coordinator.async_setup()

            await asyncio.sleep(1.0 + _WAIT)
            await coordinator.runner.async_wait_idle()

            assert _kinds(coordinator, COMMAND_WOULD_CALL) == []
            assert _kinds(coordinator, COMMAND_SUPPRESSED) == []
            assert coordinator.pending["deferred"][BLIND]["state"] == "abandon"

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_a_restart_re_arms_a_pending_deferral(hass_factory, runtime_entry):
    """Tear the coordinator down mid-wait and build a fresh one: the wait comes back.

    Nothing is carried across -- a brand-new `DeferralRegistry`, a brand-new
    runner, a brand-new timer. The wait survives because it is *re-derived*
    from the configuration and the world, which is the one structural
    difference between this and a `wait_for_trigger`. After the restart no
    watched entity changes at all, so only the re-armed `recheck_every` timer
    can produce the movement below.
    """

    async def _run():
        hass = hass_factory()
        entry = runtime_entry()
        try:
            _seed(hass, door="on", zavri="on")

            before = CoverLogicCoordinator(hass, config(DEFER_GUARD), entry)
            await before.async_setup()
            assert BLIND in before.pending["deferred"]  # counter: it was waiting
            await before.async_unload()

            after = CoverLogicCoordinator(hass, config(DEFER_GUARD), entry)
            await after.async_setup()
            # Nothing has been issued by the new coordinator: the blind is
            # held back again, by the same guard, on a brand-new clock.
            assert _kinds(after, COMMAND_WOULD_CALL) == []
            assert BLIND in after.pending["deferred"]

            await asyncio.sleep(1.0 + _WAIT)
            await after.runner.async_wait_idle()

            would = _kinds(after, COMMAND_WOULD_CALL)
            assert would, after.commands.recent
            assert {entry_["src"] for entry_ in would} == {SOURCE_GUARD_TIMEOUT}

            await after.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_unload_disarms_the_recheck_timer(hass_factory, runtime_entry):
    """A timer that outlives its coordinator evaluates against a dead config entry."""

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(DEFER_GUARD), runtime_entry())
            await coordinator.async_setup()
            assert coordinator._unsub_recheck is not None  # noqa: SLF001  (counter)

            await coordinator.async_unload()
            await asyncio.sleep(1.0 + _WAIT)

            assert _kinds(coordinator, COMMAND_WOULD_CALL) == []
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# What the coordinator subscribes to.
# ---------------------------------------------------------------------------


def test_a_directional_guard_makes_its_blinds_watched():
    """A guard judged against a position must recompute when that position moves.

    `referenced_entities` cannot see this: it enumerates what *conditions*
    read, and a directional guard reads a position no condition mentions. On
    this house's own fixture it yields no `cover.*` entry at all.
    """
    assert not [entity for entity in _entity_ids(config()) if entity.startswith("cover.")]
    watched = _entity_ids(config(DIRECTIONAL_GUARD))
    assert BLIND in watched
    # Narrow, not "every cover": only blinds the guard could actually judge.
    assert OTHER not in watched


def test_a_non_directional_guard_adds_no_cover_subscription():
    """`applies_to: any` never asks where the blind is, so nothing to watch.

    The counter to "subscribe to every blind a guard targets", which would put
    the layer that decides on the output of the layer that moves.
    """
    watched = _entity_ids(config(ANY_GUARD))
    assert not [entity for entity in watched if entity.startswith("cover.")]


def test_a_cover_moving_by_itself_recomputes_when_a_directional_guard_watches_it(
    hass_factory, runtime_entry
):
    """End to end: the subscription is real, not just a set the function returns."""

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(DIRECTIONAL_GUARD), runtime_entry())
            await coordinator.async_setup()
            await _settle(coordinator)
            first = coordinator.last_success

            hass.states.async_set(
                BLIND, "open", {"current_position": 40, "current_tilt_position": 40}
            )
            await _settle(coordinator)

            assert coordinator.last_success > first

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The sensor, over the real coordinator.
# ---------------------------------------------------------------------------


def test_the_sensor_shows_the_executors_state_and_keeps_matica_diff(hass_factory, runtime_entry):
    """`dry_run`, `pending` and `last_command` are filled by the real path.

    `tests/ha/test_sensor.py` drives the sensor off a fake coordinator, so it
    would stay green if the real one never filled these in. This is the test
    that would not.
    """

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass, door="on", zavri="on")
            coordinator = CoverLogicCoordinator(hass, config(DEFER_GUARD), runtime_entry())
            await coordinator.async_setup()
            await _settle(coordinator)

            sensor = CoverLogicModeSensor(coordinator, "entry1")
            sensor.hass = hass
            attributes = sensor.extra_state_attributes

            assert attributes["dry_run"] is True
            assert set(attributes["pending"]) == {"deferred", "queued"}
            assert attributes["pending"]["deferred"][BLIND]["name"] == "door open"
            assert attributes["last_command"]["kind"] == COMMAND_WITHHELD
            # The old matrix is not running here, so "not checked" -- and it is
            # still reported, which is the point: `matica_diff` stays.
            assert "matica_diff" in attributes
            assert attributes["matica_diff"] is None

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())


def test_dry_run_is_read_live_off_the_entry(hass_factory, runtime_entry):
    """Flipping the option must show up without a reload -- that is why it is an option."""

    async def _run():
        hass = hass_factory()
        try:
            _seed(hass)
            entry = runtime_entry()
            coordinator = CoverLogicCoordinator(hass, config(), entry)
            await coordinator.async_setup()
            assert coordinator.dry_run is True

            entry.options[OPT_DRY_RUN] = False
            assert coordinator.dry_run is False

            await coordinator.async_unload()
        finally:
            await hass.async_stop(force=True)

    asyncio.run(_run())
