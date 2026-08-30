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

# ---------------------------------------------------------------------------
# `guards:` -- the vocabularies one guard entry is written in.
#
# Here rather than in `model.py` for the same reason the `COND_*` strings are
# here: the pure core (`config_schema` parsing them, `validation` checking
# them, `model` defaulting to them) and the Home Assistant layer (a future
# `guard` subentry flow's selectors, its `strings.json` labels) must spell
# them identically, and a second copy of a string vocabulary is a second
# thing that can drift.
# ---------------------------------------------------------------------------

# What a matching guard does. There is deliberately no `skip_close` here:
# direction is a *field* (`GUARD_DIRECTIONS`), not a second copy of every
# policy -- see `docs/rationale.md`, "Why direction is a guard field and not
# a `skip_close` policy".
GUARD_SKIP = "skip"
GUARD_DEFER = "defer"
GUARD_FORCE = "force"
GUARD_POLICIES = (GUARD_SKIP, GUARD_DEFER, GUARD_FORCE)

# Which movement a guard is about.
#
# `closing` means a DECREASING POSITION, and nothing else. For a blind with
# slats, "closing it" is two-dimensional in ordinary speech -- drive it down
# *and* shut the slats -- but every door/sauna interlock in the house this
# schema is derived from exists to stop one specific thing: the blind being
# driven down onto an open door or a running sauna. Slats moving on a blind
# that is already where it is hurt nobody. Reading `closing` as "any downward
# movement including the slats" would make nine of the house's thirteen
# guards refuse tilt commands they have always allowed -- so the axis is the
# position axis, on purpose.
GUARD_CLOSING = "closing"
GUARD_OPENING = "opening"
GUARD_ANY = "any"
GUARD_DIRECTIONS = (GUARD_ANY, GUARD_CLOSING, GUARD_OPENING)

# When a guard gets its say: before the engine is asked (the guard removes
# the target from the input, so no decision is made for it at all) or after
# (the guard inspects the decided action and overrides it). Both shapes exist
# in the house -- see `docs/rationale.md`, "Why a guard has a `stage`".
GUARD_STAGE_INPUT = "input"
GUARD_STAGE_OUTPUT = "output"
GUARD_STAGES = (GUARD_STAGE_INPUT, GUARD_STAGE_OUTPUT)

# What a `defer` does once `max_wait` has elapsed. These two are opposites,
# both are in use in the house, and which one is meant is never inferable --
# hence no default anywhere (see `docs/rationale.md`, "Why `defer` states
# both `max_wait` and `on_timeout`").
GUARD_TIMEOUT_PROCEED = "proceed"
GUARD_TIMEOUT_ABANDON = "abandon"
GUARD_TIMEOUTS = (GUARD_TIMEOUT_ABANDON, GUARD_TIMEOUT_PROCEED)

# How often a pending `defer` is re-examined when the guard's own config does
# not say. Restart resilience is a property of the guard, not a second object
# a human has to remember to pair with it, so every parsed `defer` carries a
# `recheck_every` whether its author wrote one or not. 900 s is the interval
# the house's one *working* restart watchdog actually runs at
# (`time_pattern: /15`), chosen over inventing a number.
GUARD_DEFAULT_RECHECK = 900
