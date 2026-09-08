# Copilot instructions

Before suggesting or generating code in this repository, read
[`../MODELS.md`](../MODELS.md) in full. It is the single source of truth
for this project: what it is and why, its architecture (a pure decision
core with no Home Assistant import, enforced by `tests/test_purity.py`,
plus a separate Home Assistant layer), the decision model, the
configuration format, the migration gate that guards any behavioural
change, deliberate decisions that look like bugs but are not, current
project phase status, how to run the tests, and the rules for changing
this code.

Key rules to apply when suggesting changes:

- **Parity first, improvements later** — while the migration gate
  (`tests/parity/`) binds, behaviour must not change, even where it looks
  wrong. See `MODELS.md` §6 and §9 before "fixing" anything in the pure
  core (`model.py`, `world.py`, `conditions.py`, `config_schema.py`,
  `engine.py`, `validation.py`, `legacy.py`).
- Do not suggest adding a `homeassistant` import to any of those modules.
- Do not suggest editing `fixtures/dom_peter.yaml` for documentation or
  example purposes.
- Never include a `claude.ai` (or any AI-session) URL in a commit message.

This file is deliberately short; do not duplicate `MODELS.md`'s content
here.
