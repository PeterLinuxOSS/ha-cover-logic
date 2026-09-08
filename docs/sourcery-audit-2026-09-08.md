# Sourcery whole-repository review, 2026-09-08

Reviewed as PRs #70-#76 (the repository split by layer, since Sourcery caps a
review at 500 000 diff characters and the whole-repo PR #12 is ~2 000 000).

19 findings. Triaged below against the code; the three false positives are
recorded with their evidence so nobody re-litigates them.

## Real, worth fixing

| # | Where | Defect |
|---|---|---|
| 1 | `coordinator.py:527` | `build_world` sits **before** the `try`, so a snapshot failure escapes without setting `last_error`, notifying listeners, or re-arming the timer. The `finally` added earlier the same day does not cover it -- same class as the bug it fixed. |
| 2 | `sensor.py:248` | `matica_diff` iterates only entities the legacy matrix names, so a target the matrix omits is never compared and parity is reported falsely. This is the primary post-deploy check, so it weakens its own evidence. |
| 3 | `tests/parity/test_settle_bound.py:221` | The gate enforcing "settle must outlast the house's `for:`" iterates `trigger:` assuming a list. HA also allows a single mapping, whose **keys** are then iterated and skipped, so those automations pass the gate unreviewed. |
| 4 | `tests/ha/test_subentry_flows.py:2794` | Seeds a broken guard into `entry.data["guards"]`, but version 3 reads guard **subentries**, so `config_from_subentries` sees no broken guard. The test passes vacuously. |
| 5 | `config_schema.py:177` | `yaml.load` raises `yaml.YAMLError`, not `ConfigError`, while the docstring promises "`ConfigError` on any problem" and every caller catches only `ConfigError`/`OSError`. Malformed YAML surfaces as an unhandled exception. |
| 6 | `conditions.py:269` | `today_at` zeroes the seconds that `parse_hhmm` was deliberately changed to honour ("must not be silently truncated to the minute"). Self-contradictory. |
| 7 | `config_store.py:215` | Modes with equal `order` fall back to subentry storage order, so which mode wins is nondeterministic. Rules and guards both have a duplicate-order check; modes do not. |
| 8 | `config_store.py:10` | Module docstring still says guards live in `entry.data["guards"]`; the version-3 migration moved them to subentries. |
| 9 | `tests/parity/jinja_bridge.py:27` | Imports `matica` before `available()`/`skipif` can act, so a checkout without `/config/tests/matica.py` fails collection instead of skipping. |
| 10 | `tests/scenarios.py:391` | Witness worlds carry no `SunTimes`, so a rule needing `condition: sun` can never be satisfied and is skipped as infeasible or called dead. |
| 11 | `tests/scenarios.py:217` | `pairwise` returns partial coverage when the greedy pass stalls, silently breaking the documented guarantee. |
| 12 | `tests/scenarios.py:475` | `_require` treats `event_targets_zone` as satisfied without honouring `want_true`, so negating an earlier event rule is a no-op. |
| 13 | `.github/workflows/hacs.yml:25` | HACS and hassfest actions run from mutable `main`/`master` while the comments claim they are pinned. |
| 14 | `pyproject.toml:20` | The `ha` extra is unpinned, so the HA job tests whatever release is current rather than the documented one. |
| 15 | `.github/workflows/claude-review.yml:144` | Allowlists `mcp__github_inline_comment__create_inline_comment` with no MCP server configured, so the promised inline comments cannot be created. |
| 16 | `tests/ha/test_settle.py:394` | Asserts a deterministic outcome at an *equal* deadline, so scheduler ordering decides it. |
| 17 | `tests/ha/test_init.py:140` | Setup tests never unload, abandoning the coordinator's subscription and timers. |
| 18 | `tests/ha/test_options_flow.py:1284` | The translation guard checks a hand-maintained step list, so a new step can render untranslated and still pass. |

## Deliberately left open, with the diagnosis

**#10, #11, #12 -- `tests/scenarios.py`.** Attempted and reverted the same
day, because the attempt made `test_every_rule_fires_at_least_once` fail on
`horucava.kvety#1`, and that failure is a real design gap rather than a slip.

