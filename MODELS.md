# MODELS.md — brief for an AI working on this repository

This is the single place an AI assistant should read to understand this
project before making changes. It is derived from the code, the tests and
`docs/` in this repository as of commit `ea044eb` (branch `main`). Where a
claim could not be verified against something in the repo, that is said
explicitly rather than guessed.

If you are an AI assistant and a pointer file (`CLAUDE.md`, `AGENTS.md`,
`GEMINI.md`, `.github/copilot-instructions.md`) sent you here, this file is
the source of truth; those files exist only to point at it, and duplicating
their content there is exactly the failure mode this project's own author
keeps fighting elsewhere (see `docs/rationale.md`'s design principle: one
representation, not several that can drift).

## 1. What this is and why it exists

`cover_logic` is a universal, rule-based cover (blind) controller for Home
Assistant. It exists to replace a house-specific decision system: a
367-line Jinja template (`sensor.zaluzie_cielovy_stav` in that house's
`configuration.yaml`) plus roughly 1500 lines of YAML across scripts and
automations, all wired to ten specific `cover` entities, seven zones and
four modes in one particular house.

That system works, but it cannot move to a different house without being
rewritten from scratch — the logic and the house are the same 1850-odd
lines of YAML. `cover_logic` is the same decision logic re-expressed as
data (a YAML configuration file parsed into typed Python objects) plus a
small pure engine that evaluates it. Moving to a different house is meant
to become a configuration change, not a rewrite. `fixtures/dom_peter.yaml`
is that same house's matrix transcribed into this format; the project
proves parity against it before anything is allowed to change.

## 2. Architecture

`custom_components/cover_logic/` is split into two halves, enforced by a
test:

**Pure core — no Home Assistant import, ever:**

- `model.py` — frozen dataclasses: `Blind`, `Zone`, `Mode`, `Rule`, `Action`,
  `Ref`, `Config`, and the `KEEP` singleton.
- `world.py` — `World`, an immutable snapshot of every state/attribute the
  engine may read, plus `Event` and `Target`.
- `conditions.py` — evaluates one condition body against a `World`.
- `config_schema.py` — parses YAML text into a `Config`.
- `config_store.py` — builds the same `Config` from Home Assistant config
  subentries (`config_from_subentries`), so the UI and a YAML file are two
  doors into one representation, and writes the inverse
  (`subentries_from_config`, used by `import_config` and by first-run
  setup), self-checked by round-tripping back through
  `config_from_subentries` before trusting it. Duck-types the entry rather
  than importing `homeassistant`, which is what keeps it on this list.
  Also the one place that groups and sorts rule subentries by
  `(mode, zone, order)` (`_grouped_rules`) — every other rule-ordering
  consumer reads that function instead of re-deriving the sort (§9) — and
  the one writer of `entry.data["guards"]` (`guards_to_data`), which
  `__init__.py`'s migration, `config_flow.py`'s first run and
  `services.py`'s import all call rather than each serializing guards
  themselves.
- `conformance.py` — `diff_configs(live, reference)`, a field-by-field
  `Config` equality check, no YAML/text comparison. Used both by
  `__init__.py`'s startup repair-issue check and by the options-flow
  health overview to detect drift from `fixtures/dom_peter.yaml`.
- `engine.py` — `evaluate(config, world) -> Decision`, the decision core.
- `validation.py` — static checks over a `Config` (`validate(config) ->
  list[Problem]`), including which subentry(ies) a problem is attributable
  to (`Problem.owners`, read by `subentry_flow.py`'s form-blocking logic
  and by the options-flow health report).
- `legacy.py` — translates the *old* Jinja matrix's action vocabulary into
  `(position, tilt)`, shared by the migration gate and the live comparison
  sensor so the two can never define "matches" differently.
