# Rationale

Design decisions and hard-won debugging traps that used to live inline as
long docstrings or comment blocks. They are collected here, organised by
module, so the source can follow PEP 257 / Google-style docstrings (a
one-line summary first, a short body only where needed) without losing the
"why" -- several of these paragraphs record traps that already cost real
debugging time once. Every place this content moved from carries a short
pointer back to its section here.

Nothing below is new: each section is the original comment or docstring text,
moved rather than rewritten. Where a decision's reasoning previously lived
only in a test's comment (not in the production module itself), that is
noted in the section.

## `world.py`

### Why `World` takes a defensive copy

`frozen=True` stops the fields being re-assigned; it does nothing about a
caller mutating the dict it passed in. This module exists to guarantee that
one evaluation sees one consistent state, so that guarantee is enforced here
rather than left to callers.

## `conditions.py`

### Why `_ref_chain` must be threaded through every recursive call

`_ref_chain` is private: it tracks the names of `ref` conditions currently
being resolved, so a `ref` cycle raises a clear error instead of recursing
until Python's stack limit. Every recursive call in `evaluate_condition` must
thread it through, or the cycle guard silently stops working for that branch.

### Why `numeric_state` requires an explicit `default`

`default` mirrors Jinja's `| float(999)` fallback. A dead sensor must fall on
the safe side, and which side that is depends on the rule -- so the default
is always explicit in the config, never implied.

### Why the sun sector is half-open `[facade-tolerance, facade+tolerance)`

The template being replaced used `az >= 45 and az < 135`, and the scenario
axis contains 45/135/225/315 on purpose, so an inclusive upper bound breaks
parity on exactly those points.

### Why autoescape stays off

CodeQL's `py/jinja2/autoescape-false` is a false positive here: that rule
assumes the output reaches a browser. This environment renders one thing
only -- a boolean guard from the operator's own configuration -- and the
result is compared against a fixed set of truthy strings and discarded. No
HTML is produced, nothing is served, and the template author is the system
operator, so there is no privilege boundary to cross. Escaping would only
alter `<`, `>` and `&`, which a boolean expression does not contain.

### Why a broken template raises instead of evaluating False

StrictUndefined is deliberate: an undefined name must raise rather than
render empty, because an empty render would read as False, and False here
can mean "leave the house open during a heatwave". A broken user template
must not silently evaluate to False for the same reason -- "false" can mean
"leave the house open during a heatwave". This must never be wrapped in a
`try`/`except`: a future change that swallows the error would fail silently
in exactly the situation the strictness exists to catch.

### Why the wrap-around time window is an OR, not an AND

For a same-day window (e.g. 08:00-18:00), `after <= now < before` is correct.
For a wrap-around window (e.g. 22:00-06:00), `after` is later in the clock
than `before`, so the intended window crosses midnight. ANDing the two
one-sided checks (as a naive port of the native HA schema would) is wrong
here -- it always yields an empty set, since no time is both >= 22:00 and <
06:00 on the same clock face. The window is everything from `after` to
midnight PLUS everything from midnight to `before`, i.e. an OR of the two
checks.

## `config_schema.py`

### Why a mode or zone id must not contain a dot

`engine.evaluate` builds a rule key as `f"{mode}.{zone}"`, joining the two
with a plain string concatenation; `validation._check_rule_keys` recovers
them with `key.partition(".")`, which always splits on the FIRST dot. If
either id itself contains a dot, that join is not reversible: e.g. mode
"a.b" + zone "c" and mode "a" + zone "b.c" both produce the identical key
"a.b.c", so a rules entry meant for one pair silently applies to the other --
and `_check_rule_keys` accepts it, because splitting on the first dot happens
to name two ids that both exist. Rejecting the dot at parse time is cheaper
than making the join unambiguous.

### Why condition bodies and `guards` are exempt from strict key checking

`_check_keys` is only ever called on structures this module owns the schema
of: blind, zone, mode, rule, value and action entries, plus the top level.
It is never called on a condition body (native HA condition schema, not
ours) or on `guards` (unparsed until a later phase).

### Why a `!ref` default is range-checked exactly like a literal

A `default` is a config-time constant exactly like a literal action axis, so
it must pass the same 0..100 range check `_parse_axis` applies to literals --
otherwise `position: !ref` with an out-of-range fallback validates clean
while the same value written literally as `position: 250` would raise. This
cannot affect parity: parity depends on the helper's runtime value, never on
the fallback used when it is missing or unparsable.

## `engine.py`

### Why `EngineError` must propagate uncontained out of `_evaluate_zone`

`_evaluate_zone` decides every blind in one zone, contained: a broken rule
anywhere in this zone must not lose the decision for every other zone's
blinds. `EngineError` is deliberately let through uncontained -- both the one
raised inside the loop (a zone naming a blind the config does not have) and
any raised earlier by mode resolution or the ownership check before this
function is ever called. Those mean there is no valid decision to make at
all, not that one zone's rules misbehaved, so masking them as keep/keep would
hide a config that cannot be evaluated behind a decision that looks normal.
Only a failure from evaluating this zone's *rules* -- everything that is not
an `EngineError` -- is contained here.

### Why the `#none` trace label is ambiguous on purpose

