# Shadow mode

Cover Logic decides from the moment it is set up, whether or not it is
allowed to act on that decision. **`dry_run` is what separates the two, and
it is on by default.** While it is on, every command is decided, planned,
queued and logged — and nothing is sent to a real `cover.*` service. A fresh
install therefore moves nothing, even though
[`sensor.cover_logic_mode`](../reference/sensor.md) is already reporting a
mode and a target for every blind.

This is the intended way to start: install it alongside whatever already
controls your blinds (existing automations, `adaptive-cover`, a hand-written
script), watch what Cover Logic *would* have done, compare it against what
actually happened, and only then hand it the covers for real.

## Where to toggle it

**Settings → Devices & services → Cover Logic → Configure → Execution.**
This one screen holds the two settings that change the engine's relationship
to real hardware:

- **Dry run** — on: nothing moves, every intended command is written to the
  log. Off: covers really move. Takes effect immediately, with **no
  reload** — this is the one setting in the whole configuration that is
  deliberately not a subentry, precisely so "turn the hands off right now"
  never has to wait for one.
- **Dead band** — how many points a cover's reported position or tilt may
  differ from a rule's target before Cover Logic bothers to send a command.
  Also used as the tolerance for deciding a move has arrived. Default **5**.
  Too small, and a blind whose motor slightly mis-reports its own position
  after moving gets re-sent the same command for no reason — tilting a
  blind's slats shifts the motor's own position estimate, so a repeated
  *absolute* command is itself an unwanted movement. Too large, and the
  integration stops correcting real drift. If one blind's tilt motor
  under-reports further than the rest of the house after a move (watch
  [`last_command`](../reference/sensor.md) for a `suppressed` entry that
  looks wrong), raise this rather than chasing the individual blind.

Both settings are read live from the running entry — no reload needed for
either.

## What the log looks like while dry

Every command — dry or real — is written in the same shape, so comparing
against `/api/logbook` or whatever automation you are replacing is the whole
point of the exercise. The newest one is also always available as
[`last_command`](../reference/sensor.md) on the diagnostic sensor, without
tailing anything.

## Checklist before turning it off

Work through all four before flipping **Dry run** off. None of them need the
log tailed in real time — read them back from `sensor.cover_logic_mode`'s
attributes and the logbook after the fact.

1. **`trace` names your own zones, not a default.** Every entry should read
   like `night.terrace#0`, not `night.*#0` — a `*` means that pair fell
   through to a mode's shared default list rather than the zone-specific
   rules you expect. Seeing `*` where you expected a named zone usually
   means the rule you wrote was never saved, or was saved for a different
   zone id than the blind's own.
2. **`matica_diff` is `[]`**, if you are migrating off something this
   sensor can be compared against (the old system this project itself
   replaced exposed one). An empty list means "checked, and they agree";
   `null` means there is nothing to compare against at all — read
   [sensor reference](../reference/sensor.md#matica_mode-and-matica_diff)
   for the difference between those two.
3. **No open repair issues** under Settings → System → Repairs — see
   [repairs](../reference/repairs.md) for what each one means and how to
   clear it. None of the four are fatal to dry-run operation, but every one
   means some blind is not being decided the way you think it is.
4. **Watch one dusk and one morning in the log.** These are the two moments
   most configurations have the most rules firing in the shortest time —
   mode switches, guards releasing, dead-band skips all happening together.
   A quiet daytime log proves far less than fifteen minutes of a real
   sunset.

Once all four are satisfied, flip **Dry run** off from the same Execution
screen. Nothing else changes — the same rules, the same guards, the same
trace — only the services actually go out.
