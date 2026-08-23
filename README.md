# Cover Logic

Universal rule-based blind/cover controller for Home Assistant.

## Status

**Phase 1**: Pure Python decision engine without Home Assistant integration. All logic is tested without HA runtime, event loop, or I/O.

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

See the specification and design rationale in the docs directory (coming in phase 2).
