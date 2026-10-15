# Shade a window when the sun hits it

The most common rule in this project: close a blind partway, angled, whenever
the sun is actually on that side of the house — and leave it alone the rest
of the time. This combines three concepts already covered on their own
pages: a blind's [facade azimuth](../concepts/blinds.md), the
[`sun_hits_target`](../concepts/rules.md) condition, and a
[rule](../concepts/rules.md) that only partly acts (`position: keep`, only
`tilt` set).

```yaml title="config"
blinds:
  - entity: cover.office_window
    facade_azimuth: 270 # west
    tolerance: 40
zones:
  office:
    members: [cover.office_window]
conditions:
  sun_up:
    condition: state
    entity_id: sun.sun
    state: above_horizon
  sun_on_this_facade:
    condition: sun_hits_target
modes:
  - id: home
rules:
  home.office:
    - if: {condition: and, conditions: [!ref sun_up, !ref sun_on_this_facade]}
      then: {position: keep, tilt: 15}
      name: afternoon sun, west-facing
    - then: {position: keep, tilt: keep}
```

## Why `position: keep`

Height is a decision you usually want a person — or a different rule, like
[heatwave shading](heatwave.md) — to make. Shading from the sun is about
**angle**, not height: leaving `position` at `keep` means this rule only ever
touches the slats, and whatever else moved the blind up or down today is
left alone. If you also want the blind to close by height when the sun hits
it, set `position` to a number instead — nothing about `sun_hits_target`
requires `tilt`-only.

## One rule works for every facade

`sun_hits_target` reads `facade_azimuth` and `tolerance` off **the blind
currently being decided**, not off the condition itself. The one rule above,
written once under `home.office`, would work unchanged if copied to
`home.bedroom`, `home.kitchen`, or any other zone — each blind supplies its
own facing. **Set up blinds now**
(see [first setup](../getting-started/first-setup.md)) relies on the same
property: its one shading rule covers every blind you give a compass
direction, though it sets a fixed position 20 / tilt 45 rather than the
tilt-only action above.

## Picking `tolerance`

`tolerance` (default 45°) is how wide the "sun is on this facade" window is,
centred on `facade_azimuth`. A narrower cone (shown here: 40°) means the
blind reacts closer to when the sun is actually square-on to the window;
a wider one reacts earlier in the morning and later in the evening, at the
cost of catching more glancing, low-intensity sun. There is no universally
right number — start at the default and narrow it if the blind reacts
before direct sun is actually a problem.

## Adding a fallback for cloudy conditions

`sun_hits_target` only looks at the sun's position, never at actual light
levels — on an overcast day it will still fire exactly as it would in full
sun, because geometrically the sun is still on that side of the sky. If you
want the rule to stand down on a dim day, add a lux condition with `and`, the
same way [dusk by lux](dusk-by-lux.md) debounces a threshold — but be aware
of the saturating-sensor trap that page explains before relying on one.

## Common mistakes

- Setting `tolerance` very wide and then being surprised the blind shades
  for most of the day — a wide cone is a wide cone on both sides of the
  bearing.
- Forgetting the catch-all rule (`then: {position: keep, tilt: keep}`) at
  the end of the list — without it, the zone falls through to `#none`
  (equivalent in effect, but invisible in the trace as "nothing matched"
  rather than "this rule decided to do nothing").
- Expecting this rule alone to protect against heat — it reacts to sun
  position only; pair it with [heatwave](heatwave.md) if indoor temperature
  is the actual concern.
