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

One option stands between the two: **`dry_run`, which is on by default.** While
it is on, every command is decided, planned, queued and logged — and nothing is
sent. So a fresh install still moves nothing. You install it alongside whatever
already controls your blinds, watch what it *would* do, compare, and turn
`dry_run` off (Configure → **Execution**) when the log stops surprising you.
That is the intended way to start.

The author's own house has been running it in that shadow mode since August 2026,
with 122 configuration entries, checked against the 367-line Jinja template it is
replacing across 92,160 scenarios on every change.

## Why it exists

The author's blinds were run by a 367-line Jinja template plus roughly 1,500
lines of YAML scripts. It worked — and none of it could move to a different
house without being rewritten from scratch, because the house was baked into
every line. This project turns the same logic into data: the same decisions, but
as a configuration you can carry, diff, and hand to someone else.

## Install

Not in the HACS default store yet, so add it as a custom repository:

1. **HACS → ⋮ → Custom repositories**, URL `https://github.com/PeterLinuxOSS/ha-cover-logic`, category **Integration**.
2. Find "Cover Logic" in HACS and download it.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → "Cover Logic"**.

Or without HACS: copy `custom_components/cover_logic/` into your Home Assistant
`custom_components/` directory, restart, then step 4.

Setup then asks how you want to start:

| | |
|---|---|
| **Set up blinds now** *(recommended)* | Pick your cover entities, say which way each window faces, and it builds a working configuration: shade toward the sun during the day, leave everything alone at night. It shows you what it is about to create before creating it. |
| **Load a configuration from a YAML file** | You already have a file. |
| **Start from the example** | Imports `docs/example-config.yaml`, a complete worked example for an invented house — a plausible starting point rather than an empty one. |
| **Start empty** | Nothing at all. You build it by clicking. |

## Configuring it

Two routes to the same place. Neither is second-class.

**By clicking.** Every part of the configuration — blinds, zones, named values,
named conditions, modes and rules — is a subentry you add and edit in the
integration's own UI. No YAML required, and the UI refuses to save a
configuration it cannot explain.

**By writing YAML.** Write the file, then call the `cover_logic.import_config`
service with its path. `dry_run: true` reports what would change without
changing it; `overwrite: true` replaces the current configuration instead of
merging. `cover_logic.export_config` writes the current configuration back out,
so you can move between the two routes freely.

**Or have a model write it for you.** The YAML format is documented for exactly
this: point an LLM at [`MODELS.md`](MODELS.md) §4 and
[`docs/example-config.yaml`](docs/example-config.yaml), describe your house in
plain language, and import the result with `dry_run: true` first. `MODELS.md` is
written to be read by a model, not just by a person — that is why it states the
traps and the deliberate decisions rather than only the syntax.

## How it decides

Four ideas, and that is the whole model:

- **Blind** — one cover, plus what it can do (tilt or not, how long a full run takes) and which way its window faces.
- **Zone** — a group of blinds that should behave alike.
- **Mode** — a state the whole house can be in (a normal day, night, a heatwave, away). Exactly one is active; the first mode whose condition holds wins, and the last one has no condition so there is always an answer.
- **Rule** — within a mode and zone, the first rule whose condition holds sets the position and tilt. Rule order is meaning, not presentation.

A rule can leave an axis alone rather than setting it, and a zone can inherit a
mode's default rules instead of repeating them. Both exist because the author's
own configuration had 86 rules with only 44 distinct bodies before they did.

**Interlocks** (`guards:`) sit outside that: they can drop a blind from
consideration before the engine is asked, or override the decision afterwards.
They express the things that are not really decisions — do not lower this blind
onto an open terrace door, open everything when the wind gets up, wait until the
sauna is finished. Like rules, the first matching interlock wins.

## What you get to look at

One diagnostic sensor, `sensor.cover_logic_mode`. Its state is the active mode;
its attributes carry the decided action for every blind, which rule produced it,
what is queued or waiting on an interlock, what the last command was — and, while
you are still running an old system alongside, a direct comparison against it.

## Documentation

| | |
|---|---|
| [`MODELS.md`](MODELS.md) | The full technical brief: architecture, the decision model, the configuration format, the deliberate decisions that look like bugs, and the rules for changing this code. **Read this before any nontrivial change.** |
| [`docs/example-config.yaml`](docs/example-config.yaml) | A complete, commented configuration for an invented house. |
| [`docs/rationale.md`](docs/rationale.md) | Why the pure core is the way it is, module by module — and the debugging traps. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to run the tests and the parity-first rule. |

`fixtures/dom_peter.yaml` is not an example. It is the real configuration of the
house this was built for, and it doubles as the fixture the migration check
compares against. Do not copy from it.

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
