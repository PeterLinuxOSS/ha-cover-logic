# Repair issues

Four checks surface as Home Assistant repair issues (**Settings → System →
Repairs**) rather than only a log line, because a log line only reaches
someone tailing it at the right moment. None of the four stop the
integration from running — each one means something is quietly not being
decided the way you probably expect, until you act on it. None are
auto-fixable from the Repairs UI (`is_fixable: false`); every fix below is
made in the configuration itself.

## Fixture drift {#fixture_drift}

**Cause.** You have set a **Fixture to compare against** (Configure →
Execution → `fixture_path`) — typically a YAML file kept in a checkout
alongside the integration, used as a second source of truth for your
configuration — and the live subentries no longer match what that file
describes.

**Effect.** Nothing about the running decision changes. This only means the
two no longer agree, so anything that trusts the fixture as "what the house
actually runs" (a test suite, a migration check) is comparing against a
stale picture.

**Fix.** Re-import the file (the `import_config` service) to bring the live
configuration back in line with it, or re-export the live configuration to
that path if the live configuration is the one you actually intend to keep.
If you have no `fixture_path` configured, you will never see this issue at
all — it only applies to the specific workflow of keeping a configuration
file and the live subentries in sync on purpose.

## Unknown entities {#unknown_entities}

**Cause.** The configuration names at least one entity id that exists in
neither Home Assistant's state machine nor its entity registry — checked
once, shortly after Home Assistant finishes starting.

**Effect.** A rule, condition or guard that depends on a missing entity
behaves as though that input were permanently absent (falling back to
whatever `default:` it states, or simply never matching). A blind among the
named entities can never be moved at all, by anything.

**Fix.** Either create the missing helper/entity, or edit the configuration
to reference the entity your house actually has. The issue's own
description lists exactly which entities and how many.

## Blinds missing features {#blinds_missing_features}

**Cause.** A blind's rules ask it to do something its `cover.*` entity does
not actually support — checked once, shortly after Home Assistant finishes
starting. The entity picker for "which cover is this blind" filters only on
the `cover` domain, so it is possible to pick a garage door or an awning
that has no tilt channel, or no position setter at all.

**Effect.** Nothing crashes — the command the hardware cannot carry out is
simply dropped silently at the point it would be sent, which otherwise looks
exactly like a dead-band skip or a withheld guard with no obvious cause.

**Fix.** Either pick a cover entity that genuinely supports what the rules
ask for, or narrow the rules to only what the entity can do: `position: 0`
and `position: 100` need only open/close support, anything strictly between
needs a position setter, and `tilt` needs a tilt channel at all.

## Configuration has warnings {#config_warnings}

**Cause.** `validate()` found one or more warning-severity problems in the
configuration — re-checked on every setup of the entry, so this reappears or
clears as you edit. Unlike the two "checked once" issues above, editing the
configuration and letting it reload will re-evaluate this one. Possible
causes (not an exhaustive list, but the common ones): a zone with no rules
at all, a rule that can never be reached because an earlier one in the same
list already matches everything, a guard that can never fire, a blind no
zone claims, a `tilt` value asked of a blind with no tilt axis, or a
computed `slat_angle` asked of a blind that states no facade or slat
geometry, or placed on the wrong axis.

**Effect.** Nothing stops working outright — every one of these means some
specific thing is quietly not being decided, the same class of fault as the
two "checked once" issues above, just caught by the broader structural
checker rather than by looking at live entities.

**Fix.** The issue's own description lists every warning found, by code and
message, naming the affected subentry. Open that subentry (zone, rule, guard,
or blind) from the options menu and either fix what it names, or — if the
behaviour really is what you intended — nothing further is required; the
issue exists so you confirm that on purpose, not so it fixes itself.
