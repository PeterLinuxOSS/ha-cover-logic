# Close blinds during a heatwave

A heatwave rule is a [`numeric_state`](../concepts/rules.md) condition on an
outdoor temperature sensor, used either inside a mode's `when` (the whole
house reacts) or inside a single rule's `if` (one zone reacts, which is
usually what you want — a heatwave is not reason enough to black out a room
nobody uses in the daytime).

```yaml title="config"
blinds:
  - entity: cover.living_room_window
    facade_azimuth: 180
    tolerance: 40
zones:
  living_room:
    members: [cover.living_room_window]
conditions:
  heatwave:
    condition: numeric_state
    entity_id: sensor.outdoor_temperature
    default: 20
    above: 28
  sun_on_this_facade:
    condition: sun_hits_target
values:
  heatwave_tilt:
    entity: input_number.heatwave_tilt_target
    default: 40
modes:
  - id: home
rules:
  home.living_room:
    - if: {condition: and, conditions: [!ref heatwave, !ref sun_on_this_facade]}
      then: {position: keep, tilt: !ref heatwave_tilt}
      name: heatwave shading on the sunlit side, leave height alone
    - then: {position: keep, tilt: keep}
```

## `default` is the most important field here

`numeric_state` requires an explicit `default` ([rules](../concepts/rules.md)
reference it in passing; this is the condition it belongs to). For a
heatwave threshold, the default must read as **not a heatwave** — 20°C
above, as here — so a dead or `unavailable` outdoor temperature sensor
never fakes one and closes the whole house. Picking a default that *would*
read as true is a mistake that only shows up the day the sensor actually
fails.

## Only shade the sunlit side

Pairing `heatwave` with `sun_hits_target` (as above) means the rule only
fires for a blind whose own facade the sun is currently on — a heatwave
condition alone, with no sun check, shades every blind in the zone
regardless of which way it faces, including ones already in shadow that
gain nothing from closing. Drop the `and`/`sun_on_this_facade` half if you
genuinely want every blind in the zone to react the same way regardless of
orientation (for example, a single-aspect room).

## Reading the tilt amount from a helper

`heatwave_tilt` above is a `type: entity` [value](../concepts/values.md), so
the shading amount is an `input_number` you can tune from the dashboard
without editing the rule. Use a literal number instead
(`tilt: 40`) if you never expect to change it.

## Letting it recover automatically

Nothing extra is needed for the blind to reopen once the heatwave passes —
`heatwave` simply stops matching once the sensor reads below 28°C, and the
catch-all rule (`then: {position: keep, tilt: keep}`) takes over. If you
want it to actively *reopen* rather than just stop re-closing, give the
catch-all a real action (for example `tilt: 100`) instead of `keep`.

## Combining with wind safety

A heatwave and high wind can coincide. If a blind should never be forced
shut while it is windy regardless of temperature, add a
[guard](../concepts/guards.md) with `policy: skip` ahead of this rule's
effect, or `policy: force` to fully open on wind — guards are evaluated
after rules and can override what a heatwave rule decided.

## Common mistakes

- A `default` that reads as "heatwave" when the sensor is missing — always
  pick the default that means "nothing unusual is happening".
- A heatwave condition with no `sun_hits_target` pairing, closing the whole
  zone uniformly when only one facade actually needs it.
- Expecting the rule to also protect against wind — temperature and wind are
  independent conditions; combine them explicitly if both matter.
