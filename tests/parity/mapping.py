"""Parity-test glue around the old-vocabulary translation.

The translation itself (`to_action`/`expected_actions`) lives in
`cover_logic.legacy` now -- it is not just a test helper, `sensor.py`'s
`matica_diff` uses the exact same functions to compare the engine against the
live matrix in the house. Re-exported here so `test_migration_gate.py` does
not need to change its import. `world_from_stav` stays here: it depends on
`jinja_bridge.now_for` and the `Stav` test fixture, both specific to this
offline gate, not something the production sensor has any use for.
"""

from cover_logic.legacy import expected_actions, to_action
from cover_logic.world import Event, World

from .jinja_bridge import now_for

__all__ = ["expected_actions", "to_action", "world_from_stav"]


def world_from_stav(stav, event: Event | None = None) -> World:
    """Feed the engine exactly the state the Jinja render saw."""
    return World(
        states=stav.entity(),
        attributes=stav.atributy(),
        now=now_for(stav),
        event=event or Event(),
    )
