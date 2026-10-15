# Contributing

Read [`MODELS.md`](MODELS.md) first — architecture, the decision model,
the configuration format, and the migration gate all live there. This
file covers the mechanics of making and testing a change.

## The one rule that overrides the others

**Parity first, improvements later.** While the migration gate
(`tests/parity/`) binds — i.e. until this engine has actually replaced the
Jinja matrix in the house it was built to migrate — the pure decision
core's *behaviour* must not change, even in places where it looks wrong.
`MODELS.md` §6 documents several deliberate choices that read like bugs
(unclamped position values, truncation instead of rounding, a half-open
sun sector, a naive local `World.now`): they exist to match the system
being replaced, bit for bit, across 92,160 scenarios. If you think one of
these is genuinely wrong, that is a conversation for after the migration
gate is retired, not a patch to send now. Read `docs/rationale.md` before
touching anything in the pure core that looks like it could be simplified
or "corrected".

## Running the tests

Two interpreters, and both must be green:

```bash
# Pure decision core — any Python >= 3.12, no Home Assistant needed.
python3 -m pytest tests/ -q

# The Home Assistant integration layer — needs homeassistant==2026.8.0,
# which itself requires Python >= 3.14.2.
.venv/bin/python -m pytest tests/ -q
```

They are not redundant with each other: the system-Python run proves the
decision core has no accidental Home Assistant dependency (enforced by
`tests/test_purity.py`), while the `.venv` run is the only one that
actually exercises `tests/ha/` (the config flow, coordinator, world
builder, and sensor — each gated behind its own
`pytest.importorskip("homeassistant")`, so it silently skips under system
Python instead of failing).

If your change touches `engine.py`, `conditions.py`, or the meaning of
`fixtures/dom_peter.yaml`, also run the migration gate:

```bash
python3 -m pytest tests/parity -q
```

This needs `/config/configuration.yaml` on a real Home Assistant host (see
`MODELS.md` §5 for exactly what it needs and why) — it cannot run in CI,
and `pytest.mark.skipif` quietly skips it anywhere that file is not found.
It is this project's central correctness guarantee; do not treat a skip as
a pass.

Lint and format, both required:

```bash
ruff check .
ruff format --check .
```

## Style

This project lints with **Home Assistant core's own `ruff` `select` list**
(`pyproject.toml`'s `[tool.ruff.lint]`, taken directly from core's
`pyproject.toml` on `dev`), formats with `ruff format`, and documents with
**PEP 257 / Google-style docstrings** (`[tool.ruff.lint.pydocstyle]`
`convention = "google"`) — a one-line summary first, a short body only
where one earns its place. Design rationale and hard-won debugging traps
belong in `docs/rationale.md`, linked back to from the module they concern,
rather than in long inline comments — see that file's own opening
paragraph for why, and follow the same pattern for new modules.

`pyproject.toml`'s `[tool.ruff.lint]` comments explain the handful of rule
families this project deliberately ignores (`C901` and the `PLR09xx` "too
many X" family for readability, `TC001`/`TC003` because this project has no
Home Assistant imports in its pure modules to defer, `TRY300`/`TRY301`
where the surrounding `try`/`except` shape is intentional and already
explained in the function's own docstring) — read those comments before
adding a rule to that ignore list yourself; the bar is "this rule is wrong
for a reason specific to this code", not "this rule is inconvenient".

Tests are exempt from docstring and magic-value rules
(`[tool.ruff.lint.per-file-ignores]`, `"tests/**"`) because this project's
own convention is that a test documents itself by its name (e.g.
`test_ref_truncates_toward_zero_like_jinjas_int_filter`), and test
assertions routinely compare against literal expected values.

## Before you send a change

- Both test commands above are green.
- `ruff check .` and `ruff format --check .` are clean.
- If the change touches decision logic, `tests/parity` is green, run
  locally.
- No `claude.ai` (or any AI-session) URL anywhere in the commit message.
- `fixtures/dom_peter.yaml` is untouched unless the change is genuinely
  keeping it in sync with the real house it describes.
