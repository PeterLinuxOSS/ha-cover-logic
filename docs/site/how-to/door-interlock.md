# Never close a blind onto an open door

A blind mounted over a door — a terrace, a balcony — must never be driven
down while that door is open, and should close itself once the door shuts
again. One `defer` guard does both; this page is the practical recipe for
that common case, and [guards](../concepts/guards.md) covers the mechanism
in depth.

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
  - name: do not lower the terrace blind while the door is open
    policy: defer
    applies_to: closing
    targets: [terrace]
    when: !ref terrace_door_open
    max_wait: 10800
    on_timeout: abandon
    recheck_every: 900
```

## How the one guard does both jobs

While the door is open and the rule wants the blind lower than it is now,
the guard fires and the blind is held: no command is sent. Holding is the
"never onto an open door" half.

The guard is re-evaluated on every recompute, and the door sensor is one of
the entities the integration watches, so the door shutting triggers a
recompute by itself. At that moment the guard's condition is false, the
wait is released, and the rule's own decision — closed — is sent like any
other. That is the "close it once the door shuts" half. Nothing has to
remember the close: it is simply what the rule still decides.

`applies_to: closing` keeps the guard out of everything else. Raising the
blind, or moving only its slats, is never held, and a blind that is already
closed is not "closing", so opening the door under it holds nothing.

Do not add a `skip` guard with the same `when` in front of this one. Guards
are first match wins per blind, so the `skip` would take every case and the
`defer` would never fire — and with it, `pending.deferred` would stay empty
and `max_wait` would never be counted.

## Choosing `max_wait` and `on_timeout`

`max_wait: 10800` (three hours, what the author's own house uses) is how
long the guard waits before its timeout answer applies. The two answers
are opposites and there is no default:

- **`abandon`** — the wait gives up: a warning is logged and nothing is
  sent. The guard still fires while the door stays open, so the blind keeps
  waiting, and it still closes when the door finally shuts. In practice
  `abandon` stops the periodic re-examination and leaves a log line saying
  the door was open unusually long.
- **`proceed`** — the held close is sent once the deadline passes, onto the
  open door. This is rarely what you want for a door.

`max_wait: null` waits with no deadline at all: no timeout ever applies,
and the blind closes when the door shuts, however long that takes.

## Watching it work

While the door is open and a close is being held, the
[diagnostic sensor](../reference/sensor.md)'s `pending.deferred` attribute
has an entry for `cover.terrace_door`: the guard's index and `name`,
`state` (`waiting`, then `abandon` or `proceed` once `max_wait` has run
out), `waited` and `max_wait` in seconds, `on_timeout` and `recheck_every`.
The entry disappears when the door shuts. A blind over an open door with
**no** entry is one the rule does not currently want lower — nothing is
being held. See
[troubleshooting](../troubleshooting.md#the-blind-is-waiting-and-did-not-move)
for the full walkthrough.

## Restart safety

Nothing above needs a watchdog script. A deferral is re-derived from the
configuration and the live door state on every evaluation rather than kept
as an in-flight timer, so a Home Assistant restart cannot cancel it: the
first evaluation after startup recreates the wait if the door is still
open, or sends the close if it shut in the meantime. `recheck_every`
(900 s here, also the default) makes an unresolved wait re-examine itself
even when nothing changes, so the timeout is applied on time — the
re-examination is scheduled no later than the `max_wait` deadline. The only
cost of a restart is that `waited` starts again from zero, which only ever
moves the timeout later, never earlier.

## Common mistakes

- Pairing this `defer` with a `skip` guard on the same door — see above;
  the first one written takes every case.
- Setting `applies_to: any` instead of `closing` — this would also hold
  the blind from *opening* while the door is open, which is never the
  hazard a door interlock exists for.
- Forgetting `on_timeout` — there is no default, and a config that omits
  it is rejected rather than silently picking one.
