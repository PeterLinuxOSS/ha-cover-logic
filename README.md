# Cover Logic

Most blind automations are a pile of "if this, then that" rules, and they work
until the day two of them disagree about the same window. Cover Logic replaces
that pile with a single decision: you describe your blinds, the modes your house
can be in, and the rules for each — and one engine decides where every blind
should be, every time, with a record of which rule decided it and why. Because
the decision is data rather than scattered automations, you can read it, test
it, and move it to a different house. If you have ever lost an afternoon to
"why did that blind just close?", this is aimed squarely at you.

It is a Home Assistant custom integration. Nothing is stored outside your
Home Assistant instance and nothing leaves it.

---

## Status

**It decides, and it can now move blinds — but not until you say so.**

The decision engine, the configuration UI, validation, the diagnostic sensor,
the command planner, the safety interlocks and the execution queue are all
built and tested, and the executor is now wired to real `cover.*` services.
It starts in [shadow mode](https://peterlinuxoss.github.io/ha-cover-logic/getting-started/shadow-mode/):
`dry_run` is on by default, so a fresh install decides, plans and logs
everything, and moves nothing until you turn it off.

## Install

Not in the HACS default store yet, so add it as a custom repository:

1. **HACS → ⋮ → Custom repositories**, URL `https://github.com/PeterLinuxOSS/ha-cover-logic`, category **Integration**.
2. Find "Cover Logic" in HACS and download it.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → "Cover Logic"**.

Or without HACS: copy `custom_components/cover_logic/` into your Home Assistant
`custom_components/` directory, restart, then step 4.

See [install](https://peterlinuxoss.github.io/ha-cover-logic/getting-started/install/)
and [first setup](https://peterlinuxoss.github.io/ha-cover-logic/getting-started/first-setup/)
for the four ways to start and a walkthrough of the recommended one.

## Documentation

**https://peterlinuxoss.github.io/ha-cover-logic**

Concepts, how-to guides, the configuration reference and troubleshooting all
live there. [`MODELS.md`](MODELS.md) and [`docs/rationale.md`](docs/rationale.md)
are the technical brief and the design-decision ledger for anyone changing
this code — the site links both under "For developers".

`fixtures/dom_peter.yaml` is not an example — [`docs/example-config.yaml`](docs/example-config.yaml)
is that. `fixtures/dom_peter.yaml` is the real configuration of the house this
was built for, and it doubles as the fixture the migration check compares
against. Do not copy from it.

## Running the tests

```bash
# Decision core, no Home Assistant needed — any Python >= 3.12
python3 -m pytest tests/ -q

# The Home Assistant layer — needs homeassistant==2026.8.0, hence Python >= 3.14.2
.venv/bin/python -m pytest tests/ -q
```

Both must stay green. `MODELS.md` §8 explains what each interpreter actually
exercises.

`tests/parity/` is different and worth knowing about: it renders the live Jinja
template from a real house's `configuration.yaml` and compares it against the
engine across 92,160 scenarios. Those files are not in this repository, so **it
cannot run in CI** — a green CI run says nothing about whether a house still
decides the same way. Run it locally before trusting any change to the decision
logic.

```bash
ruff check .
ruff format --check .
```

## License

MIT — see [`LICENSE`](LICENSE).
