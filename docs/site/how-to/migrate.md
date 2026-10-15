# Migrate to Cover Logic

Two starting points, two different paths. Both end the same way: Cover Logic
holding every blind, nothing else sending `cover.*` commands to them.

## From hand-written automations

Cover Logic is built to run **alongside** whatever already controls your
blinds, not to replace it on day one — [shadow mode](../getting-started/shadow-mode.md)
exists for exactly this. The migration path is:

1. **Set it up with `dry_run` on** (the default) and build a configuration
   that covers everything your existing automations decide — see the
   [concepts](../concepts/blinds.md) pages and the other how-to guides for
   the individual pieces (sun shading, heatwaves, door interlocks).
2. **Let both systems run side by side** for at least a few days, spanning a
   dusk and a morning at minimum — the [shadow mode checklist](../getting-started/shadow-mode.md#checklist-before-turning-it-off)
   is the same list to work through here.
3. **Compare decisions, not just outcomes.** Read `sensor.cover_logic_mode`'s
   `trace` against what your automations actually did, from the logbook
   (`/api/history/period` with `significant_changes_only=0` if you need
   attribute-only changes too). Agreement on *why* a blind moved matters as
   much as agreement on *where* it ended up — a rule that coincidentally
   reaches the same position for the wrong reason will diverge the next time
   something is different.
4. **Remove cover calls from the old automations one owner at a time**, not
   all at once. Pick the automation that owns the fewest blinds or the
   simplest logic first, remove only its `cover.*` service calls (leaving
   anything else it does — notifications, other domains — untouched), and
   watch that specific set of blinds for a day before moving to the next
   owner. A blind briefly owned by **nobody** just sits still, which is
   safe; a blind owned by **two** systems at once is where surprises come
   from, so shrinking the overlap one automation at a time is what keeps the
   migration diagnosable if something looks wrong.
5. **Turn `dry_run` off** once every blind has exactly one owner left:
   Cover Logic.

Keep the old automations disabled rather than deleted until you are
confident — turning one back on is a safety net; reconstructing it from
memory is not.

## From `adaptive-cover`

`adaptive-cover` runs one instance per window, each deciding a single
climate-aware position from sun geometry. Cover Logic's model is different
— several zones and modes live in one configuration — so migrating is a
translation of concepts, not a field-for-field import. This table maps the
`adaptive-cover` idea to its Cover Logic equivalent:

| `adaptive-cover` concept | Cover Logic equivalent |
|---|---|
| Window azimuth | [`facade_azimuth`](../concepts/blinds.md#azimuth-and-the-tolerance-cone) on the blind |
| Field of view (FOV) | [`tolerance`](../concepts/blinds.md#azimuth-and-the-tolerance-cone) on the blind, used by `sun_hits_target` |
| Climate mode (shades when hot) | A [mode](../concepts/modes.md) or [rule](../concepts/rules.md) with a `numeric_state` temperature condition — see [heatwave](heatwave.md) |
| Manual override | [`manual_detection.hold`](../concepts/manual-moves.md#hold) |
| Start/end time window | A `time` condition (`after`/`before`), inline or as a [named condition](../concepts/modes.md) referenced with `!ref` |

A few differences are worth knowing going in, not discovering midway:

- **One cover entity can only belong to one Cover Logic zone**, but a zone
  can hold several blinds that should move together — where
  `adaptive-cover` had one instance per window, Cover Logic usually
  collapses several of those instances into one zone with one shared rule
  list, unless the windows genuinely need independent decisions.
- **Cover Logic has no built-in slat-angle-from-sun-elevation output the
  same way `adaptive-cover`'s does by default** — the equivalent is the
  [`slat_angle` computed value](../concepts/values.md#type-slat_angle),
  which uses the same underlying formula but is opt-in per `!ref` in a rule,
  and ships with its slat-direction convention **unverified against real
  hardware** — read that page's warning before relying on it.
- **A manual override in Cover Logic is detected from the cover's own state
  change**, not configured as a separate per-window toggle — see
  [manual moves](../concepts/manual-moves.md) for exactly what counts as
  manual, including its one real limitation (voice commands are not always
  detectable this way).

As with migrating from automations, do this with `dry_run` on first and
compare before disabling the old integration's config entries — the same
[shadow mode checklist](../getting-started/shadow-mode.md#checklist-before-turning-it-off)
applies regardless of what you are migrating from.
