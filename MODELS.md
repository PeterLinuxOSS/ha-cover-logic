# MODELS.md — brief for an AI working on this repository

This is the single place an AI assistant should read to understand this
project before making changes. It is derived from the code, the tests and
`docs/` in this repository as of commit `5f6c228` (branch `main`). Where a
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
  subentries, so the UI and a YAML file are two doors into one representation
  rather than two representations. It duck-types the entry rather than
  importing `homeassistant`, which is what keeps it on this list.
- `engine.py` — `evaluate(config, world) -> Decision`, the decision core.
- `validation.py` — static checks over a `Config` (`validate(config) ->
  list[Problem]`).
- `legacy.py` — translates the *old* Jinja matrix's action vocabulary into
  `(position, tilt)`, shared by the migration gate and the live comparison
  sensor so the two can never define "matches" differently.

**Home Assistant layer:**

- `ha_world.py` — the only pure→HA seam: builds a `World` from a live
  `hass.states`. Imports `homeassistant` unconditionally.
- `coordinator.py` — subscribes to exactly the entities the config reads
  (`config_schema.referenced_entities`), debounces bursts, calls `evaluate`,
  and holds the last-known-good `Decision` even through a failing
  evaluation.
- `sensor.py` — `sensor.cover_logic_mode`, a diagnostic entity: mode,
  per-blind targets, trace, and a live diff against the old matrix.
- `config_flow.py` — a one-field setup flow (the config file path);
  validates on submit.
- `__init__.py` — the config entry's `async_setup_entry`/
  `async_unload_entry`.

`tests/test_purity.py` enforces the split with an AST walk: it parses
`model.py`, `world.py`, `conditions.py`, `config_schema.py`,
`config_store.py`, `engine.py`, `validation.py` and `legacy.py` and fails if
any of them imports anything
starting with `homeassistant`. This is what makes exhaustive testing of the
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
`coordinator.py`, `sensor.py`, `ha_world.py` and `config_flow.py` import
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
`modes`, `conditions`, `values`, `rules`, `guards`. (`guards` is accepted
and stored on `Config.guards` but not otherwise interpreted anywhere in
this codebase today — its schema is not settled; see `docs/rationale.md`.)

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
- **Phase 3 — execution. Not started.** No module in this repository
  issues a Home Assistant service call anywhere yet (that is exactly what
  phase 2's guard proves).
- **Phase 4 — UI (full rule-editing config flow / subentries). Not
  started.** The current `config_flow.py` is deliberately minimal — one
  field, the config file path — with a comment saying so explicitly.
  `docs/spike-condition-selector.md` is exploratory research for this
  phase (whether Home Assistant's native `condition` selector can be used
  in a subentry flow); it is a spike record, not implemented code.

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
  — see §5): **232 passed, 5 skipped**. The 5 skips are the five
  `tests/ha/*` modules, each behind its own module-level
  `pytest.importorskip("homeassistant")` — nothing under `homeassistant`
  is installed for this interpreter. On a checkout that is not this host,
  expect `tests/parity`'s 3 tests to skip too.
- **The repo's `.venv` (Python 3.14)** — `.venv/bin/python -m pytest
  tests/ -q`. `homeassistant==2026.8.0` itself requires Python ≥3.14.2;
  this venv exists so `tests/ha/` (everything behind
  `pytest.importorskip("homeassistant")`) actually runs instead of being
  skipped. As of this writing, same checkout: **281 passed**, no skips —
  `tests/ha/*` runs because `homeassistant` is installed in `.venv`, and
  `tests/parity` runs for the same host-adjacency reason as above.

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
  `conditions.py`, `config_schema.py`, `engine.py`, `validation.py` or
  `legacy.py` must not import `homeassistant`, directly or transitively —
  `tests/test_purity.py` enforces this per-module.
- **Do not touch `fixtures/dom_peter.yaml`** for anything other than
  keeping it in sync with the real house it describes. It is
  simultaneously the live configuration this repository's author runs and
  the migration gate's fixture; changing it for a documentation or example
  purpose breaks both.
- **Consult `docs/rationale.md` before "fixing" anything that looks odd**
  in the pure core — it is the collected record of exactly this class of
  trap, organised by module.
- **Run both interpreters' test suites (§8) before claiming green**, and
  run the migration gate locally (`tests/parity`, only possible on the
  Home Assistant host — see §5) before trusting any change to `engine.py`,
  `conditions.py`, or the meaning of `fixtures/dom_peter.yaml`.
