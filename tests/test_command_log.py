"""`CommandLog`: the executor's five states, and the one it can never reach.

The interesting assertion in this file is the negative one. `CommandLog` is
what `coordinator.py` binds `CoverRunner`'s service caller to, so "the whole
path is wired and nothing moves" rests on this class being unable to issue a
service call -- not on a switch, not on a default, and not on anyone's care.
`test_this_module_cannot_reach_home_assistant` is that guarantee, checked
against the source rather than against the author's intention.
"""

import ast
import asyncio
import inspect
from pathlib import Path

from cover_logic import command_log
from cover_logic.command_log import CommandLog
from cover_logic.const import (
    COMMAND_CALLED,
    COMMAND_DISPATCHED,
    COMMAND_SUPPRESSED,
    COMMAND_WITHHELD,
    COMMAND_WOULD_CALL,
)
from cover_logic.planner import SetPosition


def _log(**kwargs):
    """A log with a stated clock, so a timestamp is an assertion and not a race."""
    ticks = iter(range(1, 10_000))
    return CommandLog(clock=lambda: f"t{next(ticks)}", **kwargs)


# ---------------------------------------------------------------------------
# The negative guarantee.
# ---------------------------------------------------------------------------


def test_this_module_cannot_reach_home_assistant():
    """No `homeassistant` import, deferred or otherwise, anywhere in the module.

    `tests/test_purity.py` makes the same check from its own list, which is
    what keeps it true as the file changes; this one states *why* it matters
    for this particular file, next to the tests that depend on it.
    """
    source = Path(inspect.getsourcefile(command_log)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported  # counter: the walk really found imports to check
    assert not [name for name in imported if name.startswith("homeassistant")]


def test_calling_it_records_instead_of_issuing():
    """The `CoverCall` seam: awaitable, returns `None`, and reaches nothing."""
    log = _log()
    asyncio.run(log("set_cover_position", {"entity_id": "cover.a", "position": 34}))

    assert log.last == {
        "kind": COMMAND_DISPATCHED,
        "at": "t1",
        "blind": "cover.a",
        "service": "cover.set_cover_position",
        "position": 34,
        "reached_home_assistant": False,
    }


# ---------------------------------------------------------------------------
# The reporting doors.
# ---------------------------------------------------------------------------


def test_observed_lines_keep_their_kind_and_fields():
    log = _log()
    log.observe(COMMAND_WOULD_CALL, {"blind": "cover.a", "step": "1/2", "cmd": "SetPosition(0)"})
    assert log.last == {
        "kind": COMMAND_WOULD_CALL,
        "at": "t1",
        "blind": "cover.a",
        "step": "1/2",
        "cmd": "SetPosition(0)",
    }


def test_the_kind_is_whatever_the_caller_said_not_something_inferred():
    """The counter to a `kind` sniffed out of the fields: same fields, four kinds."""
    log = _log()
    fields = {"blind": "cover.a"}
    for kind in (COMMAND_WOULD_CALL, COMMAND_CALLED, COMMAND_SUPPRESSED, COMMAND_WITHHELD):
        log.observe(kind, fields)
    assert [entry["kind"] for entry in log.recent] == [
        COMMAND_WITHHELD,
        COMMAND_SUPPRESSED,
        COMMAND_CALLED,
        COMMAND_WOULD_CALL,
    ]


def test_withheld_names_the_guard_and_its_policy():
    log = _log()
    log.withheld("cover.a", "guard #2 'sauna': skip", policy="skip", guard=2)
    assert log.last == {
        "kind": COMMAND_WITHHELD,
        "at": "t1",
        "blind": "cover.a",
        "reason": "guard #2 'sauna': skip",
        "policy": "skip",
        "guard": 2,
    }


# ---------------------------------------------------------------------------
# The ring, and what comes out of it.
# ---------------------------------------------------------------------------


def test_nothing_recorded_yet_is_none_not_an_empty_dict():
    """`None` is "the executor has done nothing"; `{}` would read as an event."""
    assert _log().last is None
    assert _log().recent == ()


def test_only_the_newest_entries_are_kept_and_they_come_out_newest_first():
    log = _log(depth=3)
    for index in range(5):
        log.observe(COMMAND_WOULD_CALL, {"blind": f"cover.{index}"})
    assert [entry["blind"] for entry in log.recent] == ["cover.4", "cover.3", "cover.2"]
    assert log.last["blind"] == "cover.4"


def test_handing_out_an_entry_hands_out_a_copy():
    """A state attribute passed by reference is one a platform can mutate."""
    log = _log()
    log.observe(COMMAND_WOULD_CALL, {"blind": "cover.a"})
    handed = log.last
    handed["blind"] = "cover.tampered"
    assert log.last["blind"] == "cover.a"


def test_values_are_flattened_to_something_serialisable():
    """A dataclass in a state attribute takes the whole entity down at write time."""
    log = _log()
    log.observe(COMMAND_WOULD_CALL, {"cmd": SetPosition(entity="cover.a", position=0)})
    assert isinstance(log.last["cmd"], str)
    assert "SetPosition" in log.last["cmd"]


def test_plain_values_pass_through_untouched():
    """The counter to `_plain` stringifying everything: numbers stay numbers."""
    log = _log()
    log.observe(COMMAND_WOULD_CALL, {"target": 34, "ratio": 0.5, "on": True, "why": None})
    entry = log.last
    assert entry["target"] == 34
    assert entry["ratio"] == 0.5
    assert entry["on"] is True
    assert entry["why"] is None


def test_the_source_mapping_is_not_kept_by_reference():
    log = _log()
    fields = {"blind": "cover.a"}
    log.observe(COMMAND_WOULD_CALL, fields)
    fields["blind"] = "cover.tampered"
    assert log.last["blind"] == "cover.a"
