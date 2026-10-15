# `sensor.cover_logic_mode`

The one diagnostic entity this integration creates. Its state is the
currently active mode (`home`, `night`, whatever your configuration names);
its attributes are the full decision behind that mode and how the executor
acted on it. It is `unavailable` only before the first evaluation has ever
completed — after that, a later failing evaluation keeps showing the last
good decision rather than going blank, with the failure surfaced through
`last_error` instead.

**`readiness` and `pending` are both nested objects**, not flat attributes —
if you are searching the entity's attributes for a blind's name or an
entity id, search *inside* `readiness.blocked` and `pending.deferred`/
`pending.queued` as well as at the top level, or you will miss it.

## `targets`

A mapping of every blind's entity id to the action decided for it this
evaluation: `{"position": <0-100 or "keep">, "tilt": <0-100 or "keep">}`.
This is the engine's answer, independent of whether the executor was
actually allowed to act on it — it is populated the same way whether
`dry_run` is on or off, and regardless of what `readiness` says.

## `trace`

A mapping of every blind's entity id to the **label of the rule that decided
it** — for example `night.terrace#0` (zone `terrace`'s own first rule in
mode `night`), `home.*#1` (mode `home`'s shared default list, second rule —
see [rules: inherited defaults](../concepts/rules.md#inherited-defaults)),
or `home.terrace#none` (no rule matched at all, which decides the same thing
as `keep`/`keep`). This is the first thing to read when a blind did
something you did not expect — see
[troubleshooting](../troubleshooting.md).

## `last_error`

The most recent evaluation failure, or `None` if the last evaluation
succeeded. The sensor keeps showing the previous good `targets`/`trace`
while this is set — a failing evaluation never blanks the entity.

## `last_success`

An ISO-8601 UTC timestamp of the last evaluation that completed without
error, or `None` if none ever has.

## `dry_run`

Whether the executor is only logging commands (`true`) or actually issuing
them (`false`). Read live from the config entry's options, so it changes the
instant you flip it on the **Execution** screen — no reload needed. See
[shadow mode](../getting-started/shadow-mode.md).

## `pending`

What is currently waiting, as two independent sub-mappings — a blind can
appear in both at once, since a guard's deferral and an in-flight movement
sequence are unrelated facts about it.

### `pending.deferred`

Entity id → one guard's live wait:

| Field | Meaning |
|---|---|
| `guard` | Which guard (by position in the `guards` list) is holding this. |
| `name` | That guard's `name`. |
| `policy` | Always `defer` — only a `defer` guard produces an entry here. |
| `stage` | The guard's `stage` (`output` or `input`). |
| `state` | `"waiting"` while the deadline has not been reached, or the resolved timeout answer (`proceed`/`abandon`) once it has. |
| `waited` | Seconds elapsed since the wait began. |
| `max_wait` | The guard's configured `max_wait` in seconds, or `null` for "wait indefinitely". |
| `on_timeout` | The guard's configured `proceed`/`abandon`. |
| `recheck_every` | How often (seconds) this wait is re-examined even with no new state change. |

A deferral's clock resets to zero across a Home Assistant restart — see
[guards: `max_wait`, `on_timeout`, `recheck_every`](../concepts/guards.md#max_wait-on_timeout-recheck_every)
for why that is the safe direction rather than a bug.

### `pending.queued`

Entity id → what the executor's runner is doing with it right now, listed
only once a sequence actually has a command queued (an empty sequence is
never listed):

| Field | Meaning |
|---|---|
| `running` | `"<priority>/<source>"` of the sequence currently executing, if any. |
| `seq` | That sequence's own id. |
| `step` | `"<current>/<total>"` position within the sequence (for example a position move followed by a tilt move). |
| `age` | How long the running sequence has been executing. |
| `waiting` | `"<priority>/<source>"` of a second request queued behind the running one, if any. |
| `waiting_age` | How long that second request has been queued. |

This is the one thing a log line cannot show you: what has **not** happened
yet, not what already has.

## `manual_holds`

A sorted list of blind entity ids currently held because
[`manual_detection.hold`](../concepts/manual-moves.md#hold) recognised a
person moving them by hand. A held blind is left alone — its current target
is not re-sent — until the decided target actually changes. See
[troubleshooting](../troubleshooting.md) for what to check when a blind in
this list seems "stuck".

## `last_command`

The single newest thing the executor did, or deliberately did not do. One of
five kinds (`kind` field), each with its own extra fields:

| `kind` | Meaning |
|---|---|
| `would_call` | Dry run: the runner reached a command and did not send it. |
| `called` | Live: the runner reached a command and issued it. |
| `suppressed` | An axis was given a target but produced no command anyway — most often the dead band, or an axis the blind does not have. |
| `withheld` | A guard removed the whole action before the runner ever saw it (`reason`, `policy`, `guard` fields explain which). |
| `dispatched` | The command reached `hass.services.async_call` and returned without raising — written *after* the call, so this is "accepted", where `called` only meant "attempted". |

Every entry also carries `at` (an ISO-8601 UTC timestamp) and, where
relevant, `blind`, `service`, and the command's own payload fields.

## `readiness`

Whether the inputs this decision depends on could actually be read, and
which blinds a missing one is currently blocking. `None` only before the
first evaluation has ever completed.

| Field | Meaning |
|---|---|
| `ready` | `true` if every entity the whole configuration reads is currently readable. |
| `missing` | Entity ids that are absent, `unknown` or `unavailable` and were read by some node with no stated `default:` — truncated to a bounded number, with `missing_count` giving the real total. |
| `missing_count` | The real count behind `missing`, in case the list itself was truncated. |
| `blocked` | Entity id (a blind) → the subset of `missing` that **that blind's own decision** reads — this is the per-blind veto; a blind absent from `blocked` was not stopped by this, however broken the rest of the house's inputs are. |

This is the answer to "it decided something sensible-looking and moved
nothing" — without it, that looks like a bug in the executor rather than a
deliberate refusal to act on an incomplete picture of the world. See
[troubleshooting](../troubleshooting.md#the-blind-never-appears-in-the-log-at-all).

## `matica_mode` and `matica_diff`

A live comparison against a separate, external entity named
`sensor.zaluzie_cielovy_stav` — the Jinja-template matrix this project was
originally built to replace on its author's own house. This is specific to
that one migration and almost certainly does not exist on your
installation.

- `matica_mode` — that entity's own state (its "mode"), or `null` if the
  entity does not exist.
- `matica_diff` — `null` means "nothing to compare against" (the entity is
  absent, or its comparison attribute is missing or unparsable);
  **`[]` means "compared, and they agree"**; a non-empty list names the
  entities the two disagreed about. `null` and `[]` are never
  interchangeable — one means "not checked", the other means "checked, no
  disagreement found".

If you are not migrating from that specific system, treat both as always
`null`/inert.
