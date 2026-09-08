# Open findings for phase 2

From the whole-branch review of phase 1 (2026-08-25). Sixteen findings were
raised; nine were fixed in `f8fc1b5` and `b498678`. These eight remain. Each
was verified by running code, not by inspection.

Phase 1 ships no hardware control, so none of these can move a blind today.
They matter when phase 2 adds the Home Assistant layer.

Each is tracked as an issue: #3, #4, #5, #6, #7, #8, #9, #10 (in the order
below). This file is the durable record — the issues are for working through
them.

---

## 1. Condition bodies are validated by nobody — *important* (#3)

`config_schema._check_keys` deliberately exempts condition bodies: they are
native Home Assistant condition dicts whose key set this project does not own.
`validation.validate()` checks only *ref names*. Between them, nothing checks a
condition body's shape at all.

| config written | `validate()` | `evaluate()` |
|---|---|---|
| `{condition: sate, entity_id: x, state: "on"}` | CLEAN | `ValueError: unknown condition type: 'sate'` |
| `{condition: numeric_state, entity_id: sensor.t, above: 25}` | CLEAN | `KeyError: 'default'` |
| `{condition: state, entity_id: input_boolean.x}` | CLEAN | `KeyError: 'state'` |

Row 2 matters most: that is exactly the shape Home Assistant's own condition UI
produces. The `default` key is this dialect's addition.

**Failure scenario.** A rule is added to `dovolenka.spalna` with a typo'd
condition type. Import is clean, validation reports nothing, the house runs for
weeks. The first time vacation mode and the bedroom zone are evaluated
together, `evaluate()` raises — and per finding 2, every blind loses its
decision, not just that zone.

**Fix.** A `_check_condition_shapes` pass walking the same nodes
`_referenced_condition_names` already walks, asserting known `condition:` values
and the required keys per type.

## 2. One bad rule in one zone destroys every blind's decision — *important* (#4)

`engine.evaluate` iterates zones in a plain loop with no isolation. Any
exception from any rule anywhere — an unknown condition type, a template error,
a ref `KeyError` — propagates out of `evaluate()` and the whole `Decision` is
lost. All ten blinds, not just the offending zone.

This is the blast radius of the deliberate decision that template exceptions
propagate rather than silently evaluating false. That decision is right: a
broken template must not silently mean "no", because "no" can mean leaving the
house open during a heatwave. But the damage should be bounded.

**Fix.** Keep the propagate semantics, add containment: catch at the per-zone
boundary, mark that blind `#error` in the trace, leave it KEEP, decide the rest.
The diagnostic sensor then names the broken zone instead of going blank.

## 3. `sun_hits_target` cannot read the azimuth from an attribute — *important* (#5)

`_sun_hits_target` calls `world.number(azimuth_entity, default=-1.0)` with no
`attribute=` argument, and `DEFAULT_AZIMUTH_ENTITY = "sensor.sun_solar_azimuth"`
is a constant in the engine.

In stock Home Assistant the azimuth is an **attribute of `sun.sun`**.
`sensor.sun_solar_azimuth` exists but is **disabled by default**.

**Failure scenario.** A new installation sets `azimuth_entity: sun.sun` with the
real attribute present and the sun above the horizon. It does not match;
evaluation falls through to the catch-all. No error, nothing pointing at the
cause — the symptom is only "the sun rules never fire".

**Fix.** Accept an `azimuth_attribute` key on the condition and pass it through;
`world.number` already takes an `attribute` argument. That also removes the last
house-specific constant from the engine.

## 4. No time axis: a time-gated mode kills its whole rule set — *important* (#6)

`tests/scenarios.py` holds `NOW` as a module constant (13:00) and `_require`'s
`time` branch explicitly gives up. Verified:

```
rule  {condition: time, after: "22:00", before: "06:00"}  -> dead=['day.z#0']
mode  {id: night, when: <same>}                           -> dead=['night.z#0']
```

Defining a night mode by a clock window is one of the most natural ways to
configure this system. The current house dodges it only because its modes key
off `input_boolean.cover_down`. A user who writes a time-window night mode gets
a red suite on day one, told their rules are unreachable when they are not.

**Fix.** Make `now` an axis, derived from the `after`/`before` values present in
the config (each boundary ± 1 minute).

## 5. No axis for `condition: template` either — *important* (#7)

A rule guarded by `{{ is_state('input_boolean.x','on') }}` gets no axis for
`input_boolean.x` and is reported dead. `derive_axes` understands only `state`
and `numeric_state`; `_require` falls through to `entity_id is None` and raises
`_Infeasible`.

Since `template` is documented as *the* escape hatch, the first user to reach
for it loses all coverage for that rule.

**Fix.** Minimum viable: extract `states(...)` / `is_state(...)` entity names
from the template body. Failing that, at least distinguish "unreachable" from
"not solvable from the axis vocabulary" in the dead-rule message, so nobody is
sent hunting a bug that does not exist.

## 6. The package cannot tell phase 2 which entities to watch — *important* (#8)

There is no entity-enumeration API in `custom_components/cover_logic/`. The only
code that walks the config for entity ids — `_all_condition_nodes` and
`derive_axes` — lives in `tests/scenarios.py`.

Phase 2 needs exactly this to build its trigger list, so it will either
duplicate the walker and drift from it, or snapshot all of `hass.states` on
every event.

**Fix.** Promote it: `config_schema.referenced_entities(config) -> set[str | tuple[str, str]]`,
and have `scenarios.py` import it.

## 7. `World.__post_init__` copies attributes shallowly — *minor* (#9)

The docstring promises the snapshot "cannot be changed from outside", but
`dict(self.attributes)` shares nested values. Home Assistant attributes are
frequently lists and dicts (`hvac_modes`, forecast lists), so in phase 2 those
objects will be shared with `hass`.

Nothing in the engine mutates them, so this is minor today — but the guarantee
as written is stronger than the code delivers. Either weaken the docstring or
deep-copy the values.

## 8. Test-suite gaps — *minor* (#10)

- `test_fixture_has_no_validation_errors` filters to `severity == ERROR`. The
  fixture currently produces zero problems of any severity, so nothing is
  hidden, but no guard keeps it that way. `assert validate(config) == []` would
  lock in the clean state. Do this only after deciding on the known
  `no_catch_all` false positive, or it becomes a tripwire.
- `test_properties.py`'s determinism test passes the *same* `World` object
  twice, testing only that `evaluate` has no internal memo. The stronger version
  already exists as
  `test_engine.py::test_equal_but_separately_built_worlds_give_the_same_decision`,
  so this is a stale duplicate rather than a gap — worth aligning so the weak
  form is not taken as the pattern.

---

## Also worth doing before phase 3

`Decision.trace` is a formatted string (`"noc.spolocne#0 name"`). The diagnostic
sensor will want mode, zone, index and name as separate fields, and
`fired_rules` already has to do `label.split(" ")[0]` to recover them. A small
frozen dataclass now is cheaper than string-parsing in the sensor later.

The comment justifying truncation in `_resolve_value` cites parity with Jinja's
`| int` — a justification that **expires** when phase 3 stops being parity-bound,
at which point someone will reasonably "fix" it to `round()`. Write the durable
reason down now: cover positions are coarse, and truncation is the conservative
direction for a closing blind.
