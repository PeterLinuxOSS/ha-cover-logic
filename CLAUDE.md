# CLAUDE.md

Read **[`MODELS.md`](MODELS.md)** first. It is the single source of truth
for what this project is, how it is built, and the rules for changing it —
architecture, the decision model, the configuration format, the migration
gate, the deliberate decisions that look like bugs, project phase status,
how to run the tests, and the parity-first rule for anyone touching this
code.

This file stays short on purpose: a second copy of that brief would drift
from it, which is exactly the failure this project's own `docs/rationale.md`
exists to avoid for the source code itself.

Three things worth restating because they are easy to violate by habit:

- **English only, everywhere git can see it** — commit messages, branch names,
  PR titles and bodies, code, comments, docs. The owner works in Slovak in
  chat; the repository is public and stays English. (Owner's instruction,
  2026-08-31.)
- **Comments are one-liners.** A comment says what and why in one sentence.
  Long reasoning belongs in `docs/rationale.md` or a ledger, with a pointer
  above the code — not a paragraph. (Owner's instruction, 2026-08-31.)
- **No `claude.ai` session link** in a commit message, here or in any PR to an
  upstream project (see `MODELS.md` §9).
