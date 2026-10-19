# Conditions reference

A condition answers true or false about the house at the moment of a
recompute. Conditions appear in three places: a mode's `when`, a rule's
`if`, and a guard's `when`. The dialect is a subset of Home Assistant's own
condition syntax, plus three types of its own and two keys Home Assistant
conditions do not have (`for:` and `latch:`).

A condition type not listed here, or one missing a required key, is an
error when the configuration is validated (`bad_condition_shape`).

## Named conditions and `!ref`

The top-level `conditions:` mapping gives a condition a name, so it can be
written once and reused anywhere a condition is accepted:

```yaml title="config"
blinds:
  - entity: cover.office
zones:
  office:
    members: [cover.office]
conditions:
  door_open:
    condition: state
    entity_id: binary_sensor.office_door
    state: "on"
  dark:
    condition: numeric_state
    entity_id: sensor.outdoor_lux
    below: 2800
    default: 99999
  dark_and_closed:
    condition: and
    conditions: [!ref dark, {condition: not, conditions: [!ref door_open]}]
modes:
  - id: evening
    when: !ref dark_and_closed
  - id: day
rules:
  evening.office:
    - then: {position: 0, tilt: 0}
  day.office:
    - then: {position: 100, tilt: 100}
```

- `!ref <name>` stands for the named condition's body. A named condition may
  reference another regardless of the order they are written in.
- A `!ref` naming nothing is an error (`unknown_condition_ref`); a chain of
  references that leads back to itself is an error too
  (`circular_condition_ref`).
- In the UI, named conditions are **Add condition** subentries.

A **list** written where one condition is expected means all of them must be
true (an implicit `and`).

## Logic: `and`, `or`, `not`

| type | required | true when |
|---|---|---|
| `and` | `conditions` | every listed condition is true |
| `or` | `conditions` | at least one listed condition is true |
| `not` | `conditions` | **none** of the listed conditions is true |

`not` over several conditions is `not (A or B)`, as in Home Assistant — not
`not (A and B)`.

## `state`

| key | required | meaning |
|---|---|---|
| `entity_id` | yes | the entity to read |
| `state` | yes | the value to match, or a list of values (any one matches) |
| `attribute` | no | compare this attribute instead of the state |
| `for` | no | seconds the entity must have been in its current state |

`for:` is an extension: Home Assistant's own conditions have no `for:`. It is
measured from the entity's last state change. If no change time is known,
`for:` is ignored rather than failing the condition.

## `numeric_state`

| key | required | meaning |
|---|---|---|
| `entity_id` | yes | the entity to read |
| `above` / `below` | at least one | strict bounds: `value > above`, `value < below` |
| `default` | **yes** | the number used when the entity is missing, `unknown`, `unavailable` or not numeric |
| `attribute` | no | read this attribute instead of the state |
| `for` | no | seconds the threshold must stay crossed before it counts |
| `latch` | no | only `daily`: once crossed, stay true until local midnight |

`default` is required because there is no safe guess for it: pick the value
that means "nothing unusual is happening" (see
[dusk by lux](../how-to/dusk-by-lux.md) and [heatwave](../how-to/heatwave.md)).
A node with `default:` also tells the [readiness gate](../troubleshooting.md#the-blind-never-appears-in-the-log-at-all)
that a missing reading is acceptable.

`for:` qualifies the entry into the crossed state, and `latch: daily`
refuses the exit; [dusk by lux](../how-to/dusk-by-lux.md) shows why a noisy
sensor usually needs both. The memory behind `for:` and `latch:` lives only
in the running integration: after a restart or reload, a `for:` dwell starts
again from the first reading, and a latch that was holding is forgotten.

## `time`

| key | required | meaning |
|---|---|---|
| `after` | at least one | `HH:MM` or `HH:MM:SS`, local time, inclusive |
| `before` | at least one | `HH:MM` or `HH:MM:SS`, local time, exclusive |

When `after` is later than `before` (for example `after: "22:00"`,
`before: "06:00"`), the window wraps around midnight.

## `sun`

| key | required | meaning |
|---|---|---|
| `after` / `before` | at least one | `sunrise` or `sunset` |
| `after_offset` / `before_offset` | no | offset in **seconds** (negative is earlier), not Home Assistant's `"-00:20:00"` string |

`before: sunrise` together with `after: sunset` is the dark window around
midnight. Where the sun does not rise or set that day, a condition naming
the missing event is false.

## `template`

| key | required | meaning |
|---|---|---|
| `value_template` | yes | a Jinja template; true when it renders `true`, `on`, `yes` or `1` |

The template sees `states()`, `is_state()`, `state_attr()`, `now()` and
`today_at()`, nothing else. Entity ids written literally in the template
are watched, so a change to them triggers a recompute; an entity id the
template computes is not.

## `sun_hits_target`

True when the sun is above the horizon and its azimuth lies within the
decided blind's `facade_azimuth ± tolerance` (the lower edge included, the
upper excluded). It reads the facing off **the blind being decided**, so one
rule serves every facade — see [shade by sun](../how-to/shade-by-sun.md). A
blind with no `facade_azimuth` never matches.

| key | default | meaning |
|---|---|---|
| `sun_entity` | `sun.sun` | must be `above_horizon` |
| `azimuth_entity` | `sensor.sun_solar_azimuth` | where the sun's azimuth is read |
| `azimuth_attribute` | none | read the azimuth from this attribute instead (for example `azimuth` on `sun.sun`) |
| `tolerance` | the blind's own `tolerance` | override the half-width of the sector, in degrees |

`sensor.sun_solar_azimuth` is disabled by default in Home Assistant; enable
it, or use `azimuth_entity: sun.sun` with `azimuth_attribute: azimuth` as the
starter setup does. A missing or out-of-range azimuth is false.

## `manual_move`

True when this recompute was prompted by a person moving **the blind being
decided** (see [manual moves](../concepts/manual-moves.md) for what is
detected).

| key | default | meaning |
|---|---|---|
| `direction` | any | `opening`, `closing` or `any` |

## `event_targets_zone`

True when the event that prompted this recompute names a person listed in
the decided zone's `occupants`. No keys. The running integration currently
prompts recomputes only with `state_change` and `manual_move` events, which
name no person, so in a live house this condition is not true yet.

## `for:` on other types

Only `state` and `numeric_state` take `for:`. On any other type it is
ignored and reported as the `for_ignored` warning.
