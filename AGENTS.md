# AGENTS.md

Read **[`MODELS.md`](MODELS.md)** first, in full, before making any change.
It covers what this project is, its architecture (pure decision core vs.
Home Assistant layer, and why the split is enforced by a test), the
decision model, the configuration format, the migration gate that guards
any behavioural change, project phase status, how to run the tests, and
the rules for changing this code.

## Before you touch anything

- Run both test commands in `MODELS.md` §8 and confirm both are green
  before you start, and again before you finish.
- `ruff check .` and `ruff format --check .` must stay clean.
- If your change touches `engine.py`, `conditions.py`, or the meaning of
  `fixtures/dom_peter.yaml`, also run `tests/parity` locally (`MODELS.md`
  §5) — it is this project's central correctness guarantee and does not
  run in CI.
- Do not edit `fixtures/dom_peter.yaml` for documentation or example
  purposes; see `MODELS.md` §9 for why.
- Do not add a `claude.ai` (or any AI-session) URL to a commit message.

This file intentionally does not restate `MODELS.md`'s content — keep any
addition here short, and put anything substantive there instead.
