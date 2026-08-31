"""What the executor did, or deliberately did not do, most recently.

The dry-run day's instrument. `runner.py` already writes one greppable line per
command -- deliberately in the *same shape* whether it is dry-running or live,
because comparing the two against `/api/logbook` is the whole point -- but a log
line is only visible to someone tailing the log at the right moment. This module
keeps the last few of those same lines in memory so `sensor.cover_logic_mode`
can show them, and so "what did it last do, and why" is answerable from the
entity a person is already looking at.

Five kinds of entry, and the distinction between the first three is the single
most valuable thing a dry-run day produces:

- `would_call` -- the runner reached a command and dry run stopped it.
- `called` -- the runner reached a command and issued it (live).
- `suppressed` -- an axis was *given a target* and produced no command anyway
  (the dead band, or a blind with no slats). This is the difference between
  "the runner stood still" and "the runner was never asked", which is exactly
  what nobody could reconstruct on 2026-08-27.
- `withheld` -- a guard suppressed the whole action before the runner ever saw
  it. Recorded by the coordinator, not the runner, because the runner never
  learns about a decision that was taken away from it.
- `dispatched` -- the command reached `hass.services.async_call` and returned
  without raising. Written *after* the call, by the coordinator's own service
  caller, which is what makes it the difference between "issued" and "issued
  and accepted": the runner's `called` line is written before the attempt.

**This module has no clock and no Home Assistant import.** The timestamp is
supplied by an injected `clock`, so a test states the time instead of racing
it, and so nothing here has to decide between naive local time and UTC -- a
decision this project has already gotten wrong once elsewhere and keeps in one
place (`ha_world.build_world`). It is in `tests/test_purity.py`'s
`PURE_MODULES`: it is a value container, and if it ever needs `hass` that is a
design change that should have to argue with a failing test first.

Values are flattened to strings/numbers on the way in. A Home Assistant state
attribute must survive JSON serialisation, and a `dict` holding a `Command`
dataclass does not -- so the flattening happens once, here, rather than being
something every reader has to remember.
"""

from collections import deque
from collections.abc import Callable, Mapping
import datetime as dt
from typing import Any

from .const import COMMAND_DISPATCHED, COMMAND_WITHHELD

# How many entries are kept. Small on purpose: the sensor shows the newest one,
# and the log is the archive. A deep in-memory ring here would be a second,
# worse copy of something Home Assistant already stores properly.
DEFAULT_DEPTH = 25


def _utc_now_iso() -> str:
    """The default clock: an ISO-8601 UTC timestamp, seconds resolution.

    UTC rather than local time because this is a machine record read next to
    `last_success` (which `coordinator.py` also keeps in UTC), not a
    house-facing wall clock -- `World.now` is the one place naive local time is
    deliberate, and it is deliberate there for a parity reason that has nothing
    to do with logging.
    """
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _plain(value: Any) -> Any:
    """`value` as something a Home Assistant state attribute can carry.

    `str`, `int`, `float`, `bool` and `None` pass through; everything else --
    a `Command` dataclass, a `Plan`, an `Action` -- becomes its `str()`. The
    conversion is deliberately lossy and deliberately here: an attribute that
    cannot be serialised takes the *whole* entity down at write time, which is
    a spectacular way to lose a diagnostic entity.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


class CommandLog:
    """A bounded ring of the executor's most recent actions and non-actions.

    Three doors in, and all three only report: `observe` (from
    `runner._log_fields`), `withheld` (from the coordinator's guard handling)
    and `dispatched` (from the coordinator's service caller, once a call has
    actually gone out). This class issues nothing and has no Home Assistant
    import at all -- it is the record of the hands, never the hands.
    """

    def __init__(
        self, *, depth: int = DEFAULT_DEPTH, clock: Callable[[], str] | None = None
    ) -> None:
        """Keep the newest `depth` entries; timestamp them with `clock`."""
        self._entries: deque[dict[str, Any]] = deque(maxlen=depth)
        self._clock = clock if clock is not None else _utc_now_iso

    def dispatched(self, service: str, data: Mapping[str, Any]) -> None:
        """Record one `cover.*` call that reached Home Assistant and returned.

        Called after the service call, never before: a `dispatched` entry means
        accepted, where the runner's own `called` line means only attempted.
        """
        entity = data.get("entity_id")
        payload = {key: value for key, value in data.items() if key != "entity_id"}
        self._record(
            COMMAND_DISPATCHED,
            {
                "blind": entity,
                "service": f"cover.{service}",
                **payload,
                # Stated, not implied: this is where a reader sees a command leave the integration.
                "reached_home_assistant": True,
            },
        )

    def observe(self, kind: str, fields: Mapping[str, Any]) -> None:
        """Record one line the runner just wrote, under its own `kind`.

        The `kind` is passed rather than sniffed out of `fields`: "which of the
        three states is this" is known for certain at the one place that emits
        the line, and re-deriving it here from the presence of a key would be a
        second implementation of the same fact, free to drift.
        """
        self._record(kind, fields)

    def withheld(
        self,
        entity: str,
        reason: str,
        *,
        policy: str | None = None,
        guard: int | None = None,
    ) -> None:
        """Record that a guard took a decision away before the runner saw it."""
        self._record(
            COMMAND_WITHHELD,
            {"blind": entity, "reason": reason, "policy": policy, "guard": guard},
        )

    def _record(self, kind: str, fields: Mapping[str, Any]) -> None:
        entry: dict[str, Any] = {"kind": kind, "at": self._clock()}
        entry.update({key: _plain(value) for key, value in fields.items()})
        self._entries.append(entry)

    @property
    def last(self) -> dict[str, Any] | None:
        """The newest entry, or `None` if the executor has done nothing yet.

        A copy, not the stored dict: a state attribute handed out by reference
        is one an entity platform can mutate on its way through, and the ring
        would then be reporting something that never happened.
        """
        if not self._entries:
            return None
        return dict(self._entries[-1])

    @property
    def recent(self) -> tuple[dict[str, Any], ...]:
        """Every kept entry, newest first. Copies, for the same reason as `last`."""
        return tuple(dict(entry) for entry in reversed(self._entries))
