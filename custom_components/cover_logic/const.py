"""Constants shared across the integration."""

DOMAIN = "cover_logic"

# Built-in condition types beyond the Home Assistant native set.
COND_EVENT_TARGETS_ZONE = "event_targets_zone"
COND_REF = "ref"
COND_SUN_HITS_TARGET = "sun_hits_target"

# Default event kind used when nothing more specific applies.
EVENT_ARRIVAL = "arrival"
EVENT_STATE_CHANGE = "state_change"
