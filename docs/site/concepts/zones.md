# Zones

A zone is a group of blinds decided together. Rules are never written
against an individual blind — they are written against a zone, one rule
list per `(mode, zone)` pair, and every blind in that zone gets the same
answer for a given rule.

```yaml title="config"
blinds:
  - entity: cover.bedroom_window
  - entity: cover.office_window
zones:
  bedroom:
    members: [cover.bedroom_window]
    occupants: [alex]
  office:
    members: [cover.office_window]
modes:
  - id: home
rules: {}
```

## Fields

- `members` — the blind entity ids decided together as this zone.
- `occupants` — person names (as in `person.<name>`) read by the
  [`event_targets_zone`](../reference/conditions.md#event_targets_zone)
  condition. A zone with no `occupants` simply never matches that condition
  (and the running integration raises no person events yet, so for now no
  zone does).

## One zone per blind

Every blind must belong to **exactly one** zone:

- A blind claimed by **two** zones has no answer — whose rules should apply
  is unanswerable — so the config fails to evaluate at all
  (`EngineError`, reported as a validation error before that).
- A blind claimed by **no** zone is **skipped**, not fatal: everything else
  still gets decided, and a `blind_without_zone` validation warning calls it
  out so it does not go unnoticed. Nothing will ever move that blind until
  it is added to a zone.

That asymmetry is deliberate: "two owners" has no correct guess to fall back
on, while "no owner" does — leave it alone and say so loudly.

```yaml
# fragment -- this blind is defined but assigned to no zone.
# validate() reports "blind_without_zone"; nothing ever moves it.
blinds:
  - entity: cover.garage_door
zones:
  office:
    members: [cover.office_window] # garage_door is not a member anywhere
```

## Common mistakes

- Adding a blind's `entity` to `blinds:` and forgetting to add it to a
  zone's `members:` — it parses fine and is silently never decided.
- Listing the same blind under two zones' `members:` — this is a hard error,
  not a warning, because there is no rule that says which zone wins.
- Expecting `occupants:` to filter *which* rules in the zone can fire — it
  only feeds `event_targets_zone`; every other condition in the zone's rules
  evaluates normally regardless of who lives there.

## Where to edit this

**Settings → Devices & services → Cover Logic → Add zone** (or **Edit
zone**). The picker for `members` only offers blinds already added under
**Add blind**.

See the [reference: zones](../reference/configuration.md#zones) for the full
field list.
