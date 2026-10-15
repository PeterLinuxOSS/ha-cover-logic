# Decide dusk from a lux sensor

A lux sensor looks like the obvious way to decide "is it dark enough to
close for the night" — more direct than a fixed clock time, and it adapts to
an overcast day on its own. It is also the condition type most likely to
misbehave if written as a plain threshold, for two independent reasons this
page covers in order: noise at the threshold, and a sensor that does not
move much at all.

```yaml title="config"
blinds:
  - entity: cover.terrace_door
zones:
  terrace:
    members: [cover.terrace_door]
conditions:
  dusk:
    condition: numeric_state
    entity_id: sensor.outdoor_illuminance
    below: 2800
    for: 180
    latch: daily
    default: 99999
modes:
  - id: night
    when: !ref dusk
  - id: home
rules:
  night.terrace:
    - then: {position: 0, tilt: 0}
  home.terrace:
    - then: {position: keep, tilt: keep}
```

## Why `default: 99999`, not a low number

`numeric_state` requires an explicit `default` ([rules](../concepts/rules.md)
mentions this in passing for every condition of this kind). For a "below"
threshold, the default has to be a value that **fails** the comparison, so a
missing or `unavailable` sensor reads as broad daylight rather than as dusk —
the opposite mistake to [heatwave](heatwave.md)'s default, but the same
principle: pick the default that means "nothing unusual", never the one that
happens to be convenient to type.

## Why a plain threshold is not enough: `for:`

A `numeric_state` condition with no `for:` reacts to every single reading,
and most lux sensors rewrite their value every couple of minutes even while
sitting still. At dusk the value spends a long stretch hovering right around
any fixed threshold, and a sensor that briefly dips below 2800 lux and climbs
back up a reading later would otherwise flip the mode twice in the space of
four minutes — each flip sending every blind in the zone on a full run.
This was measured, not theoretical: on one installation, a lux reading of
2788, then 2813, then 2742 four minutes apart made a terrace door blind
close, reopen, and close again in one evening.

`for: 180` fixes the obvious half of this: the threshold must stay crossed
for three minutes before the condition answers true at all. Note that this
`for:` is **not** measured the way `condition: state`'s `for:` is — a
`numeric_state` has no single "last changed" moment to measure from, since
the sensor keeps rewriting its value whether or not it has crossed anything.
This dialect resolves it from a separate per-threshold memory instead (see
["Why hysteresis fails" below](#why-not-hysteresis) for what that memory
is for).

## Why `for:` alone is still not enough: `latch: daily`

`for:` only qualifies the *entry* — it says a reading has to stay below the
threshold for a while before dusk is believed at all. It says nothing about
the *exit*: once genuinely past dusk, the value still sits near the
threshold for a long time as the sky keeps darkening, and a single upward
wobble an hour after real dusk would otherwise undo it and reopen a house
that was correctly closed for the night. Dusk does not un-happen once it has
happened.

`latch: daily` is the other half: once the threshold has genuinely been
crossed (survived its `for:`) today, the condition keeps answering true for
the rest of the local day, regardless of what the sensor does afterwards.
The reset is the calendar rolling over to a new date, not a second timer to
tune.

**Neither half works alone.** `latch: daily` with no `for:` would let a
single stray midday reading latch the rest of the day as "dusk" the moment
anything dipped the sensor even briefly — `latch:` only makes sense tied to
an entry that has already been debounced. This is checked, not just advised:
a configuration with `latch:` on a condition that is not `numeric_state`, or
with any value other than `daily`, is a validation error, not a quietly
ignored field.

## Why not hysteresis? {#why-not-hysteresis}

The textbook fix for a noisy threshold is hysteresis — a different "off"
level than "on" level, so a value has to travel further before flipping
back. It does not work here: measured over ten days, this class of lux
sensor saturates around 3050 during the day and never once exceeded 3200 in
those ten days of measurements, so a hysteresis band wide enough to actually
absorb daytime noise would
never release again once triggered — the house would stay in "dusk" from
roughly midday onward, every day. `for:` plus `latch: daily` solves the
actual problem (a debounced entry, a remembered exit) without needing the
sensor's full dynamic range to have headroom it does not have. Measure your
own sensor's daytime ceiling before trying hysteresis on it — a sensor that
saturates much lower than its nominal maximum is the warning sign.

## The latch does not survive a restart

The memory `for:`/`latch:` need is held in the running coordinator, not
written to storage — a Home Assistant restart loses it. In practice this is
safe in the direction that matters: after a restart during the latched
window, the condition falls back to the plain threshold until it re-earns
the latch on its own, which can only ever make the house open **later** in
that edge case, never close early onto a boundary it had not actually
crossed. If a restart happens to land in the narrow window between a
genuine dusk and the sensor settling comfortably below the threshold, a
single wobble could reopen things until the dwell re-qualifies — rare, and
strictly a smaller risk than running with no latch at all.

## Common mistakes

- Setting `default` to a value that happens to be below the threshold —
  this makes a dead sensor report dusk immediately, the opposite of a safe
  failure.
- Using `latch: "Daily"`, `latch: true`, or any other spelling — the one
  accepted value is the literal string `daily`.
- Expecting `for:` here to behave like `condition: state`'s `for:` — it is
  resolved from a dedicated dwell, not from `World.since`, precisely
  because this entity's value changes constantly.
