"""Derive the lighting flags this integration publishes for other automations.

Pure: no `hass`, no clock, no I/O. The coordinator turns the returned desired
states into service calls. Kept off the decision path so a failure here cannot
move a blind. See docs/rationale.md -- "Why the integration writes the
lighting flags".
"""

from __future__ import annotations

from .conditions import evaluate_condition


def derive_outputs(config, world, output_helpers) -> dict[str, bool]:
    """Map each configured output helper to its desired on/off from its condition.

    `output_helpers` is the `{condition_name: entity_id}` mapping the user set
    on the Execution screen (`const.OPT_OUTPUT_HELPERS`); empty -- the shipped
    default -- derives nothing, so no installation's entity ids are baked in.
    """
    return {
        entity: evaluate_condition(config.conditions[cond], world, None, config.conditions)
        for cond, entity in output_helpers.items()
        if cond in config.conditions
    }
