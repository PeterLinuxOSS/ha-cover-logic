"""Constants shared across the integration."""

DOMAIN = "cover_logic"

# The config entry's one data key: the path to the YAML rules file. Shared
# between `__init__.py` (reads it) and `config_flow.py` (writes it), so the
# two can never drift to different key spellings.
CONF_CONFIG_PATH = "config_path"
DEFAULT_CONFIG_PATH = "/config/cover_logic.yaml"

# The config entry `VERSION` `config_flow.CoverLogicConfigFlow` declares and
# `__init__.async_migrate_entry` migrates up to -- one place, not two, so a
# version bump can never happen in one without the other. Version 1 is the
# original shape (`entry.data[CONF_CONFIG_PATH]`, no subentries); version 2
# is subentries as the source of truth, `CONF_CONFIG_PATH` no longer read.
CONFIG_ENTRY_VERSION = 2

# Built-in condition types beyond the Home Assistant native set.
COND_EVENT_TARGETS_ZONE = "event_targets_zone"
COND_REF = "ref"
COND_SUN_HITS_TARGET = "sun_hits_target"

# Default event kind used when nothing more specific applies.
EVENT_ARRIVAL = "arrival"
EVENT_STATE_CHANGE = "state_change"