The attempt varied the sky over four fixed `SunTimes` probes on an hours-scale
spread, on the stated ground that "the offsets a real config uses -- minutes
-- cannot flip which of the four a clause lands in". That premise does not
hold for a witness that must satisfy **two** sun clauses at once with opposite
truth values. `vecer` asks for `after: sunset` with `after_offset: -1200` and
`je_noc` for `after: sunset` with `before_offset: -1260`; those windows
overlap almost entirely, so telling them apart needs the sun placed to the
minute. The solver pins the sky for the first clause it solves and then cannot
satisfy the negation of the second:

```
_Infeasible: no sun probe makes {'condition': 'sun', 'before': 'sunrise',
'before_offset': -1260, 'after': 'sunset'} answer False
(pinned sun: SunTimes(sunrise=... 6:00, sunset=... 11:00))
```

Two things a correct version needs, both absent from the attempt:

1. **Probes derived from the configuration, not fixed constants.** This file
   already does exactly that for facades -- `DEFAULT_AZIMUTH_PROBES` is only
   the fallback "when a configuration declares no facade at all", and real
   probes are derived per config. Sun probes have to come from the offsets the
   configuration actually contains, so every boundary a clause can distinguish
   is representable.
2. **The sky as a backtrackable choice point,** like any other axis. Pinning
   it first-wins and raising when a later clause disagrees is what produced
   the failure above.

Reverting was the safe call, not the lazy one: `scenarios.py` derives the
space that `test_coverage_rules.py` and the 92 160-scenario migration gate
both stand on, and a half-wired version of it is precisely the "green signal
that did not measure what you think" this project has already paid for twice.

**#16, #17, #18 -- three test-hygiene items, not started.** `test_settle.py`
asserting a deterministic outcome at an equal deadline (a flaky test, and the
one whose subject is the settle boundary, so it deserves a controlled clock
rather than a wider gap); `test_init.py` never unloading what it sets up; and
the options-flow translation guard trusting a hand-maintained step list. None
of them can produce a wrong decision in the house -- they weaken tests rather
than behaviour -- which is why they are last, not why they do not matter.

**#7's warning half.** The nondeterminism is fixed -- `_ordered_modes` sorts by
`(order, subentry id)`, so a storage round-trip can no longer change which
mode wins, and a mutation test pins it. What is *not* added is a
`duplicate_mode_order` **warning**, which rules and guards both have. Wiring
one needs `validation.py`, `config_store.py`, `options_flow.py` and
`subentry_flow.py` to move together, and the defect itself is already gone, so
what remains is a consistency gap rather than a bug. The house is unaffected
either way: its mode orders are 0, 10, 20, 30.

## Confirms findings already known and documented

Two of the three smaller items from the 2026-09-07 audit, found independently:

- `ha_world.py:61` -- a `state` condition with both `attribute:` and `for:`
  never gets `last_changed` snapshotted, so `_held_long_enough` returns `True`
  and the debounce is ignored. Fails **open**.
- `runner.py:328` -- a carried-over tilt from a cancelled sequence is appended
  after the successor's commands with no `WaitForPosition`, so the tilt is
  issued mid-travel and these motors discard it. Reported twice.

## False positives, with the evidence

- **`capabilities.py:44`, "`SET_TILT_POSITION` should be 64."** No: Home
  Assistant's `CoverEntityFeature.SET_TILT_POSITION` **is** 128 and `STOP_TILT`
  is 64 (checked against the installed package). The house's blinds report
  `supported_features: 191` = 1+2+4+8+16+32+128, which only adds up with 128.
- **`readiness.py:204`, "`None` is not treated as unready."** It is:
  `const.UNREADY_STATES` is `frozenset({None, "unknown", "unavailable"})`.
  Sourcery could not see `const.py` because the split put it in another PR.
- **`boundaries.py:52`, "`config_schema` is absent from the package."** Split
  artifact, same reason.

The last two are the cost of splitting the repository to fit the review limit:
a reviewer that cannot see a module cannot see a constant defined in it. Worth
remembering before trusting a cross-module claim from these PRs.
