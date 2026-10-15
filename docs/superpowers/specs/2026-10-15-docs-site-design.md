# Documentation site and in-UI help — design

Date: 2026-10-15. Status: approved in chat, awaiting spec review.

## Goal

Make Cover Logic understandable to someone who did not build it. Today the
user-facing documentation is a 172-line README; everything deeper lives in
`MODELS.md` (773 lines) and `docs/rationale.md` (2,377 lines), which are written
for maintainers and models. The config flow carries the rest, in field
descriptions of up to 540 characters.

Prompted by a comparison with `basbruss/adaptive-cover`, whose strongest asset
is not its engine but how it explains itself: a decision flowchart, a picture
per cover type, and a variables table with default and range.

Out of scope (separate sub-project): number of flow steps, selectors replacing
free text, the duplicated options/subentry editing paths, new entities.

## 1. Site

MkDocs Material, English, published to GitHub Pages.

```
docs/site/            # mkdocs docs_dir (keeps existing docs/ files untouched)
  index.md            # what it is, one decision diagram, comparison table
  getting-started/
    install.md
    first-setup.md    # "Set up blinds now" walkthrough, with screenshots
    shadow-mode.md    # dry_run, and the checklist for turning it off
  concepts/
    blinds.md         # facade azimuth + tolerance (SVG), travel time, tilt
    zones.md
    modes.md
    rules.md          # first-match, keep vs set, inherited defaults
    values.md         # helper values, slat_angle (SVG of slat geometry)
    guards.md         # policies, stage, priority (Mermaid)
    manual-moves.md   # manual_detection, hold
  how-to/
    shade-by-sun.md
    dusk-by-lux.md    # for: + latch, why not hysteresis
    door-interlock.md
    heatwave.md
    migrate.md        # from automations, from adaptive-cover
  reference/
    configuration.md  # GENERATED from config_schema.py
    services.md
    sensor.md         # every attribute of sensor.cover_logic_mode
    repairs.md        # every repair issue: cause, fix
  troubleshooting.md  # "why did this blind move?": trace, readiness.blocked,
                      # pending.deferred, manual_holds, matica_diff
mkdocs.yml
```

`MODELS.md`, `docs/rationale.md` and `CONTRIBUTING.md` stay where they are and
are linked from a "For developers" nav entry; they are not rewritten.

README shrinks to: one-paragraph pitch, status, install, link to the site,
license, how to run tests.

## 2. Visuals

- **Mermaid** (native in Material): mode → rule → guard → command pipeline;
  guard priority; command lifecycle (planned → queued → waiting → sent).
- **Hand-written SVG** for geometry, using `currentColor` so they work in light
  and dark themes: facade top view with azimuth and tolerance cone; slat cross
  section with distance and depth; one day of sun on a facade.
- **Screenshots**: at most four (start menu, facing step, summary, options
  menu), taken on the throwaway HA instance on the laptop. Few, because each
  one goes stale with every flow change.

## 3. Generated reference

`scripts/gen_reference.py` reads the voluptuous schemas in `config_schema.py`
and writes `docs/site/reference/configuration.md`: one table per section
(field, type, default, allowed range or values, description).

Descriptions come from a single place so they cannot drift: the
`data_description` in `strings.json` where a field has one, otherwise a short
docstring map in the generator. A test regenerates into memory and fails if the
committed file differs — the same idea as the fixture drift check.

## 4. In-UI help

- Every `data_description` becomes one sentence, at most 150 characters. The
  removed explanation moves to the matching concept page.
- Every step and repair issue with a description ends with a link
  `[Learn more]({docs_url}/concepts/guards/)`. URLs cannot be literal in
  translations (hassfest), so `{docs_url}` is a description placeholder filled
  from one constant in `const.py`.
- Repair issue texts shrink to: what is wrong, one line on the fix, link to
  `reference/repairs.md#<issue>`.
- `manifest.json` `documentation` points to the Pages URL.
- `translations/sk.json` is shortened in the same pass; `en.json` stays
  byte-identical to `strings.json`.

Tests (in `tests/`, no HA needed):
- every `data_description` ≤ 150 characters, every issue/step description ≤ 300;
- every step or issue that has a description passes `docs_url` and links to a
  path that exists under `docs/site/`;
- the placeholder is supplied at every call site (`description_placeholders`)
  — checked by AST, because the
  HA flow tests would not notice a missing placeholder rendering as `{docs_url}`.

## 5. CI and publishing

`.github/workflows/docs.yml`:
- on pull request: `mkdocs build --strict` (broken links and missing pages fail);
- on push to `main`: build and deploy to `gh-pages`.

Nothing is pushed and Pages is not enabled until the diff has been shown and
approved.

## 6. Branching

Work happens on `docs/site`, branched from `deploy/2026-09-18` (the deployed
code, 46 commits ahead of `main`), so the site describes what actually runs —
manual hold, `dead_band`, lighting flags, `slat_angle`. The docs PR targets
`main` only after `deploy/2026-09-18` is merged there; otherwise the public site
would document features the released version lacks.

## Done when

- `mkdocs build --strict` passes locally and in CI;
- generated reference matches the schema (test green);
- string-length and link tests green, full test suite green;
- the integration deployed to the house shows the shortened texts and working
  links (one options flow opened and checked by hand);
- README is a landing page, not a manual.