- `starter_config.py` — the bundled example a first run can start from.
- `planner.py` — `plan(blind, current_position, current_tilt, action) ->
  Plan`: one blind's decided `Action` turned into the ordered `Command`s that
  would realise it (`SetPosition`, `WaitForPosition`, `Settle`, `SetTilt`),
  plus the `Clamp`s that had to be applied. Descriptions only — it issues
  nothing. This is where the motor-level facts live: a tilt command sent
  during travel is discarded, the angle is only ever set by movement, a
  repeated absolute command is itself a movement (hence a dead band, not
  equality), and 0..100 clamping happens here because the engine
  deliberately does not clamp (§6). See `docs/rationale.md` — "`planner.py`".

**Home Assistant layer:**

- `ha_world.py` — the only pure→HA seam: builds a `World` from a live
  `hass.states`. Imports `homeassistant` unconditionally.
- `coordinator.py` — subscribes to exactly the entities the config reads
  (`config_schema.referenced_entities`), debounces bursts, calls `evaluate`,
  and holds the last-known-good `Decision` even through a failing
  evaluation.
- `sensor.py` — `sensor.cover_logic_mode`, a diagnostic entity: mode,
  per-blind targets, trace, and a live diff against the old matrix.
- `config_flow.py` — the setup flow: a menu of four ways to start (set up
  blinds now, load a YAML file, start from the bundled example, start
  empty), each ending in `async_create_entry`; also registers the six
  subentry flow handlers (`async_get_supported_subentry_types`) and hands
  out the options flow (`async_get_options_flow`).
- `subentry_flow.py` — the six subentry-type flow handlers (`blind`, `zone`,
  `value`, `condition`, `mode`, `rule`): one `ConfigSubentryFlow` subclass
  per type, plus the schema/data-conversion/validation machinery
  `options_flow.py` reuses rather than duplicates ("one owner, two doors",
  below). Also where a save is blocked or allowed: an `ERROR`-severity
  `Problem` only blocks the specific subentry save that could actually fix
  it (`_CODE_OWNERS`/`_blocks_on`, reading `Problem.owners` where one type
  alone cannot disambiguate) — see §9.
