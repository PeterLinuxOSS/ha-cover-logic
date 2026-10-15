# Never close a blind onto an open door

A blind mounted over a door — a terrace, a balcony — must never be driven
down while that door is open, and should close itself once the door shuts
again, even if nobody is there to trigger a recompute. This is the two-guard
pattern covered in depth in [guards](../concepts/guards.md); this page is the
practical recipe for one specific, common case.

```yaml title="config"
blinds:
  - entity: cover.terrace_door
    facade_azimuth: 180
zones:
  terrace:
    members: [cover.terrace_door]
conditions:
  terrace_door_open:
    condition: state
    entity_id: binary_sensor.terrace_door
    state: "on"
modes:
  - id: home
rules:
  home.terrace:
    - then: {position: 0, tilt: 0} # this zone's normal rule: closed
guards:
  - name: never lower the terrace blind onto an open door
    policy: skip
    applies_to: closing
    targets: [cover.terrace_door]
    when: !ref terrace_door_open
  - name: close it once the door shuts, up to 90 minutes later
    policy: defer
    applies_to: closing
    targets: [terrace]
    when: !ref terrace_door_open
    max_wait: 5400
    on_timeout: abandon
    recheck_every: 300
```

## Why two guards, not one

A single guard cannot both refuse to close *and* remember to close later —
`skip` and `defer` are different policies with different meanings, and
`guards.py` deliberately has no clock of its own (see
[guards](../concepts/guards.md#max_wait-on_timeout-recheck_every)), so a wait
has to be its own guard, explicitly configured with a deadline. Order
matters: the `skip` guard runs first and matches immediately, so the `defer`
guard beneath it only ever gets a turn once the door has actually closed and
the engine still wants the blind down.

## Choosing `max_wait` and `on_timeout`

`max_wait: 5400` (90 minutes) is a judgement call, not a default — how long
is it reasonable to leave the house looking like the blind "forgot" to
close? `on_timeout: abandon` means: if the door is still open after that
long, give up and leave the blind alone rather than forcing a closed door
check later — `on_timeout: proceed` would instead close it onto the door
once the deadline passes, which is rarely what you want for a door
specifically. The two are opposites and there is no default; see
[guards](../concepts/guards.md#policies) for why.

Use `max_wait: null` instead of a number if you want the wait to never give
up — the blind closes the moment the door shuts, however long that takes,
with no timeout branch to configure at all.

## Watching it work

While a door is open, the [diagnostic sensor](../reference/sensor.md)'s
`pending.deferred` attribute names the blind, which guard is holding it, how
long it has waited, and what happens if the wait runs out — this is the
place to look if a blind over a door seems "stuck" rather than reading logs.
See [troubleshooting](../troubleshooting.md#the-blind-is-waiting-and-did-not-move)
for the full walkthrough.

## Restart safety

Nothing above needs a watchdog script. `recheck_every` (300s here) is what
makes the wait re-examine itself periodically even with no new state change,
and because a deferral is re-derived from `(config, world)` on every
evaluation rather than tracked as an in-flight timer, a Home Assistant
restart cannot cancel it — the first evaluation after startup re-creates the
same wait from scratch. The only cost is the elapsed time resetting to zero,
which only ever makes the blind close *later*, never earlier or onto a door
that is still open.

## Common mistakes

- Giving the `skip` guard the full `targets: [terrace]` (the zone) instead
  of the one blind — harmless here, but worth being deliberate about:
  `targets` can mix entity ids and zone ids, and a zone target affects every
  blind it owns.
- Setting `applies_to: any` instead of `closing` — this would also block the
  blind from *opening* while the door is open, which is never the hazard a
  door interlock exists for.
- Forgetting `on_timeout` on the `defer` guard — there is no default, and a
  config that omits it is rejected rather than silently picking one.
