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

# The one *operational* option of this integration: whether `runner.py` really
# issues `cover.*` service calls or only logs the ones it would have issued.
#
# It lives in `entry.options`, not `entry.data`, on purpose. `entry.data` is
# the house's configuration -- subentries and guards -- and it is versioned, so
# writing to it drags in `CONFIG_ENTRY_VERSION` and migration logic for a
# switch that gets flipped twice in its life and then once in an emergency.
# Options come with `async_update_entry` and an update listener for free, so
# the runner reads a change **without a reload**, which is the entire point of
# "turn it off when something goes wrong".
#
# The default is `True` -- dry run on -- including for an entry that existed
# before the runner did. An integration that has never had hands must not be
# handed a pair silently. Shared from here so `runner.py` (reader) and
# `options_flow.py` (writer) can never spell the key differently.
OPT_DRY_RUN = "dry_run"
DEFAULT_DRY_RUN = True

# ---------------------------------------------------------------------------
# The settle window: how long `coordinator.py` waits after the *last* watched
# state change before it evaluates.
#
# **This is not a performance knob. It is the fix for a measured defect.** On
# 2026-08-31, on the live house (local times, from the log and from state
# history):
#
#     05:34:25   input_boolean.cover_down             on -> off  (fires `svitanie`)
#     05:34:26   cover_logic evaluated                -> spalna: tilt 100
#     05:34:27   input_boolean.zaluzie_aktivna_spalna on -> off  (`svitanie` resets it)
#
# `svitanie` (`automation.zaluzie_prepocet_a_uplatnenie`, trigger id
# `svitanie`) switches the night flag off and *then* resets the per-room "this
# room is in use" flags -- two writes about one transition, a second apart. A
# coordinator that re-evaluates on the first of them reads a world that never
# really existed: night over, bedroom still in use. It decided `tilt: 100` for
# `spalna`, i.e. open the parents' slats while they were asleep. Harmless only
# because that morning was a `dry_run` day.
#
# Two seconds because that is what every automation in the house already uses
# on this same event -- `svitanie`'s own trigger carries `for: {seconds: 2}`,
# and the house's `CLAUDE.md` states the rule: "Dve automatizácie na tej istej
# udalosti = preteky. Ak jedna mení stav, ktorý druhá číta, daj druhej krátky
# `for:` (2 s stačí)." Matching that number rather than inventing one keeps the
# old system and the new one absorbing the same bursts; a value below the
# measured 1s gap would not fix the defect at all.
#
# `coordinator.py` restarts this window on every new change rather than
# batching a fixed window from the first one: `svitanie` writes several
# entities in sequence and the whole point is to evaluate after the *last* of
# them, whenever that lands.
#
# Not to be confused with `planner.SETTLE_SECONDS`, which is also two seconds
# and is a fact about the motors (a tilt command sent during travel is
# discarded). Two unrelated waits; deliberately not shared.
EVAL_SETTLE_SECONDS = 2.0
# The cap on that restarting window, measured from the *first* change of a
# burst. Restart-on-change is starvable: an entity that changes faster than the
# window is never quiet, so without a cap a single flapping sensor makes the
# integration deaf for as long as it flaps -- and this house has had exactly
# that (a Hue occupancy sensor with `occupancy_timeout=0`, `CLAUDE.md`'s
# "Kreslo senzor cuká").
#
# 10 s = five windows. It has to be comfortably wider than any real burst, or
# it would fire in the middle of one and reintroduce the defect above: the
# widest measured burst is `svitanie`'s own ~2 s, and a Home Assistant startup
# state restore lands within tens of milliseconds of itself. It also has to be
# short enough that nothing waits noticeably longer than it does today -- 10 s
# is well under the ~10-minute weather recompute cadence and well under one
# blind's ~55 s travel, so an evaluation forced out at the cap is never late
# relative to anything the house actually does.
EVAL_SETTLE_MAX_SECONDS = 10.0

# ---------------------------------------------------------------------------
# Readiness: what makes an input unreadable, and how many names a diagnostic
# may carry. See `readiness.py`'s module docstring for the measured minute
# these exist for.
#
# `unknown` and `unavailable` are Home Assistant's own two spellings of "there
# is an entity here but it cannot tell you anything", and a referenced entity
# absent from the `World` snapshot altogether is the third shape of the same
# fault -- `ha_world.build_world` leaves a missing entity out rather than
# inventing a state for it, so `World.state` answers `None`. All three are in
# here together because acting on any of them is the identical mistake.
UNREADY_STATES = frozenset({None, "unknown", "unavailable"})

# How many entity names a readiness reason or attribute may name before it
# says "+N more". Ten is a diagnostic a person reads; a hundred is noise, and
# an unbounded state attribute is one a big enough house could use to take the
# diagnostic entity down at write time.
READINESS_MAX_NAMED = 10

# How a readiness withholding announces itself in `CommandLog` and in the log.
# A guard withholding carries its own `policy`/`guard` fields and this one
# carries neither -- no guard fired -- so the prefix is what tells the two
# apart, and it is shared rather than spelled once here and once in a test.
READINESS_REASON_PREFIX = "world not ready"

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

# ---------------------------------------------------------------------------
# What the executor did, or deliberately did not do -- the five states one
# entry in `command_log.CommandLog` can be in.
#
# Here for the same reason as everything else in this section: `runner.py`
# names three of them when it tells an observer what it just logged,
# `command_log.py` names all five when it stores them, `sensor.py` shows them,
# and a person greps the log for them. Five spellings in four files is four
# places for a typo to be invisible.
# ---------------------------------------------------------------------------

# The runner reached a command and dry run stopped it.
COMMAND_WOULD_CALL = "would_call"
# The runner reached a command and issued it (not a dry run).
COMMAND_CALLED = "called"
# An axis had a target and produced no command anyway -- the dead band, or a
# blind with no slats. The difference between "it stood still" and "it was
# never asked", which is what nobody could reconstruct on 2026-08-27.
COMMAND_SUPPRESSED = "suppressed"
# A guard took the decision away before the runner ever saw it. Recorded by the
# coordinator: the runner is never told about a command it was not given.
COMMAND_WITHHELD = "withheld"
# The runner handed a command to its injected service caller.
COMMAND_DISPATCHED = "dispatched"

# How often a pending `defer` is re-examined when the guard's own config does
# not say. Restart resilience is a property of the guard, not a second object
# a human has to remember to pair with it, so every parsed `defer` carries a
# `recheck_every` whether its author wrote one or not. 900 s is the interval
# the house's one *working* restart watchdog actually runs at
# (`time_pattern: /15`), chosen over inventing a number.
GUARD_DEFAULT_RECHECK = 900
