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

## `planner.py`

### Why the plan is a sequence with an explicit wait, not two service calls

These motors (`supported_features: 191`) *ignore* a tilt command that arrives
while the blind is still travelling down. The working sequence, in the house's
own scripts, is `close_cover` -> wait until the blind reports arrival -> a
short settle pause -> `close_cover_tilt`. A fixed delay is not a substitute: a
full run takes about 55 s, so any delay short enough to be tolerable is short
enough to land mid-travel. This shipped wrong more than once, most recently on
2026-08-29 in the bedroom door automation, where the slats simply never ended
up where they were meant to. That is why `WaitForPosition` is a command in its
own right rather than a `Settle` with a bigger number: only the executor can
know when the blind arrived, and the plan has to be able to say "here".

`Settle` still exists on top of the wait because these motors report position 0
slightly *before* they physically stop, so a tilt fired the instant the state
arrives can still be discarded. The house's scripts wait 2 s; so does
`SETTLE_SECONDS`.

`blind.tilt_after_arrival` is what turns the wait off. A blind that does not
need it gets `SetPosition` followed straight by `SetTilt`, with no wait
between them -- that is the whole meaning of the flag.

### Why the dead band is five points, and why the wait uses the same number

Idempotence here is a safety requirement, not an optimisation. The matrix
recomputes on every weather update, roughly every ten minutes; without a skip,
each recompute would push commands at ten motors.

Equality is not enough for that skip, because a repeated *absolute* command is
itself a movement. The motor changes the slat angle **by moving**, so closing
the slats shifts the reported position: the kitchen blinds drift from 34 to
29-30 on their own, and re-sending 34 makes them visibly jump. `Lighting SUN`
did exactly that twice in one evening on 2026-08-27, from two different
branches, both sending `input_number.kvety_pozicia_zaluzie` verbatim.

Five is the number the house's own scripts settled on rather than a round
guess: the living-room terrace blind seats at 3%, and with a tighter band every
run considered it "not closed yet" and drove it again (2026-08-06). The same
five points also covers `set_cover_tilt_position: 100` landing on 99 on these
motors.

`WaitForPosition.tolerance` is deliberately the same constant. `scripts.yaml`
spells out why in its own comment ("Prahy su ZAMERNE zhodne"): if the threshold
that decides "close enough, do not send the command" and the threshold that
decides "it has arrived" ever differ, a blind can be simultaneously close
enough to be skipped and never close enough to finish.

### Why a clamp is reported on the `Plan`, not on the command

