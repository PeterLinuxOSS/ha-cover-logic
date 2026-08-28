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

# The zone half of a `"<mode>.<zone>"` rules key that marks a *default* rule
# list for that mode -- a rule with no zone of its own, inherited by every
# zone in that mode. Chosen as an explicit sentinel over simply omitting the
# zone (e.g. a bare `"<mode>"` key) because omission is easy to miss reading
# a `rules:` mapping by eye -- a key that is just `noc` next to five that are
# `noc.<zone>` looks like a typo, not a deliberate "applies everywhere". `"*"`
# also costs nothing extra in the subentry shape: a `rule` subentry's `zone`
# field is already required (`config_store._require`), so there is no
# "omitted" spelling available there without a second, subentry-only rule
# shape -- see also `config_schema._reject_zone_id`, which is what keeps a
# real zone from ever being named this and colliding with it.
RULE_DEFAULT_ZONE = "*"

# Default event kind used when nothing more specific applies.
EVENT_ARRIVAL = "arrival"
EVENT_STATE_CHANGE = "state_change"
