"""Every house automation that reacts to something this integration also reads.

One event wakes both: Home Assistant starts the automation's `for:` and the
coordinator starts its settle window. If the window expires first, the engine
evaluates a world where the event has happened and the automation's reaction
has not. Sometimes that intermediate world is harmless; on 2026-09-01 it was
not, and the bedroom slats went to 100 while people slept -- the window was
2 s and `svitanie`'s trigger carried `for: 2 s`, so the two landed on the same
second and the coin flip lost.

**This test makes no claim that the current window is large enough.** It cannot:
whether an intermediate world is harmful depends on what the rules decide in
it, and that varies per trigger. What it does is refuse to let the set grow
silently. Every pair below has been looked at once; a new or changed one fails
here, which is the moment to look again -- rather than one morning a month
later.

The same pattern as `tests/test_purity.py`: the list *is* the reviewed
decision, and the test exists so nothing joins it without review.

Host-only -- it reads `/config/automations.yaml`, the tradeoff `jinja_bridge`
already makes for the matrix template.
"""

import datetime as dt
import json
from pathlib import Path
import re

import pytest
import yaml

from cover_logic.config_schema import load_config_file, referenced_entities
from cover_logic.const import EVAL_SETTLE_SECONDS

AUTOMATIONS = Path("/config/automations.yaml")

pytestmark = pytest.mark.skipif(
    not AUTOMATIONS.exists(), reason="needs this house's /config/automations.yaml"
)

# Write services whose effect the engine could read back.
WRITE_SERVICES = frozenset(
    {
        "homeassistant.toggle",
        "homeassistant.turn_off",
        "homeassistant.turn_on",
        "input_boolean.toggle",
        "input_boolean.turn_off",
        "input_boolean.turn_on",
        "input_number.set_value",
        "input_select.select_option",
        "input_text.set_value",
    }
)

# (automation alias, trigger id, `for:` in seconds) -> why this intermediate
# world is survivable. Reviewed 2026-09-01. Anything not in here fails.
#
# The recurring reason is worth stating once: for every entry marked "keep",
# the intermediate world has the *event* visible but the room flag not yet
# written, and every rule guarded by `nie_akt_*` therefore resolves to
# `position: keep, tilt: keep`. Nothing moves, and the real decision happens
# on the next evaluation after the write. That is exactly what failed on
# 2026-09-01: there the flags were stale *on*, not not-yet-written, so the
# rules fell through to an action instead of to `keep`.
REVIEWED: dict[tuple[str, str, float], str] = {
    (
        "Lighting SUN",
        "cover-down",
        300.0,
    ): "writes cover_down on; intermediate mode is the old one, no command",
    ("Lighting SUN", "cover_up", 120.0): "writes cover_down off; intermediate stays noc -> keep",
    (
        "Lighting SUN",
        "light-off",
        300.0,
    ): "lux branch; writes cover_down off, intermediate stays noc -> keep",
    (
        "Lighting SUN",
        "light-off",
        600.0,
    ): "sun branch, same write as above; intermediate stays noc -> keep",
    (
        "Lighting SUN",
        "light-on",
        300.0,
    ): "writes cover_down; vecer clause is derived now, not from this",
    (
        "Lighting SUN",
        "light-out-on",
        300.0,
    ): "outdoor light branch; the cover_down write is shared, same as above",
    ("Sleeping Peter izba", "Night", 10.0): "writes aktivna_peter; intermediate flag off -> keep",
    (
        "Sleeping Peter izba",
        "postel-on",
        10.0,
    ): "writes aktivna_peter; intermediate flag off -> keep",
    ("Sleeping Peter izba", "wakeup", 300.0): "writes aktivna_peter; intermediate flag off -> keep",
    ("Žalúzie - prepočet a uplatnenie", "alarm", 2.0): "under the window; write lands first",
    ("Žalúzie - prepočet a uplatnenie", "kvety", 5.0): "under the window; the bound const.py cites",
    ("Žalúzie - prepočet a uplatnenie", "ochrana", 2.0): "under the window",
    (
        "Žalúzie - prepočet a uplatnenie",
        "odchod",
        300.0,
    ): "nobody home; every zone resolves without a flag",
    ("Žalúzie - prepočet a uplatnenie", "pocasie", 2.0): "under the window",
    (
        "Žalúzie - prepočet a uplatnenie",
        "postel",
        300.0,
    ): "writes the three flags; intermediate off -> keep",
    (
        "Žalúzie - prepočet a uplatnenie",
        "prichod-dom",
        30.0,
    ): "arrival; intermediate flag off -> keep",
    (
        "Žalúzie - prepočet a uplatnenie",
        "prichod-mimka",
        30.0,
    ): "arrival; intermediate flag off -> keep",
    (
        "Žalúzie - prepočet a uplatnenie",
        "prichod-peter",
        30.0,
    ): "arrival; intermediate flag off -> keep",
    (
        "Žalúzie - prepočet a uplatnenie",
        "prichod-spalna",
        30.0,
    ): "arrival; intermediate flag off -> keep",
    (
        "Žalúzie - prepočet a uplatnenie",
        "priznak",
        2.0,
    ): "under the window; reacts to the flags themselves",
    (
        "Žalúzie - prepočet a uplatnenie",
        "svitanie",
        2.0,
    ): "THE 2026-09-01 case; survivable only because the window is now 8 s",
    ("Žalúzie - prepočet a uplatnenie", "slnko", 2.0): "under the window",
    (
        "Žalúzie - prepočet a uplatnenie",
        "strana",
        2.0,
    ): "under the window; four azimuth thresholds share one id",
}