The trace label `#none` is deliberately ambiguous between two causes: no
rules were configured at all for this (mode, zone) key, and rules were
configured but none of them matched this blind. Both end in "keep, keep" and
both are represented the same way. Debugging a `#none` means checking for
either cause -- first whether `mode.zone` exists in `config.rules` at all,
then, if it does, why every rule in it fell through.

### Why `_resolve_value` truncates instead of rounding

This reasoning previously lived only in `tests/test_engine.py`
(`test_ref_truncates_toward_zero_like_jinjas_int_filter`), not in the
production module. Parity-critical: the Jinja template this engine replaces
uses `| int(34)`, which truncates toward zero, not `round()`. 50.7 must
resolve to 50. This must NOT be "improved" to rounding -- that would silently
break parity against the live system across a whole scenario class.

The durable reason, beyond parity: cover positions are coarse, and truncation
is the conservative direction for a closing blind.

### Why the engine does not clamp resolved values to 0..100

This reasoning previously lived only in `tests/test_engine.py`
(`test_ref_value_above_100_passes_through_unclamped`,
`test_ref_negative_value_passes_through_unclamped`) and
`tests/test_properties.py` (`test_action_axes_are_always_an_int_or_keep`),
not in the production module. Deliberate: the engine does not clamp
out-of-range helper values, because the template being replaced does not
clamp either -- clamping here would itself break parity. Clamping belongs in
the execution layer that issues the hardware call, where it is a safety
concern, not a decision-fidelity one. Helpers really do drift outside their
intended range (see the root `CLAUDE.md` on `float(8)` defaults vs. actual
helper state after a restart), so this is pinning real behaviour, not a
hypothetical. The only honest guarantee at this layer is "KEEP or an int".

Note this is a different decision from `config_schema._parse_axis`, which
*does* range-check a literal or a `!ref` default at config-parse time (see
"Why a `!ref` default is range-checked exactly like a literal" above) --
config-time constants are checked because they are the config author's
mistake to catch early; a helper's live runtime value is not, and parity
depends on it flowing through unchanged.

## `validation.py`

### Why `_find_cycle_from` is iterative, not recursive

Iterative (an explicit stack) rather than recursive so that a long-but-legal
reference chain is bounded by heap, not by the interpreter's frame limit -- a
config with a few thousand chained conditions must still validate, not
crash.

`visited` marks every node whose references have been (or are being)
explored; `on_path` is the subset of `visited` currently on the DFS path from
`start_name`. A reference into a visited-but-not-on-path node is a legal
cross-edge (e.g. the shared bottom of a diamond); a reference into a node
that is on the current path is a back-edge, i.e. a cycle. Each stack frame
pairs a node with an iterator over its outgoing references, so resuming a
frame after a child is fully explored is a plain `next()` on that same
iterator rather than a recursive call.

### Why `_walk_condition_nodes` is the single traversal

`node` is a condition body (a dict), a bare list of conditions (this
dialect's list-as-AND shorthand), or None. It recurses into the
`conditions:` list of an `and`/`or`/`not` combinator. This is the single
traversal behind every check that needs to visit each condition dict in a
config once -- the circular-ref check, the unknown-ref check, and the
condition-shape check (issue #3) all read from it instead of each re-walking
the tree its own way.

### Why `_check_unknown_condition_refs` exists despite YAML-time checking

The YAML parser only catches an unknown condition ref for refs written with
the `!ref` tag. A hand-written literal `{condition: ref, name: ...}` dict
passes `load_config` unchecked -- condition bodies are deliberately exempt
from strict key checking (see `config_schema._check_keys`) -- and would
otherwise surface as a bare unhandled `KeyError` deep inside `conditions.py`
at evaluation time instead of a validation report.

### Why `_check_condition_shape` only checks known types and required keys

It checks one condition dict's own shape, not its children (the caller walks
those separately). Only unknown types and missing *required* keys are
reported; extra keys this dialect does not know about (`alias`, `enabled`,
whatever Home Assistant adds next) are deliberately ignored, since condition
bodies are native Home Assistant dicts this project does not own the full
schema of.

### Why `_check_condition_shapes` exists as a separate check

`config_schema._check_keys` deliberately exempts condition bodies from strict
key checking (they are native Home Assistant condition dicts, a schema this
project does not own), and `_check_unknown_condition_refs` only checks ref
*names*. Nothing else validates a condition body's shape -- so an unknown
`condition:` value or a missing required key would otherwise pass
`validate()` clean and only surface as a bare `ValueError` or `KeyError`
deep inside `conditions.py` at evaluation time, far from the config that
caused it.

## `model.py`

### Why `Config` is frozen without `slots` (unresolved)

Every other frozen dataclass in this module (`Ref`, `Action`, `Blind`,
`Zone`, `Mode`, `Rule`) is declared `@dataclass(frozen=True, slots=True)`.
`Config` and `World` (in `world.py`) are the two exceptions -- both are
`@dataclass(frozen=True)` without `slots`. No comment, docstring, test, or
commit message in this repository's history explains why. A working test
(adding `slots=True` to both and running the full suite) shows no functional
or performance obstacle in this codebase as it stands today. This entry
records that the omission was investigated and is being left exactly as
found, rather than "fixed" or given a fabricated justification -- if the
original reason resurfaces, it belongs here.
