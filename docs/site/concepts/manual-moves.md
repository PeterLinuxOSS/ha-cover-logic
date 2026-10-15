# Manual moves

Manual-move detection is how the integration tells "a person just moved this
blind by hand" apart from its own commands and from everything else that
also changes a cover's state. It is the one part of the configuration the
decision engine itself never reads — it only affects what the coordinator
does after a decision is already made.

```yaml title="config"
blinds:
  - entity: cover.living_room_balcony
zones:
  living_room:
    members: [cover.living_room_balcony]
modes:
  - id: home
rules: {}
manual_detection:
  ignore_while_on: [script.evening_routine, input_boolean.bulk_move_in_progress]
  hold: true
```

## `ignore_while_on`

A list of entities that are only `on` for the duration of an automated
mover's own run — most usefully a `script.*` entity, which Home Assistant
keeps `on` for exactly as long as that script executes. While any of them is
`on`, a state change on a watched blind is attributed to that run, not to a
person — a script inherits the caller's context (including `user_id`), so
without this exclusion a script a person started would look exactly like
that person moving the blind directly. It is not restricted to the `script.`
domain: an `input_boolean` a house sets around its own bulk movements works
identically.

## `hold`

When `true`, a blind a person just moved by hand is left alone — the
integration stops re-sending that blind's current target on every later
recompute. The hold remembers the mode and the target in force when the
move was seen, and is released as soon as either changes: a mode switch
releases it even when the new mode decides the same target. Guards still
act normally on a held blind; only the plain rule-decided outcome is
withheld. Without `hold`, a level-held target (for example "night mode
always wants position 0") would be silently re-asserted on the next
recompute, undoing a move someone just made from the app seconds earlier.

Holds live in memory only: a Home Assistant restart or a reload of the
integration forgets them, and the next recompute sends the decided target
again.

## Why detection needs `user_id`

A move is only recognised as manual when its state change carries a
`context.user_id` at all. That is a deliberate, narrower test than "this
wasn't one of our own commands" — a change with no `user_id` attached (many
automations' and scripts' own commands) is silently discarded rather than
assumed to be a person, because the absence of "us" is not evidence of a
person either. Three conditions must all hold for a movement to count as
manual:

1. a `user_id` is present on the new state's context,
2. it is not one of this integration's own commands (tracked by context,
   not by `user_id` alone), and
3. no entity in `ignore_while_on` is currently `on`.

A physical wall switch or a remote wired straight to the motor carries no
`user_id`, so such a move is **not** detected and `hold` does not protect
it: the next recompute that re-sends the target undoes it. Only moves made
from the Home Assistant app or UI (by a logged-in user) are recognised.

## The voice limitation

Home Assistant does not guarantee a `user_id` on the context of every state
change a voice command produces — a command spoken through an Assist
satellite can arrive with a context that carries no `user_id`, depending on
the pipeline and integration involved. Because manual-move detection
requires `user_id` to be present at all, a blind moved by voice may not be
recognised as a manual move, and `hold` will not protect it. There is no
workaround at the configuration level; this is a property of what Home
Assistant attaches to a voice-originated service call, not something
`manual_detection` can see around.

## Common mistakes

- Expecting `hold` to apply to a guard-driven outcome — it only withholds
  the plain rule decision; a guard that fires still wins.
- Forgetting to add a new script that moves blinds in bulk to
  `ignore_while_on` — every run will otherwise look like somebody grabbed
  every blind in the house by hand at once.
- Assuming a voice-controlled or wall-switch move is covered the same way
  an app move is — only moves carrying a `user_id` are detected (see
  above).

## Where to edit this

**Settings → Devices & services → Cover Logic → Manual detection** (there is
only one instance for the whole house; the menu offers **Edit manual
detection** once it exists).

See the [reference: manual_detection](../reference/configuration.md#manual_detection)
for the full field list.
