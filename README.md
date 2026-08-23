# Cover Logic

Universal rule-based blind/cover controller for Home Assistant.

## Status

**Phase 1**: Pure Python decision engine without Home Assistant integration. All logic is tested without HA runtime, event loop, or I/O.

As of 2026-08-23: **165 tests pass** (`python3 -m pytest tests/`), including the full migration gate (`test_parity_on_the_whole_space`, all 92 160 scenarios) and rule-coverage proof (`test_every_rule_fires_at_least_once` — all 83 rules in `fixtures/dom_peter.yaml` fire at least once, none unreachable). Full run, gate included: **43.58s**. The default run (`-m "not slow"`, excluding the full-space gate) is 164 tests in 14.55s.

## Quick Start

### Run tests

```bash
python3 -m pytest tests/ -q
```

### Run migration bridge (local only)

Requires `/config/configuration.yaml` and `/config/automations.yaml`:

```bash
python3 -m pytest tests/parity -q
```

This tool analyzes existing HA automations and maps them to `cover_logic` rules.

## Architecture

The six core modules form a pure Python decision core:

- `model.py` — Frozen data types for rules, conditions, and cover states
- `world.py` — Snapshot of system state at evaluation time
- `conditions.py` — Condition evaluation engine
- `config_schema.py` — YAML configuration parser
- `engine.py` — Core decision logic and rule application
- `validation.py` — Static invariant checking

None of these modules import Home Assistant. This is enforced by `tests/test_purity.py`.

## Documentation

The specification and design rationale are available in the Home Assistant config directory at:
`/config/docs/superpowers/specs/2026-08-23-cover-logic-design.md`
