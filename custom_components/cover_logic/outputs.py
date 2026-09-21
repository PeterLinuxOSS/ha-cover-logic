"""Derive the lighting flags this integration publishes for other automations.

Pure: no `hass`, no clock, no I/O. The coordinator turns the returned desired
states into service calls. Kept off the decision path so a failure here cannot
move a blind. See docs/rationale.md -- "Why the integration writes the
lighting flags".
"""

from __future__ import annotations

from .conditions import evaluate_condition
from .const import OUTPUT_CONDITIONS


def derive_outputs(config, world) -> dict[str, bool]:
    """Map each output helper to its desired on/off from its named condition."""
    return {
        entity: evaluate_condition(config.conditions[cond], world, None, config.conditions)
        for entity, cond in OUTPUT_CONDITIONS.items()
        if cond in config.conditions
    }