- `options_flow.py` — the main menu (`CoverLogicOptionsFlow`): per-type
  add/edit/remove over counts read off `entry.subentries`, a read-only
  "rules in real evaluation order" report, import/export, and a health
  overview (validation error/warning counts attributed to the causing
  subentry, conformance against `fixtures/dom_peter.yaml`, the
  coordinator's last recompute). "One owner, two doors": it never
  re-implements a subentry type's form, it builds a bare instance of the
  matching `subentry_flow.py` class and calls its shared methods directly —
  every one of those methods needs nothing from `self` but the `entry` it
  is explicitly handed.
- `services.py` — the `cover_logic.import_config`/`export_config` services:
  the bridge between the YAML representation and subentries.
  `import_config` validates first, then either replaces every existing
  subentry or refuses outright (never a partial merge); `export_config`
  refuses a symlink, a directory, a missing parent, or a target that
  already has whole-line comments (`dump_config` cannot reproduce them, so
  overwriting one would silently discard them).
- `const.py` — the few names shared across the split (`DOMAIN`, the config
  entry data key, the current `CONFIG_ENTRY_VERSION`, the non-HA-native
  condition-type strings), so the config-flow version bump and
  `async_migrate_entry`'s target version can never drift apart. No
  `homeassistant` import, but not in the purity-tested list below — it is
  imported by both sides.
- `__init__.py` — the config entry's `async_setup_entry`/
  `async_unload_entry`/`async_migrate_entry` (moves a version-1, file-path
  entry onto subentries without touching the original file) and the
  `fixture_drift` repair-issue check.

`tests/test_purity.py` enforces the split with an AST walk: it parses
`model.py`, `world.py`, `conditions.py`, `config_schema.py`,
`config_store.py`, `conformance.py`, `engine.py`, `validation.py`,
`legacy.py`, `starter_config.py` and `planner.py` and fails if any of them
imports anything
starting with `homeassistant`. That list, not anyone's intention, is what
enforces the split — a new pure module is only pure once its filename is in
`PURE_MODULES`. This is what makes exhaustive testing of the
decision logic possible without an HA runtime, event loop or I/O — the whole
`tests/test_scenarios.py` / `tests/parity/` machinery depends on the core
being callable as plain, fast, synchronous Python.

**Why `__init__.py` defers its Home Assistant imports.** A package's
`__init__.py` executes merely by importing any submodule of that package —
including `from cover_logic.config_schema import load_config` — and that
happens from the system-Python 3.12 pure test run, which has no
`homeassistant` installed at all (see §7). An unconditional
`from homeassistant... import ...` at the top of `__init__.py` would break
every one of those imports, not just this module's own tests. So
`__init__.py` puts every Home Assistant name behind `TYPE_CHECKING`
(quoted, since the project does not use `from __future__ import
annotations`) or imports it inside the function that needs it, at call
time — see the module's own docstring for the full reasoning.
`coordinator.py`, `sensor.py`, `ha_world.py`, `config_flow.py`,
`subentry_flow.py`, `options_flow.py` and `services.py` import
`homeassistant` unconditionally at module level instead, because none of
them is ever imported by `cover_logic/__init__.py` itself — only by Home
Assistant's own loader, or by `tests/ha/` behind an explicit
`pytest.importorskip("homeassistant")` guard.

## 3. The decision model

`evaluate(config, world) -> Decision` (`engine.py`) is pure and total:
every blind reachable from the config gets exactly one `Action`, and
`Decision.trace` records which rule produced it, so "why did it do that"
is answerable from the output alone.

**Modes.** `config.modes` is an ordered tuple. The first mode whose `when`
condition matches the `World` wins (`_resolve_mode`); the last mode in the
list is meant to have `when: None` and act as the fallback.
`validation._check_modes` makes this an `ERROR` if no fallback exists at
all, or if a fallback exists but is not last (anything after it can never
match). Choosing no mode at all is not a valid outcome — `evaluate` raises
`EngineError` if it happens.

**Zones.** Every blind belongs to exactly one zone (`_resolve_ownership`
raises `EngineError` on a blind owned by two zones, or by none). A zone,
not an individual blind, is the unit rules are written against.

**Rules.** For the resolved mode and each zone, the rule list is looked up
by the key `f"{mode}.{zone_id}"` and evaluated first-match-wins, one blind
at a time (`_apply_rules`). A rule may be scoped to certain `events`
(`rule.events`); if `world.event.kind` is not in that set the rule is
skipped regardless of its condition. No matching rule, or no rules
configured for that key at all, both resolve to `Action()` (keep, keep) —
`engine.py`'s own comment calls this ambiguity, labelled `#none` in the
trace, deliberate: both causes end the same way and debugging either means
checking both.

**Actions.** An `Action` (`model.py`) is a pair of axes, `position` and
`tilt`. Each axis is independently one of: an `int` 0..100, the `KEEP`
singleton (leave this axis alone), or a `Ref` (read a Home Assistant
helper at evaluation time, via `engine._resolve_value`). This collapsed
what the old system expressed as five separate action kinds and eleven
named constants into one shape.

**`sun_hits_target` is relative to the blind being decided.**
(`conditions._sun_hits_target`) reads `target.blind.facade_azimuth` and
`target.blind.tolerance` off the `Target` passed into `evaluate_condition`
— not off any global or per-condition config — so the identical rule body
(e.g. "shade when the sun hits this facade") means something different for
each blind, purely because each `Blind` in `config.blinds` carries its own
azimuth. This is what lets one rule generalize across a house of any
orientation instead of needing a copy per facade.

## 4. The configuration format

A `Config` is parsed by `config_schema.load_config`/`load_config_file` from
one YAML document with up to seven top-level keys: `blinds`, `zones`,
`modes`, `conditions`, `values`, `rules`, `guards`. (`guards` has a real,
parsed, validated schema since phase 3 task 2, but nothing *evaluates* one
yet — `guards.py` is the next task. See `docs/rationale.md`'s "The `guards:`
schema" for the six decisions behind its shape.)

- **`blinds`** — a list of `{entity, facade_azimuth?, tolerance?,
  travel_time?, tilt_after_arrival?, has_tilt?}`. `entity` is the only
  required key.
- **`zones`** — a mapping of `zone_id -> {members, occupants?}`. `members`
  is a list of blind entity ids; `occupants` is a list of person names,
  read by `event_targets_zone`.
- **`modes`** — an ordered list of `{id, when?}`. The entry with no `when`
  must be last.
- **`conditions`** — a mapping of reusable, named condition bodies,
  referenced elsewhere with the `!ref <name>` YAML tag. The dialect is a
  subset of Home Assistant's native condition schema (`state`,
  `numeric_state`, `time`, `template`, `and`/`or`/`not`) plus two
  target-relative extensions this project adds: `sun_hits_target` and
  `event_targets_zone` (see §3). `numeric_state` requires an explicit
  `default` here, unlike stock HA — it mirrors Jinja's `| float(999)`
  fallback, and which side is "safe" depends on the rule, so it is never
  implied.
- **`values`** — a mapping of `name -> {entity, default}`: a helper entity
  read at decision time, for use as `!ref name` inside an action's
  `position`/`tilt`.
- **`rules`** — a mapping of `"<mode id>.<zone id>" -> list[Rule]`, each
  rule `{if?, then, events?, name?}`. `if` is a condition body (or list of
  them, ANDed); a rule with no `if` matches unconditionally and should be
  last in its list. `then` is `{position?, tilt?}`, each `keep`, an
  integer 0..100, or `!ref <value name>`.
- **`guards`** — an ordered list of safety interlocks, each
  `{policy, when?, targets?, applies_to?, stage?, max_wait?, on_timeout?,
  recheck_every?, then?, name?}`. `policy` is `skip`/`defer`/`force`; `when`
  is an ordinary condition body (the same dialect, `!ref` included — guards
  deliberately do not have a language of their own); `targets` names blind
  entity ids and/or zone ids and defaults to every blind; `applies_to` is
  `closing`/`opening`/`any`, where `closing` means a *decreasing position*
  and never the slats; `stage` is `output` (override the decided action) or
  `input` (drop the target before the engine is asked at all). A `defer`
  must state both `max_wait` (`null` = wait indefinitely, a value and not an
  omission) and `on_timeout` (`proceed`/`abandon`, no default — the two are
  opposites and both are in real use), and carries `recheck_every` so a
  pending wait survives a restart without a second hand-written watchdog.
  A `force` states the action it imposes in `then`. Guards resolve
  first-match-wins in written order, exactly like rules; there are no
  numeric priorities. `docs/example-config.yaml` has a worked example of
  all four shapes.

A mode or zone id must not contain a `.` — `engine.evaluate` joins
`f"{mode}.{zone_id}"` with a plain string concatenation and
`validation._check_rule_keys` recovers the two halves by splitting on the
*first* dot, so a dot in either id makes that join ambiguous and silently
misroutes a rules entry (`config_schema._reject_dot` rejects this at parse
time; see `docs/rationale.md`).

A worked example for a different, invented house is at
`docs/example-config.yaml`, checked by `tests/test_example_config.py`
(asserts `validate()` reports no `ERROR`-severity problem). It is not a
translation of `fixtures/dom_peter.yaml` and is not used by any parity
check.

**The same seven keys, as Home Assistant config-entry subentries.** Since
phase 4, a config entry can hold this same `Config` as six subentry types
(`blind`, `zone`, `value`, `condition`, `mode`, `rule` — one per YAML key
that is naturally "many small items"; `guards` lives in `entry.data` as the
same list of mappings a YAML `guards:` key holds, parsed by the same
`config_schema.parse_guards` and written back by `config_store.guards_to_data`
— only its *storage* is different, not its schema, and only until a `guard`
subentry type exists) instead of, or as well
as, a YAML file. `config_store.config_from_subentries` reads that shape into
the identical `Config` `config_schema.load_config` builds from text;
`config_store.subentries_from_config` is the inverse. A `mode`/`rule`
subentry carries an explicit integer `order` field YAML does not need —
subentries are an unordered flat mapping, so first-match-wins resolution
needs that field to mean anything. The live house's entry has 133 subentries
today (10 blinds, 7 zones, 1 value, 25 conditions, 4 modes, 86 rules).

## 5. The migration gate

`tests/parity/test_migration_gate.py` is, in its own words, "THE migration
gate": the new engine must produce, for every one of 92,160 scenarios,
exactly what the live Jinja matrix produces — entity by entity, on both
the `state` and `arrival` event variants. This is the project's central
guarantee: nothing gets switched over in the actual house until it stays
green.

It needs `/config/configuration.yaml` from the Home Assistant host it is
migrating away from, via a `matica.py` module living outside this
repository at `$HA_TESTS_DIR` (default `/config/tests`) that renders the
live template directly — `tests/parity/jinja_bridge.py` imports it by
adding that directory to `sys.path`. `bridge.available()` gates the whole
module with `pytest.mark.skipif` when `matica.py` does not exist at that
path, which is why `tests/parity/` cannot run in CI (the GitHub Actions
workflow excludes it explicitly, with a comment saying so) and why it is
skipped on any machine that is not this Home Assistant host. (This
repository happens to live inside that host's `/config` directory, at
`/config/dev/cover-logic` — so in *this* checkout specifically,
`tests/parity` finds `matica.py` and actually runs; see §8 for what that
does to the numbers.) Anyone changing `engine.py`, `conditions.py` or
`fixtures/dom_peter.yaml`'s meaning must run this locally, from a checkout
that has that file, before trusting the change.

`test_parity_on_the_whole_space` (marked `slow`) is the full 92,160-scenario
run; `test_parity_on_a_sample` checks every 97th scenario for fast
iteration; `test_mode_matches_on_the_whole_space` checks mode resolution
alone across everything. The `slow` marker exists only so the full run can
be opted out of explicitly with `-m "not slow"` — `pyproject.toml`'s own
comment says the default run intentionally has no such exclusion in
`addopts`, because this gate is meant to run every time.

## 6. Deliberate decisions that look like bugs

These are documented, intentional choices — not oversights — recorded in
full in `docs/rationale.md`, which every source module also points back to
inline. Do not "fix" any of these without first reading that file's
corresponding section:

- **Resolved positions are not clamped to 0..100.**
  (`engine._resolve_value`) A helper's live value can genuinely drift
  outside 0..100 (see the project's own `CLAUDE.md` on `float(8)`
  defaults vs. actual post-restart helper state), and the Jinja template
  being replaced does not clamp either — clamping here would itself break
  parity. Clamping belongs in a future execution layer, as a safety
  concern, not a decision-fidelity one. (Config-time literals and `!ref`
  *defaults* are still range-checked at parse time in `config_schema`
  — that is a different guarantee, catching the config author's mistake
  early, not the live value.)
- **`_resolve_value` truncates toward zero, not `round()`.** Parity-
  critical: the Jinja template uses `| int(34)`, which truncates. 50.7
  must resolve to 50, not 51. Beyond parity, truncation is also the
  conservative direction for a closing blind.
- **A broken user template raises, it does not evaluate to `False`.**
  (`conditions._template`, `jinja2.StrictUndefined`) An empty/failed
  render reading as `False` could mean "leave the house open during a
  heatwave" — this must never be wrapped in a `try`/`except` that
  swallows the error.
- **Sun sectors are half-open: `[facade - tolerance, facade + tolerance)`.**
  (`conditions._sun_hits_target`) The template being replaced used
  `az >= 45 and az < 135`; an inclusive upper bound breaks parity at
  exactly the boundary values (45/135/225/315) the scenario space tests.
- **`World.now` is naive local time**, deliberately, matching what Jinja's
  `now()` gives the template being replaced. `ha_world.build_world` sources
  it from `homeassistant.util.dt.now()` (DST-aware) and then strips the
  tzinfo, rather than doing a hand-rolled UTC-offset conversion — the
  latter is exactly the kind of bug the project's own `CLAUDE.md` records
  breaking across a DST transition before. `pyproject.toml`'s ruff config
  narrows the `DTZ` rule family for exactly this reason.

## 7. Project phases and current status

- **Phase 1 — the decision core. Complete.** Pure Python, tested
  standalone, no Home Assistant dependency.
- **Phase 2 — the Home Assistant layer, in shadow mode. Complete.** The
  integration is a real config entry: it loads and validates a config
  file, builds a coordinator that subscribes to live state and evaluates
  the engine, and exposes a diagnostic sensor that compares its decision
  against the still-live old matrix in real time. It moves nothing.
  `tests/test_no_movement.py` enforces this with two independent checks —
  an AST walk for any `<...>.services.async_call(...)` call shape, and a
  cruder grep-style check for the bare substring `"async_call"` anywhere
  under `custom_components/cover_logic/` — over every `.py` file in the
  package. That file's own docstring says explicitly: **this guard is
  temporary on purpose.** Phase 3 is what gives the integration hands (an
  executor that actually issues cover commands), and the same docstring
  instructs whoever lands phase 3 to `git rm tests/test_no_movement.py` in
  that commit — not to carve an exception into the check for the new,
  correct code.
- **Phase 3 — execution. Tasks 1 and 2 landed; still moves nothing.**
  `planner.py`
  (§2) turns a decided `Action` into a described sequence of commands, and
  is tested over the whole (capability × current position × current tilt ×
  target) grid in `tests/test_planner.py`. Task 2 gave `guards:` a real
  schema (§4): parsed by `config_schema`, validated by `validation`,
  round-tripped by `dump_config`/`config_store`, and read by
  `referenced_entities` so a guard's own entities are subscribed to — but
  **nothing evaluates a guard against a `World` yet.** There is no
  `guards.py`; that is the next task, and it is where the seven scattered
  re-implementations of the house's door/sauna interlock actually collapse
  into one. Nothing consumes the schema yet: no
  module in this repository issues a Home Assistant service call anywhere
  (that is exactly what phase 2's guard proves) — `tests/test_no_movement.py`
  is still in the repo, still passing, and now covers `planner.py` too.
  **There is no oracle for this phase.** The migration gate compares
  *decisions*, not execution; a planner tested against a model of a blind is
  not tested against a motor, so tilt timing and arrival behaviour are
  verified live or not at all. `guards.py` and `runner.py` are still to
  come, and `tests/test_no_movement.py` is deleted in its own commit when
  they land — that commit is the visible boundary between "moves nothing"
  and "moves things".
- **Phase 4 — UI: configuration through Home Assistant config-entry
  subentries. Complete, deployed, and live on the house.** Configuration
  now lives as six subentry types (`blind`, `zone`, `value`, `condition`,
  `mode`, `rule` — §4); `config_store.py` reads/writes them into the same
  `Config` the YAML path builds. `subentry_flow.py` gives each type its own
  add/edit/remove flow; `services.py` adds `import_config`/`export_config`
  (§2); `conformance.py` compares the live configuration against
  `fixtures/dom_peter.yaml`, surfaced as a `fixture_drift` repair issue on
  every setup and as a standalone test. `__init__.async_migrate_entry`
  moves a version-1 (file-path) entry onto subentries without deleting the
  original file. `docs/spike-condition-selector.md`, this phase's starting
  research question (can HA's native `condition` selector be reused in a
  subentry flow), was answered yes.
- **Phase 5 — making the phase 4 UI usable. Complete, deployed, and live.**
  Phase 4 shipped with `supports_options: false`: the entry page was Home
  Assistant's own generic flat "Add blind / Add zone / …" list, no counts,
  no way to tell an empty section from a full one — the owner's verbatim
  verdict was "vôbec to teraz nie je intuitívne". `options_flow.py` (§2) is
  the answer: a main menu with real counts, add/edit/remove per section, a
  "rules in real evaluation order" report, import/export, and a health
  overview. `config_flow.py`'s first-run step became a menu of four
  starting points instead of one bare file-path field. `tests/ha/
  test_step_id_coverage.py` drives every flow through Home Assistant's real
  `FlowManager`, catching a dangling `step_id` no other test in this
  package can see — see §9.

`docs/phase-2-findings.md` records eight findings from a review conducted
during phase 1, tracked as issues #3–#10. Checked against the code as it
stands now: five are fixed — condition-shape validation (#3, commit
`a665891`), per-zone containment of a bad rule (#4, `50e5d75`),
`sun_hits_target` reading the azimuth from an attribute (#5, `f7e35ed`),
`config_schema.referenced_entities()` (#8, `05f4a7f`), and the deep-copy of
snapshot attributes (#9, `2ec5f50`). Two remain open exactly as described:
`tests/scenarios.py` still fixes `NOW` as a module constant and raises
`_Infeasible` for any `time` condition it cannot already satisfy at that
instant (#6), and a `condition: template` rule still falls through to
`entity_id is None` in the same file and is reported unreachable rather
than genuinely evaluated (#7). The minor item (#10) is only partly done:
`tests/test_fixture_dom_peter.py::test_fixture_has_no_validation_errors`
still filters to `severity == ERROR` rather than asserting `validate(config)
== []`, exactly as the finding left it. Treat this file as a historical
record, not a live TODO list — this summary was checked once, against one
commit; re-verify against current `engine.py`/`conditions.py`/
`tests/scenarios.py` before relying on it.

## 8. How to run the tests

Two interpreters, on purpose:

- **System Python (3.12+)** — `python3 -m pytest tests/ -q`. This is the
  floor `pyproject.toml` sets (`requires-python = ">=3.12"`) deliberately
  *lower* than what Home Assistant itself needs, specifically so the pure
  decision core can be tested on whatever Python a contributor's machine
  already has, with no `homeassistant` package installed at all. As of
  this writing, run from this checkout (which sits inside the Home
  Assistant host's `/config`, so `tests/parity` finds `matica.py` and runs
  — see §5): **302 passed, 11 skipped**. The 11 skips are the eleven
  `tests/ha/*` modules, each behind its own module-level
  `pytest.importorskip("homeassistant")` — nothing under `homeassistant`
  is installed for this interpreter. On a checkout that is not this host,
  expect `tests/parity`'s 3 tests to skip too.
- **The repo's `.venv` (Python 3.14)** — `.venv/bin/python -m pytest
  tests/ -q`. `homeassistant==2026.8.0` itself requires Python ≥3.14.2;
  this venv exists so `tests/ha/` (everything behind
  `pytest.importorskip("homeassistant")`) actually runs instead of being
  skipped. As of this writing, same checkout: **534 passed**, no skips —
  `tests/ha/*` runs because `homeassistant` is installed in `.venv`, and
  `tests/parity` runs for the same host-adjacency reason as above.
  **This venv's `homeassistant==2026.8.0` is one version behind the house
  itself, which runs `2026.9.0b1`** — a known gap, not unnoticed drift: the
  integration has kept working across the host's own version bumps
  untouched. Re-pinning the venv is a separate task; do not "fix" it by
  accident while touching `pyproject.toml` or the venv.

Both must be run, and both must stay green — they are not redundant with
each other. `tests/test_purity.py` and `tests/test_no_movement.py` collect
and pass under both. CI (`.github/workflows/test.yml`) runs the matrix
`["3.12", "3.13", "3.14"]` against `tests/ --ignore=tests/parity`
(`tests/parity` cannot run in CI — see §5), plus a `ruff check .` /
`ruff format --check .` lint job, plus a `hassfest` job (using
`home-assistant/actions/hassfest@master`) validating the integration's
manifest and structure. All three jobs live in the one workflow file,
`.github/workflows/test.yml`.

## 9. Rules for anyone changing this code

- **Parity first, improvements later.** While the migration gate
  (`tests/parity/`) binds — i.e. until this engine has actually replaced
  the live Jinja matrix in the house it was built for — behaviour must
  not change, even where the current behaviour looks like a bug. See §6:
  several things that look wrong (unclamped values, truncation instead of
  rounding, a half-open sun sector) are deliberately preserving parity
  with the system being replaced. If you believe one of those is
  genuinely wrong, that is a phase-3-or-later conversation, not a patch
  to send now.
- **Never put a `claude.ai` session URL in a commit message**, in this
  repository or in any public PR to an upstream project. This is a
  standing rule from the operator's own `CLAUDE.md`, stated there as
  something that has already been gotten wrong before.
- **Keep the pure/HA split.** New code in `model.py`, `world.py`,
  `conditions.py`, `config_schema.py`, `config_store.py`, `conformance.py`,
  `engine.py`, `validation.py` or `legacy.py` must not import
  `homeassistant`, directly or transitively — `tests/test_purity.py`
  enforces this per-module.
- **Do not touch `fixtures/dom_peter.yaml`** for anything other than
  keeping it in sync with the real house it describes. It is
  simultaneously the live configuration this repository's author runs and
  the migration gate's fixture; changing it for a documentation or example
  purpose breaks both. `services.py`'s `export_config` may write to it
  directly (that is a legitimate re-export after editing through the UI),
  but never through `/config/cover_logic.yaml`, which is a symlink to it —
  see that module's own "Path safety" docstring section.
- **Consult `docs/rationale.md` before "fixing" anything that looks odd**
  in the pure core — it is the collected record of exactly this class of
  trap, organised by module.
- **A validation problem may block a subentry save only if that specific
  save could fix it.** Gotten wrong three times running — a hard-coded
  exemption list, then an exemption derived from which subentry *types*
  existed yet, which still deadlocked (adding a house's first zone made it
  permanently impossible to add another blind — a blind needs no zone to
  exist, but the exemption logic could no longer tell that apart from
  "zones aren't supported yet"). The fix, `subentry_flow._CODE_OWNERS` /
  `_blocks_on`, checks the *problem's own attribution* (`Problem.owners`,
  from `validation.py`) instead of inferring intent from what else is
  configured. Adding a new `ERROR`-severity `Problem` code means deciding
  which subentry type(s) can fix it (`_CODE_OWNERS`), or, if one instance
  of a type is not interchangeable with another (a dangling ref named by a
  specific `mode`/`rule`/`condition`), adding it to `_ATTRIBUTED_CODES`
  instead, matched against `Problem.owners` by identity. A code in neither
  dict silently never blocks anything — caught by a test asserting each
  owning form actually blocks, not by a crash.
- **A sort that decides behaviour must have exactly one implementation.**
  `config_store.py`'s rule grouping (`_grouped_rules`) used to be written
  twice — once for `Config.rules`, once for attributing a validation
  problem back to a subentry — kept equal only by the two authors' care,
  and diverged once, silently. The 92,160-scenario migration gate could
  not have caught it either way: it exercises what the engine *decides*,
  never which subentry a validation message points at. Anything needing
  rules (or modes) in order must read `_grouped_rules` (or `Config.modes`'s
  tuple order), never re-derive the sort.
- **`[%key:component::...%]`/`[%key:common::...%]` translation references
  do not resolve in a custom integration** — that syntax is core's own
  build-time indirection, and a custom component ships no such build step,
  so Home Assistant renders the raw placeholder on screen instead of a
  label. All three of `strings.json`, `translations/en.json` and
  `translations/sk.json` had 118 such references each, agreeing with each
  other on being broken — a test comparing only key *sets* across the
  files cannot catch this, since all three passed by being wrong together.
  Write literal text (English in `en.json`, Slovak in `sk.json`); never a
  `[%key:...%]` reference, however core's own strings look.
- **A `step_id` with no matching `async_step_<id>` method fails silently
  in every test that calls flow methods directly**, since none of them
  goes through Home Assistant's own
  `FlowManager._raise_if_step_does_not_exist`. `tests/ha/
  test_step_id_coverage.py` drives the real `FlowManager` to catch exactly
  this; keep it passing, and give a new screen its own `async_step_<id>`
  method rather than special-casing this test.
- **Run both interpreters' test suites (§8) before claiming green**, and
  run the migration gate locally (`tests/parity`, only possible on the
  Home Assistant host — see §5) before trusting any change to `engine.py`,
  `conditions.py`, or the meaning of `fixtures/dom_peter.yaml`.