def _seconds(value) -> float:
    """`for:` in any of Home Assistant's spellings, as seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        parts = [int(part) for part in value.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return dt.timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2]).total_seconds()
    if isinstance(value, dict):
        return dt.timedelta(
            hours=int(value.get("hours", 0) or 0),
            minutes=int(value.get("minutes", 0) or 0),
            seconds=int(value.get("seconds", 0) or 0),
        ).total_seconds()
    msg = f"unrecognised `for:` shape: {value!r}"
    raise AssertionError(msg)


def _write_targets(node, out: list[str]) -> None:
    """Every literal `entity_id` a write service inside `node` aims at."""
    if isinstance(node, dict):
        service = node.get("service") or node.get("action")
        if isinstance(service, str) and service in WRITE_SERVICES:
            for source in (node.get("target") or {}, node.get("data") or {}, node):
                if not isinstance(source, dict):
                    continue
                value = source.get("entity_id")
                if isinstance(value, str):
                    out.append(value)
                elif isinstance(value, list):
                    out.extend(str(item) for item in value)
        for value in node.values():
            _write_targets(value, out)
    elif isinstance(node, list):
        for value in node:
            _write_targets(value, out)


def _writes_any(automation: dict, read: set[str]) -> set[str]:
    """Which of `read` this automation writes -- literal targets *and* templated ones.

    The templated half is not optional. `Žalúzie - detekcia ručného zásahu`
    aims at `input_boolean.zaluzie_aktivna_{{ izba }}`, so a walk collecting
    only literal ids misses the one automation that switches all three room
    flags -- which is how it went missing from a writer map earlier the same day.
    """
    targets: list[str] = []
    _write_targets(automation, targets)
    hit = {entity for entity in read if entity in targets}

    blob = json.dumps(automation, ensure_ascii=False)
    for prefix in re.findall(r'"([a-z_]+\.[a-z0-9_]*)\{\{', blob):
        hit |= {entity for entity in read if entity.startswith(prefix)}
    return hit


def _delayed_reactions(read: set[str]) -> dict[tuple[str, str, float], set[str]]:
    """Every (automation, trigger, delay) that writes something in `read`."""
    automations = yaml.safe_load(AUTOMATIONS.read_text(encoding="utf-8"))
    found: dict[tuple[str, str, float], set[str]] = {}
    for automation in automations:
        written = _writes_any(automation, read)
        if not written:
            continue
        for trigger in automation.get("triggers") or automation.get("trigger") or []:
            if not isinstance(trigger, dict):
                continue
            held = _seconds(trigger.get("for"))
            if held <= 0:
                continue
            key = (str(automation.get("alias")), str(trigger.get("id")), held)
            found.setdefault(key, set()).update(written)
    return found


@pytest.fixture(scope="module")
def read_entities(fixtures_dir) -> set[str]:
    config = load_config_file(fixtures_dir / "dom_peter.yaml")
    return {
        entry[0] if isinstance(entry, tuple) else entry for entry in referenced_entities(config)
    }


def test_no_unreviewed_delayed_reaction(read_entities):
    """A new `for:` on an automation that writes what the engine reads must be looked at."""
    found = _delayed_reactions(read_entities)

    unknown = {str(key): sorted(value) for key, value in found.items() if key not in REVIEWED}
    detail = json.dumps(unknown, ensure_ascii=False, indent=1)
    assert not unknown, (
        "these delayed reactions are not in REVIEWED -- decide whether the "
        "intermediate world (event visible, reaction not yet written) can "
        f"command a movement, then add them with a reason: {detail}"
    )


def test_no_stale_entries_in_the_reviewed_list(read_entities):
    """The list must not outlive the house, or it stops meaning anything."""
    found = _delayed_reactions(read_entities)

    gone = sorted(str(key) for key in REVIEWED if key not in found)
    assert not gone, f"REVIEWED names reactions the house no longer has: {gone}"


def test_the_2026_09_01_case_is_still_covered_by_the_window(read_entities):
    """`svitanie` is the one that actually bit, and its margin is the window itself.

    Named on its own because it is the only entry whose survival depends on a
    number rather than on the intermediate world resolving to `keep`.
    """
    found = _delayed_reactions(read_entities)
    svitanie = [key for key in found if key[1] == "svitanie"]

    assert svitanie, "the `svitanie` trigger has gone -- this test needs rewriting, not deleting"
    for _alias, _trigger, held in svitanie:
        assert held < EVAL_SETTLE_SECONDS, (
            f"settle window {EVAL_SETTLE_SECONDS} s no longer outlasts `svitanie`'s "
            f"{held} s delay -- this is exactly the 2026-09-01 failure"
        )