The engine deliberately does not clamp (see "Why the engine does not clamp
resolved values to 0..100" above) -- clamping there would break parity with the
Jinja template, and parity is the project's central guarantee. Clamping is
hardware safety, not decision fidelity, so it belongs here.

A clamp must be visible, though: a blind that was told -5 is a fact, not a
detail. This module is pure and cannot log through Home Assistant, so it
follows the precedent the pure core already sets -- `validation.Problem` and
`Decision.trace` both return the explanation *alongside* the answer, in the
same value, instead of writing it somewhere. `Plan.clamps` is that same shape.

It is a separate field rather than an attribute of the command it belongs to
because a clamp can outlive its command: a blind already at 100 that is told
105 clamps to 100 and then emits nothing at all. That is precisely the case
worth surfacing -- something in the configuration is producing an impossible
number, and the only evidence is the clamp. Hanging it off a command would
throw it away exactly when it matters.

### Why there is no top threshold (and why there was one)

For two commits this module had a `TOP_THRESHOLD = 95` and skipped the tilt
command for any blind that would end up at or above it, on the reasoning that
the angle is only ever set by movement, so from the fully open position the
tilt cannot be set at all. That is a deliberate reversal, and this section is
the reversal, not a tidy-up of it.

**The hardware claim may well be true.** The house's own memory says it
outright (`zaluzie-motor-uhol-pohybom`: "z hornej polohy sa uhol nastavit
nedá"), and nothing here contradicts it. The problem is what follows from it.
If it is true, then the motor is the thing that ignores the command, and the
house has been sending that harmless no-op for years: `/config/scripts.yaml`'s
tilt filters -- `tilt100_f`, `tilt50_f`, `zavriet_t0_f`, `zavriet_t50_f`,
`zavriet_t100_f`, `pozicia_tilt_f` -- read `current_tilt_position` and
*nothing else*; not one of them looks at `current_position`. And
`zaluzie_otvorit` drives to 100, waits for arrival, and *then* sends
`open_cover_tilt` three times over, to every target, including the blinds
that have just reached the top. Encoding the skip here therefore does not
reproduce the house's behaviour, it diverges from it: `Action(KEEP, 50)`
against a blind reporting 97 made this module emit nothing while the house
sends `set_cover_tilt_position: 50`. "Parity first, improvements later"
(MODELS.md §9) points squarely at removing it, and the migration gate cannot
referee the question -- it compares *decisions*, and this band lives entirely
below them.

The skip also had a cost that was not theoretical. It let a tolerance on one
axis silently kill the command on the *other*:
`plan(blind, 99, 100, Action(position=94, tilt=0))` returned an empty plan.
The dead band skipped the position (|99-94| is 5, not >5), so the "where will
it end up" position fell back to the current 99, which read as "at the top",
which killed the tilt as collateral -- a blind nowhere near either target
receiving no command at all, and `Plan`'s own docstring ("empty means already
where it should be") made into a lie. Nothing corrects it afterwards:
`config_schema.referenced_entities(fixtures/dom_peter.yaml)` contains no
`cover.*` entity, so a blind's own state change triggers no recompute. That
is the same failure class as an `if` in `automations.yaml` that ANDs "what
triggered this" with "what state it is in" and thereby mutes branches that
had nothing to do with the state.

So the tilt axis is now decided by the tilt axis alone: the axis is set (not
`KEEP`) and the reported angle is outside the dead band. What settles the
hardware question is the live `dry_run` day, not this file -- and if it turns
out the motor really does ignore a tilt near the top, the correct place to
learn that is from a real blind, with the house's own no-op as the baseline.

`None` on either axis is deliberately not treated as any particular value.
The safe direction throughout the house's scripts is to send a command rather
than silently drop it when an attribute cannot be read ("Bezpecny smer je
prikaz radsej poslat nez ho ticho zahodit"), so an unknown current value
means the command goes out.

### Why an unresolved `Ref` raises instead of being ignored

`engine._resolve_action` resolves every `Ref` before a `Decision` is built, so
one reaching this layer means a caller skipped the engine. Resolving it here is
impossible -- that needs a `World`, which the planner does not have and should
never grow. Treating it as `KEEP` would turn a wiring mistake into a blind that
silently never moves, which is the failure this project keeps finding in the
system it replaces.

It raises on *either* axis of *any* blind, including the tilt axis of a
`has_tilt: false` one, where no tilt command could be emitted anyway. It did
not, once: the tilt axis was simply not resolved at all for such a blind, so
the identical mistake raised on the position axis and vanished on the tilt
axis. A guarantee that holds or not depending on the blind's capabilities is
not a guarantee. The two facts are deliberately kept apart in `plan()`: the
`Ref` raises, while an out-of-range *integer* on that same unreachable axis
stays silent (below).

### Why `bool` is rejected rather than clamped

`bool` is a subclass of `int`, so `max(0, min(100, True))` returns `True`
itself -- the clamp comparison `applied != value` is then False and
`SetPosition(entity, True)` goes out with no `Clamp` to show for it.
`config_schema._parse_axis` already guards this at the config door, with its
own comment about `position: true` silently becoming 1. Neither configuration
door can produce a bool axis today, so this is not a live bug; `plan()` is a
public function of a pure module and this hardens its own contract one layer
closer to the motor, where the failure would be a blind at 1%.

### What this module deliberately does not decide

Which Home Assistant service realises a command is the runner's business, not
the planner's, even though the house knows the mapping (position 0 is
`close_cover`, 100 is `open_cover`, tilt 100 is `open_cover_tilt` because
`set_cover_tilt_position: 100` lands on 99 on these motors -- documented
2026-07-29). Keeping service names out of a pure module is what lets the whole
sequence be tested without Home Assistant; putting them in would buy nothing,
since `SetPosition(entity, 0)` already carries everything the choice needs.

Retrying a discarded tilt (the house's scripts repeat it up to three times) is
also not here. A retry is a reaction to what the cover did after the command
was sent, and this module never observes anything -- it is handed a single
snapshot and returns a sequence. That belongs to the executor.

A blind with `has_tilt: false` that is nonetheless given a tilt value is
dropped silently rather than reported -- including an out-of-range one, which
produces no `Clamp`: `validation.py` is where a configuration mistake belongs,
and a per-recompute report of a standing config error would be noise on every
evaluation. (An unresolved `Ref` on that axis is not a configuration mistake
and does still raise -- see above.)

Whether a blind's `travel_time` is a usable number is likewise not decided
here, for the same reason, even though this module is where the consequence
lands: `travel_time: 0` yields `WaitForPosition(..., timeout=0.0)`, an arrival
wait that expires instantly, a `Settle(2.0)` against a ~55 s run, and a tilt
discarded mid-travel -- precisely what this module exists to prevent. That is
a standing configuration error, so `validation._check_blinds` reports it once,
statically, and `subentry_flow`'s own `travel_time` selector will not offer a
non-positive value in the first place. `plan()` does not second-guess the
number it is handed.

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
