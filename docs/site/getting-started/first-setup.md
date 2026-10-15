# First setup

Once the integration is added, it shows one menu: **How do you want to
start?**

| Choice | What it does |
|---|---|
| **Set up blinds now** *(recommended)* | Pick your cover entities, say which way each one faces, and it builds a working configuration for you. |
| **Load a configuration from a YAML file** | Point it at a file on disk — the same format `import_config` reads. For moving a configuration from another house, or restoring one written by hand. |
| **Start from the example configuration** | Imports `docs/example-config.yaml`, a complete worked example for an invented two-bedroom apartment — a plausible starting point to read and edit rather than an empty one. Only offered when that file ships alongside the install. |
| **Start empty** | A config entry with no subentries at all. You build everything by clicking afterwards. |

Every choice ends with a usable entry. "Start empty" is a deliberate
exception to "usable": it has no fallback mode yet, so the entry sits in a
`not ready` state (visible on the integration's card) until you add a mode
with no condition — normal for a fresh, empty entry, not an error to chase.

## Walkthrough: "Set up blinds now"

This is the three-screen flow behind the recommended choice.

### 1. Pick your covers

**Blinds** — an entity picker, filtered to the `cover` domain. Pick every
`cover.*` you want Cover Logic to decide for. You can add more blinds later
from the options menu; nothing here is final.

### 2. Say which way each one faces

One screen per blind picked, asked in turn: **"{blind} — pick the compass
direction its outside faces."** This decides when the sun reaches it — see
[blinds](../concepts/blinds.md#azimuth-and-the-tolerance-cone) for what
`facade_azimuth` actually does with the answer. Guessing is fine for now;
the SVG and the measurement method on that page explain how to get a real
reading later.

### 3. Review the summary, then create it

Before anything is saved, the flow shows exactly what it is about to build:

> At night, nothing moves.
> During the day, each blind closes partway (to position 20, tilt 45)
> whenever the sun is shining on its own side of the house, and stays fully
> open (position 100) the rest of the time.

Concretely, this is one zone holding every blind you picked, two modes
(`day` when `sun.sun` is `above_horizon`, `night` as the fallback), and two
rules per mode shared by that zone: shade on `sun_hits_target`, otherwise
stay open; at night, `keep`/`keep` for everything. It is a real, working
configuration from the first screen — not a placeholder you must finish
before anything happens — and every part of it (the zone, the modes, the
rules, the blinds' own facing) can be edited afterwards from
**Settings → Devices & services → Cover Logic → Configure**.

Confirming creates the entry. Cancelling returns to the main menu with
nothing saved.

## What to do next

The entry is created with `dry_run` on, so nothing will actually move yet —
see [shadow mode](shadow-mode.md) for what that means and the checklist for
turning it off. To reshape what "Set up blinds now" built — splitting one
zone into several, adding a guard, naming a reusable value — see the
[concepts](../concepts/blinds.md) pages and the how-to guides.
