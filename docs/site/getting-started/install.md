# Install

Cover Logic is not in the HACS default store yet, so add it as a custom
repository, or copy the files in by hand. Either way, it ends up as one
custom integration under `custom_components/cover_logic/`.

## Via HACS

1. **HACS → ⋮ (top right) → Custom repositories.**
2. URL: `https://github.com/PeterLinuxOSS/ha-cover-logic`, category
   **Integration**.
3. Find "Cover Logic" in HACS and download it.
4. Restart Home Assistant.
5. **Settings → Devices & services → Add Integration → "Cover Logic".**

## Without HACS

1. Copy `custom_components/cover_logic/` from this repository into your Home
   Assistant's own `custom_components/` directory, so the result is
   `<config>/custom_components/cover_logic/`.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add Integration → "Cover Logic".**

## Single instance

Cover Logic supports exactly one config entry per Home Assistant instance —
adding it a second time is refused before the setup menu even appears. A
multi-house or multi-floor setup is expressed with zones and modes inside
that one entry, not with a second entry.

## Next

Setup asks how you want to start — see
[first setup](first-setup.md) for the four choices and a walkthrough of the
recommended one.
