# Cover Logic

A universal, rule-based cover (blind) controller for Home Assistant.

Instead of one Jinja template and a pile of scripts wired to a specific
house, you write a YAML configuration — blinds, zones, modes, conditions
and rules — and a small, pure Python engine decides the position and tilt
for every blind, on every evaluation. It exists because the author's own
house ran that decision logic as a 367-line Jinja template plus ~1500
lines of YAML scripts, none of which could move to a different house
without being rewritten. This project turns the same logic into data.

For the full technical picture — architecture, the decision model, the
configuration format, the migration gate, and the rules for changing this
code — see **[`MODELS.md`](MODELS.md)**. This README is the shorter,
human-facing version.

## Status

**Phase 1 (decision engine) and phase 2 (Home Assistant integration,
shadow mode) are complete. Phase 3 (actually moving a cover) and phase 4
(a rule-editing UI) are not built yet.**

Concretely, today:

- The engine is a real, installable Home Assistant config entry. It loads
  a YAML rules file, validates it, and evaluates it against live state.
- It exposes one diagnostic sensor, `sensor.cover_logic_mode`, showing the
  active mode, the decided action for every blind, why each one fired, and
  a live comparison against an old system it may be replacing.
- **It does not move anything.** No code path in this repository issues a
  Home Assistant service call — `tests/test_no_movement.py` enforces this
  and is deleted, on purpose, the day phase 3 lands.
- Configuration is a hand-written YAML file today. There is a minimal
  config flow (just the file path), but the full rule-editing UI is phase
  4, not built.

If you are looking for something that already opens and closes your
blinds, this is not it yet — it is the decision-making half of that
system, running safely alongside whatever already controls your blinds.

## Install

This is a [HACS](https://hacs.xyz/)-shaped custom integration
(`custom_components/cover_logic/`, `hacs.json` at the repo root), but it is
not yet published to the HACS default store. To try it:

1. Clone or copy `custom_components/cover_logic/` into your Home
   Assistant's `custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration from Settings → Devices & Services → Add
   Integration → "Cover Logic". It will ask for one thing: the path to
   your configuration YAML file (default `/config/cover_logic.yaml`).

## Configure

Write a YAML file describing your blinds, the zones they belong to, the
modes your house can be in, named reusable conditions, and the
first-match-wins rules that decide each blind's position and tilt per
mode and zone.

`docs/example-config.yaml` is a complete, commented, working example for
an invented house (not the author's own — see below). `MODELS.md` §4 is
the field-by-field reference. `tests/test_example_config.py` keeps the
example itself honest: it is loaded and validated on every test run.

`fixtures/dom_peter.yaml` is a *different* file: it is the real
configuration for the house this project was built for, and it doubles as
the fixture the migration gate compares against. It is not an example to
copy from and is not touched for documentation purposes (see `MODELS.md`
§9).

## Run the tests

Two interpreters, both must stay green:

```bash
# Pure decision core, no Home Assistant needed — any Python >= 3.12
python3 -m pytest tests/ -q

# The Home Assistant integration layer — needs homeassistant==2026.8.0,
# which itself requires Python >= 3.14.2. The repo carries a `.venv` for it.
.venv/bin/python -m pytest tests/ -q
```

See `MODELS.md` §8 for why two interpreters and what each one actually
exercises, and §5 for the migration gate (`tests/parity/`), which needs
files from a live Home Assistant host and therefore cannot run in CI —
run it locally before trusting any change to the decision logic.

Lint and format:

```bash
ruff check .
ruff format --check .
```

## Documentation

- **[`MODELS.md`](MODELS.md)** — the full technical brief: architecture,
  decision model, configuration format, migration gate, deliberate
  decisions that look like bugs, phase status, and the rules for changing
  this code. Read this before making any nontrivial change.
- **[`docs/rationale.md`](docs/rationale.md)** — design decisions and
  debugging traps for the pure core, organised by module.
- **[`docs/example-config.yaml`](docs/example-config.yaml)** — a complete,
  commented example configuration for a different house.
- **[`docs/phase-2-findings.md`](docs/phase-2-findings.md)** — findings
  from a phase 1 code review; see `MODELS.md` §7 for which are fixed and
  which are still open.
- **[`docs/spike-condition-selector.md`](docs/spike-condition-selector.md)**
  — research spike for phase 4, on whether Home Assistant's native
  `condition` selector can be used in a config-entry subentry flow.
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — how to run the tests, the
  parity-first rule, and the code style this repository follows.

## License

MIT — see [`LICENSE`](LICENSE).
