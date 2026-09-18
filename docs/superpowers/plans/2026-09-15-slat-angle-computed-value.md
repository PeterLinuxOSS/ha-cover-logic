# Computed `slat_angle` Value Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a target-relative computed `values:` entry that derives a venetian blind's slat angle from solar geometry, so any rule in any mode can write `tilt: !ref uhol_lamiel` instead of a hard-coded constant.

**Architecture:** A new pure module `geometry.py` holds the trigonometry (no Home Assistant, no numpy). A new frozen `SlatAngle` model joins `Ref` in the `Value` union, so it is selected by the same `!ref` syntax and lives in the same `config.values` dict. Resolution becomes **target-relative**: `resolve_action` gains a `target` parameter, and `SlatAngle` reads `target.blind.facade_azimuth` plus the blind's own slat geometry — the same mechanism that already makes `sun_hits_target` work for a house of any orientation. Phase 1 ships the capability **unused**, so the 92 160-scenario migration gate stays green and house behaviour is byte-identical; Phase 2 switches rules over as a separate, measured decision.

**Tech Stack:** Python 3.12+ (system) and 3.14 (`.venv`), pytest, Home Assistant custom integration, `math` from the stdlib only.

**Spec:** [`/config/docs/superpowers/specs/2026-09-15-adaptive-cover-vs-cover-logic.md`](/config/docs/superpowers/specs/2026-09-15-adaptive-cover-vs-cover-logic.md) — comparison against `basbruss/adaptive-cover`, which is where the formula comes from, and the two measured findings in its "Doplnok" section.

## Global Constraints

- **English only, everywhere git can see it** — commits, branches, code, comments, docs. (`CLAUDE.md`)
- **Comments are one-liners.** Long reasoning goes to `docs/rationale.md` with a pointer above the code. (`CLAUDE.md`)
- **No `claude.ai` session link** in any commit or PR. (`MODELS.md` §9)
- **No `Co-Authored-By: Claude`** in commits. (owner instruction)
- **Parity first.** The migration gate in `tests/parity/test_migration_gate.py` must stay green through all of Phase 1. Phase 1 changes **no** house behaviour.
- **Purity.** `engine.py`, `conditions.py`, `model.py`, `config_schema.py`, `geometry.py` and the rest of `tests/test_purity.py::PURE_MODULES` must never import `homeassistant`.
- **No numpy.** `manifest.json` declares no dependencies and must keep declaring none; the formula uses `math`.
- **Test conventions, verified in this repo — the plan's snippets follow them:**
  `pyproject.toml` sets `pythonpath = ["custom_components", "tests"]`, so tests
  import `from cover_logic.engine import ...` — **never** a
  `custom_components.` prefix. `load_config(text: str)` takes **YAML text, not a
  dict**; every snippet below that shows a config must be written as a YAML
  string, in the style of `CFG` at the top of `tests/test_engine.py`. The local
  `world()` helper in `tests/test_engine.py` hardcodes `attributes={}` and takes
  no attributes argument, so a test needing an attribute read (sun elevation)
  constructs `World(states=..., attributes={("sun.sun", "elevation"): "40"},
  now=NOW, event=Event(), sun=SunTimes())` directly, or adds an
  `attributes=None` parameter to that helper. `PURE_MODULES` in
  `tests/test_purity.py` holds bare filenames (`"engine.py"`).
- **The `rules:` shape in YAML is a MAPPING, and a value reference is a TAG.**
  Every config snippet below is written as an illustrative dict and is wrong on
  both counts — rewrite it as YAML in this shape, which is what
  `config_schema.py:228` actually parses and what `tests/test_engine.py`'s `CFG`
  uses:

  ```text
  blinds:
    - {entity: cover.a, facade_azimuth: 180, slat_distance: 60, slat_depth: 80}
  zones:
    z: {members: [cover.a]}
  values:
    angle: {type: slat_angle, default: 50}
  modes:
    - {id: day}
  rules:
    day.z:
      - {then: {tilt: !ref angle}}
  ```

  So: `rules` is keyed `"<mode>.<zone>"` with a LIST of rules under each key —
  never a list of `{mode, zone, then}` objects — and a reference is `!ref angle`,
  never `{"ref": "angle"}`. (`{"ref": name}` is the **subentry/JSON** spelling
  that `config_store.py` reads from `.storage`; the two doors differ here and
  Task 6 is the one that touches the JSON side.)
- **`ruff` is a blocking CI job, not advisory** (`.github/workflows/test.yml` runs
  `ruff check .` and `ruff format --check .`). Run BOTH before every commit. Two rules bite
  this work specifically: `PLR2004` forbids a bare magic number in production code (named
  constants only — it is exempted for `tests/**`, not for `custom_components/**`), and
  `PT018` forbids a compound `assert a and b` in a test (split it into two asserts).
- Run the pure suite with `python3 -m pytest tests/ -q` and the full suite with `.venv/bin/python -m pytest tests/ -q` (see `MODELS.md` §"Running the tests"). **Both** must pass before every commit.

---

## File Structure

| File | Responsibility |
|---|---|
| `custom_components/cover_logic/geometry.py` | **New.** Pure solar-geometry maths: `gamma()` and `slat_angle_percent()`. Nothing else. |
| `custom_components/cover_logic/model.py` | Adds `SlatAngle`, widens `Value`, adds `Blind.slat_distance` / `Blind.slat_depth`. |
| `custom_components/cover_logic/config_schema.py` | Parses/dumps the two `values:` shapes; reports a `SlatAngle`'s entity reads. |
| `custom_components/cover_logic/engine.py` | Threads `target` into `resolve_action` / `_resolve_value`; resolves `SlatAngle`. |
| `custom_components/cover_logic/guards.py` | Passes the fired blind's `Blind` into `resolve_action`. |
| `custom_components/cover_logic/readiness.py` | A `SlatAngle` axis reads the sun entities, so it can block a blind. |
| `custom_components/cover_logic/capabilities.py` | A `SlatAngle` tilt axis needs `SET_TILT_POSITION`. |
| `custom_components/cover_logic/validation.py` | Two new WARNING codes. |
| `custom_components/cover_logic/subentry_flow.py` | The `value` add/edit form grows a type selector and the geometry fields. |
| `custom_components/cover_logic/config_store.py` | Builds the new value shape from subentries. |
| `custom_components/cover_logic/strings.json` + `translations/*.json` | New form labels and issue text. |
| `tests/test_geometry.py` | **New.** The formula, including every "undefined" branch. |
| `tests/test_model.py`, `test_config_schema.py`, `test_engine.py`, `test_guards.py`, `test_readiness.py`, `test_capabilities.py`, `test_validation.py`, `test_config_store.py`, `test_purity.py` | Extended per task. |
| `tests/ha/test_options_flow.py`, `tests/ha/test_capabilities_mirror.py` | Extended in Task 6. |

---

### Task 1: Pure solar geometry

**Files:**
- Create: `custom_components/cover_logic/geometry.py`
- Create: `tests/test_geometry.py`
- Modify: `tests/test_purity.py:12-42` (add `"geometry.py"` to `PURE_MODULES`)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `gamma(azimuth: float, facade_azimuth: float) -> float` — signed degrees between sun and facade normal, in `[-180, 180)`.
  - `slat_angle_percent(elevation: float, gamma_deg: float, slat_distance: float, slat_depth: float, scale: str = "half") -> int | None` — tilt percentage, or `None` when the geometry has no answer.

- [ ] **Step 1: Write the failing test**

Create `tests/test_geometry.py`:

```python
"""The solar geometry behind a computed slat angle, and every way it has no answer."""

import pytest

from cover_logic.geometry import gamma, slat_angle_percent


def test_gamma_is_zero_when_the_sun_is_dead_on_the_facade():
    assert gamma(180.0, 180.0) == pytest.approx(0.0)


def test_gamma_is_signed_and_wraps_the_short_way_around_north():
    # 10 degrees east of a north-facing facade, not 350 degrees west of it.
    assert gamma(10.0, 0.0) == pytest.approx(10.0)
    assert gamma(350.0, 0.0) == pytest.approx(-10.0)


def test_a_known_geometry_gives_a_known_percentage():
    # 45 deg elevation, sun on the normal, 60/80 mm slats: the formula gives a
    # slat angle of ~103 deg, which is past fully closed on a 90 deg scale.
    assert slat_angle_percent(45.0, 0.0, 60.0, 80.0) == 100


def test_a_low_sun_needs_less_closing_than_a_high_one():
    low = slat_angle_percent(15.0, 0.0, 60.0, 80.0)
    high = slat_angle_percent(60.0, 0.0, 60.0, 80.0)
    assert low is not None and high is not None
    assert low < high


def test_the_full_scale_halves_the_percentage_of_the_half_scale():
    half = slat_angle_percent(20.0, 0.0, 60.0, 80.0, scale="half")
    full = slat_angle_percent(20.0, 0.0, 60.0, 80.0, scale="full")
    assert half is not None and full is not None
    assert full == half // 2


@pytest.mark.parametrize(
    ("elevation", "gamma_deg", "distance", "depth", "why"),
    [
        (0.0, 0.0, 60.0, 80.0, "sun exactly on the horizon"),
        (-5.0, 0.0, 60.0, 80.0, "sun below the horizon"),
        (30.0, 90.0, 60.0, 80.0, "sun exactly edge-on to the facade"),
        (30.0, 120.0, 60.0, 80.0, "sun behind the facade"),
        (30.0, 0.0, 60.0, 0.0, "zero slat depth"),
        (30.0, 0.0, -1.0, 80.0, "negative slat distance"),
        (5.0, 0.0, 200.0, 80.0, "slats too far apart to ever block this sun"),
    ],
)
def test_no_answer_returns_none(elevation, gamma_deg, distance, depth, why):
    assert slat_angle_percent(elevation, gamma_deg, distance, depth) is None, why


def test_the_result_never_leaves_the_axis_range():
    for elevation in range(1, 90):
        result = slat_angle_percent(float(elevation), 0.0, 60.0, 80.0)
        assert result is None or 0 <= result <= 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_geometry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cover_logic.geometry'`

- [ ] **Step 3: Write minimal implementation**

Create `custom_components/cover_logic/geometry.py`:

```python
"""Solar geometry for a computed slat angle -- pure maths, no Home Assistant.

The slat formula is the one `basbruss/adaptive-cover` uses, which cites
MDPI Energies 13/7/1731. See docs/rationale.md -- "Why the computed slat
angle clamps when the engine does not".
"""

import math

SCALE_HALF = "half"
SCALE_FULL = "full"
SCALES = (SCALE_HALF, SCALE_FULL)

_AXIS_MIN = 0
_AXIS_MAX = 100


def gamma(azimuth: float, facade_azimuth: float) -> float:
    """Signed degrees from the facade normal to the sun, wrapped to [-180, 180)."""
    return (azimuth - facade_azimuth + 180.0) % 360.0 - 180.0


def slat_angle_percent(
    elevation: float,
    gamma_deg: float,
    slat_distance: float,
    slat_depth: float,
    scale: str = SCALE_HALF,
) -> int | None:
    """The tilt percentage that just blocks the direct beam, or `None` if undefined.

    `None` is a real answer -- sun down, sun off this facade, or slats that
    cannot block this beam at all -- and the caller turns it into the value's
    stated `default`.
    """
    if elevation <= 0.0 or slat_depth <= 0.0 or slat_distance < 0.0:
        return None
    # At or past edge-on there is no beam to block, and cos(gamma) would flip sign.
    if abs(gamma_deg) >= 90.0:
        return None

    tan_beta = math.tan(math.radians(elevation)) / math.cos(math.radians(gamma_deg))
    ratio = slat_distance / slat_depth
    discriminant = tan_beta**2 - ratio**2 + 1.0
    if discriminant < 0.0:
        return None

    slat = 2.0 * math.atan((tan_beta + math.sqrt(discriminant)) / (1.0 + ratio))
    span = 90.0 if scale == SCALE_HALF else 180.0
    percent = math.degrees(slat) / span * 100.0
    # Clamping belongs to the unit conversion, not to policy: degrees past the
    # slat's own travel are not a position this blind has.
    return max(_AXIS_MIN, min(_AXIS_MAX, int(percent)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_geometry.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Add the module to the purity gate**

In `tests/test_purity.py`, add `"geometry.py"` to the `PURE_MODULES` list (keep the list's existing order convention).

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_purity.py -q`
Expected: PASS

- [ ] **Step 6: Record the clamping decision**

Append to `docs/rationale.md` a section titled **"Why the computed slat angle clamps when the engine does not"**, stating: the engine deliberately does not clamp a resolved `Ref` (a helper's value is the user's business), but a slat angle in degrees past the slat's own travel is not a position the hardware has, so the degrees→percent conversion clamps. Two sentences, no timeline.

- [ ] **Step 7: Commit**

```bash
cd /config/dev/_wt/slat-angle
git add custom_components/cover_logic/geometry.py tests/test_geometry.py tests/test_purity.py docs/rationale.md
git commit -m "Add pure solar geometry for a computed slat angle"
```

---

### Task 2: Model and config round-trip

**Files:**
- Modify: `custom_components/cover_logic/model.py:80-113` (`Ref`, `Value`, `Blind`)
- Modify: `custom_components/cover_logic/config_schema.py:81` (`_VALUE_KEYS`), `:479-482` (dump), `:512-530` (`_parse_values`), `_BLIND_KEYS` and `_blind_to_dict`
- Test: `tests/test_model.py`, `tests/test_config_schema.py`

**Interfaces:**
- Consumes: `geometry.SCALES` from Task 1.
- Produces:
  - `model.SlatAngle(default: int, scale: str, sun_entity: str, azimuth_entity: str, azimuth_attribute: str | None, elevation_entity: str, elevation_attribute: str | None)` — frozen, slots.
  - `model.Value = int | Keep | Ref | SlatAngle`
  - `model.Blind.slat_distance: float | None`, `model.Blind.slat_depth: float | None`
  - `Config.values: dict[str, Ref | SlatAngle]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_schema.py`:

```python
def test_a_slat_angle_value_parses_with_its_defaults_filled_in():
    from cover_logic.config_schema import load_config
    from cover_logic.model import SlatAngle

    config = load_config(
        {
            "blinds": [
                {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
            ],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 50}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    value = config.values["angle"]
    assert isinstance(value, SlatAngle)
    assert value.default == 50
    assert value.scale == "half"
    assert value.sun_entity == "sun.sun"
    assert value.azimuth_entity == "sensor.sun_solar_azimuth"
    assert value.elevation_entity == "sun.sun"
    assert value.elevation_attribute == "elevation"
    assert config.blinds["cover.a"].slat_distance == 60.0
    assert config.blinds["cover.a"].slat_depth == 80.0


def test_an_entity_value_still_parses_as_a_ref():
    from cover_logic.config_schema import load_config
    from cover_logic.model import Ref

    config = load_config(
        {
            "blinds": [{"entity": "cover.a"}],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"pos": {"entity": "input_number.x", "default": 34}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"position": {"ref": "pos"}}}],
        }
    )
    assert config.values["pos"] == Ref(entity="input_number.x", default=34)


def test_a_slat_angle_value_rejects_an_entity_key():
    import pytest

    from cover_logic.config_schema import ConfigError, load_config

    with pytest.raises(ConfigError, match="slat_angle"):
        load_config(
            {
                "blinds": [{"entity": "cover.a"}],
                "zones": {"z": {"members": ["cover.a"]}},
                "values": {"angle": {"type": "slat_angle", "default": 50, "entity": "sensor.x"}},
                "modes": [{"id": "day"}],
                "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
            }
        )


def test_a_slat_angle_value_survives_a_dump_and_reload():
    from cover_logic.config_schema import dump_config, load_config

    raw = {
        "blinds": [
            {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
        ],
        "zones": {"z": {"members": ["cover.a"]}},
        "values": {"angle": {"type": "slat_angle", "default": 50, "scale": "full"}},
        "modes": [{"id": "day"}],
        "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
    }
    once = load_config(raw)
    twice = load_config(__import__("yaml").safe_load(dump_config(once)))
    assert twice.values == once.values
    assert twice.blinds == once.blinds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_config_schema.py -q -k "slat_angle or entity_value"`
Expected: FAIL — `ImportError: cannot import name 'SlatAngle'`

- [ ] **Step 3: Write minimal implementation**

In `model.py`, after the `Ref` dataclass (line 89):

```python
@dataclass(frozen=True, slots=True)
class SlatAngle:
    """A tilt percentage computed from solar geometry at evaluation time.

    Target-relative: the facade and slat geometry come from the blind being
    decided, so one entry serves a house of any orientation. `default` is used
    when the geometry has no answer -- see `geometry.slat_angle_percent`.
    """

    default: int
    scale: str
    sun_entity: str
    azimuth_entity: str
    azimuth_attribute: str | None
    elevation_entity: str
    elevation_attribute: str | None


Value = int | Keep | Ref | SlatAngle
```

Delete the old `Value = int | Keep | Ref` line. In `Blind`, after `has_tilt`:

```python
    slat_distance: float | None = None
    slat_depth: float | None = None
```

In `config_schema.py`, replace `_VALUE_KEYS` (line 81 area) and `_parse_values`:

```python
_VALUE_KEYS_ENTITY = {"default", "entity"}
_VALUE_KEYS_SLAT = {
    "default",
    "type",
    "scale",
    "sun_entity",
    "azimuth_entity",
    "azimuth_attribute",
    "elevation_entity",
    "elevation_attribute",
}
VALUE_TYPE_ENTITY = "entity"
VALUE_TYPE_SLAT_ANGLE = "slat_angle"


def _parse_values(raw: dict[str, Any]) -> dict[str, Ref | SlatAngle]:
    out: dict[str, Ref | SlatAngle] = {}
    for name, raw_body in raw.items():
        body = _expect_mapping(raw_body, f"value {name!r}")
        kind = body.get("type", VALUE_TYPE_ENTITY)
        if kind == VALUE_TYPE_SLAT_ANGLE:
            out[name] = _parse_slat_angle(name, body)
        elif kind == VALUE_TYPE_ENTITY:
            out[name] = _parse_entity_value(name, body)
        else:
            msg = f"value {name!r} has unknown type {kind!r}"
            raise ConfigError(msg)
    return out


def _value_default(name: str, body: dict[str, Any]) -> int:
    try:
        default = int(body["default"])
    except (KeyError, TypeError, ValueError) as err:
        msg = f"value {name!r} needs an integer 'default'"
        raise ConfigError(msg) from err
    # See docs/rationale.md -- "Why a `!ref` default is range-checked
    # exactly like a literal".
    if not _AXIS_MIN <= default <= _AXIS_MAX:
        msg = f"value {name!r} default must be 0..100, got {default}"
        raise ConfigError(msg)
    return default


def _parse_entity_value(name: str, body: dict[str, Any]) -> Ref:
    _check_keys(body, _VALUE_KEYS_ENTITY, f"value {name!r}")
    default = _value_default(name, body)
    try:
        entity = body["entity"]
    except KeyError as err:
        msg = f"value {name!r} needs 'entity'"
        raise ConfigError(msg) from err
    return Ref(entity=entity, default=default)


def _parse_slat_angle(name: str, body: dict[str, Any]) -> SlatAngle:
    _check_keys(body, _VALUE_KEYS_SLAT, f"value {name!r} of type slat_angle")
    scale = body.get("scale", SCALE_HALF)
    if scale not in SCALES:
        msg = f"value {name!r} of type slat_angle has unknown scale {scale!r}"
        raise ConfigError(msg)
    return SlatAngle(
        default=_value_default(name, body),
        scale=scale,
        sun_entity=body.get("sun_entity", SUN_ENTITY),
        azimuth_entity=body.get("azimuth_entity", DEFAULT_AZIMUTH_ENTITY),
        azimuth_attribute=body.get("azimuth_attribute"),
        elevation_entity=body.get("elevation_entity", SUN_ENTITY),
        elevation_attribute=body.get("elevation_attribute", "elevation"),
    )
```

Add to the imports at the top of `config_schema.py`: `from .geometry import SCALE_HALF, SCALES` and add `SlatAngle` to the existing `from .model import ...`.

Replace the dump block at line 479:

<!-- text, not python: this fragment is indented for its destination, and ruff format would dedent it and overrun line-length at its real indent -->
```text
    if config.values:
        doc["values"] = {
            name: _value_to_dict(value) for name, value in sorted(config.values.items())
        }
```

and add, next to `_blind_to_dict`:

```python
def _value_to_dict(value: Ref | SlatAngle) -> dict[str, Any]:
    if isinstance(value, Ref):
        return {"entity": value.entity, "default": value.default}
    body: dict[str, Any] = {"type": VALUE_TYPE_SLAT_ANGLE, "default": value.default}
    if value.scale != SCALE_HALF:
        body["scale"] = value.scale
    if value.sun_entity != SUN_ENTITY:
        body["sun_entity"] = value.sun_entity
    if value.azimuth_entity != DEFAULT_AZIMUTH_ENTITY:
        body["azimuth_entity"] = value.azimuth_entity
    if value.azimuth_attribute is not None:
        body["azimuth_attribute"] = value.azimuth_attribute
    if value.elevation_entity != SUN_ENTITY:
        body["elevation_entity"] = value.elevation_entity
    if value.elevation_attribute != "elevation":
        body["elevation_attribute"] = value.elevation_attribute
    return body
```

Add `"slat_distance"` and `"slat_depth"` to `_BLIND_KEYS`, and in `_parse_blind` read them with `float(...)` when present (`None` otherwise). In `_blind_to_dict`, emit each only when not `None`.

Widen the annotations that mention values: `Config.values` in `model.py`, and the `values: dict[str, Ref]` parameters in `_parse_axis`, `_parse_action`, `_parse_guard`, `parse_guards`, `_parse_rule` become `dict[str, Ref | SlatAngle]`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_config_schema.py tests/test_model.py -q`
Expected: PASS

- [ ] **Step 5: Run both full suites**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/ -q && .venv/bin/python -m pytest tests/ -q`
Expected: PASS, same counts as before this task plus the new tests. **If `tests/parity/test_migration_gate.py` fails, stop** — Phase 1 must not change any decision.

- [ ] **Step 6: Commit**

```bash
cd /config/dev/_wt/slat-angle
git add custom_components/cover_logic/model.py custom_components/cover_logic/config_schema.py tests/
git commit -m "Model and parse a computed slat_angle value"
```

---

### Task 3: Target-relative resolution in the engine

**Files:**
- Modify: `custom_components/cover_logic/engine.py:240` (call site), `:245-267` (`resolve_action`, `_resolve_value`)
- Modify: `custom_components/cover_logic/guards.py:228,294,316-339` (`_outcome` gains the blind)
- Test: `tests/test_engine.py`, `tests/test_guards.py`

**Interfaces:**
- Consumes: `model.SlatAngle` (Task 2), `geometry.gamma` / `geometry.slat_angle_percent` (Task 1).
- Produces:
  - `engine.resolve_action(action: Action, world: World, target: Target | None) -> Action` — **third parameter is now required**; `None` means "no blind in hand", which makes a `SlatAngle` fall back to its `default`.
  - `guards._outcome(index: int, guard: Guard, entity: str, world: World, held: Action | None, target: Target | None) -> Outcome`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_engine.py`:

```python
def test_a_slat_angle_axis_resolves_from_the_target_blinds_facade(make_world):
    """Two blinds, two facades, one value: each gets its own angle."""
    from cover_logic.config_schema import load_config
    from cover_logic.engine import evaluate

    config = load_config(
        {
            "blinds": [
                {
                    "entity": "cover.south",
                    "facade_azimuth": 180,
                    "slat_distance": 60,
                    "slat_depth": 80,
                },
                {
                    "entity": "cover.west",
                    "facade_azimuth": 270,
                    "slat_distance": 60,
                    "slat_depth": 80,
                },
            ],
            "zones": {"z": {"members": ["cover.south", "cover.west"]}},
            "values": {"angle": {"type": "slat_angle", "default": 7}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    world = make_world(
        states={"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": "180"},
        attributes={("sun.sun", "elevation"): "40"},
    )
    decision = evaluate(config, world)

    south = decision.targets["cover.south"].tilt
    west = decision.targets["cover.west"].tilt
    # The sun is on the south facade and edge-on-or-behind the west one.
    assert isinstance(south, int) and south > 0
    assert west == 7


def test_a_slat_angle_axis_falls_back_to_its_default_at_night(make_world):
    from cover_logic.config_schema import load_config
    from cover_logic.engine import evaluate

    config = load_config(
        {
            "blinds": [
                {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
            ],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 42}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    world = make_world(
        states={"sun.sun": "below_horizon", "sensor.sun_solar_azimuth": "180"},
        attributes={("sun.sun", "elevation"): "-20"},
    )
    assert evaluate(config, world).targets["cover.a"].tilt == 42


def test_a_slat_angle_axis_falls_back_when_the_blind_has_no_geometry(make_world):
    from cover_logic.config_schema import load_config
    from cover_logic.engine import evaluate

    config = load_config(
        {
            "blinds": [{"entity": "cover.a", "facade_azimuth": 180}],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 11}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    world = make_world(
        states={"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": "180"},
        attributes={("sun.sun", "elevation"): "40"},
    )
    assert evaluate(config, world).targets["cover.a"].tilt == 11
```

If `tests/test_engine.py` has no `make_world` fixture with an `attributes` argument, read `tests/conftest.py` first and use whatever builder the existing engine tests use, keeping the same call style.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_engine.py -q -k slat_angle`
Expected: FAIL — the tilt comes back as the `SlatAngle` object, not an int (a `Ref`-shaped object reaching an axis is exactly what `planner.plan` raises on).

- [ ] **Step 3: Write minimal implementation**

In `engine.py`, change the call site at line 240 to `resolve_action(rule.then, world, target)` and replace the two functions:

```python
def resolve_action(action: Action, world: World, target: Target | None) -> Action:
    """Resolve both axes of `action` against `world`, leaving `KEEP` alone.

    Public because a `force` guard's `then` has to be resolved the same way a
    rule's `then` is -- same truncation, same unclamped result. Two spellings
    of that would be two answers to "what does `!ref` mean", and only one of
    them would be parity-checked.

    `target` is what makes a computed value target-relative; `None` means no
    blind is in hand, which is a fall back to the value's own `default`.
    """
    return Action(
        position=_resolve_value(action.position, world, target),
        tilt=_resolve_value(action.tilt, world, target),
    )


def _resolve_value(value: Value, world: World, target: Target | None) -> Value:
    """Resolve a `Ref` or `SlatAngle`, truncated and unclamped.

    See docs/rationale.md -- "Why `_resolve_value` truncates instead of
    rounding" and "Why the engine does not clamp resolved values to 0..100".
    """
    if isinstance(value, Ref):
        return int(world.number(value.entity, default=float(value.default)))
    if isinstance(value, SlatAngle):
        return _resolve_slat_angle(value, world, target)
    return value


def _resolve_slat_angle(value: SlatAngle, world: World, target: Target | None) -> int:
    """The computed angle, or the value's `default` when the geometry has no answer."""
    blind = None if target is None else target.blind
    if (
        blind is None
        or blind.facade_azimuth is None
        or blind.slat_distance is None
        or blind.slat_depth is None
        or world.state(value.sun_entity) != "above_horizon"
    ):
        return value.default

    azimuth = world.number(value.azimuth_entity, default=-1.0, attribute=value.azimuth_attribute)
    if not 0.0 <= azimuth < 360.0:
        return value.default
    elevation = world.number(
        value.elevation_entity, default=-999.0, attribute=value.elevation_attribute
    )
    if elevation < -90.0:
        return value.default

    computed = slat_angle_percent(
        elevation,
        gamma(azimuth, blind.facade_azimuth),
        blind.slat_distance,
        blind.slat_depth,
        value.scale,
    )
    return value.default if computed is None else computed
```

Add to `engine.py`'s imports: `from .geometry import gamma, slat_angle_percent` and add `SlatAngle` to the `from .model import ...` line.

In `guards.py`, give `_outcome` a `target` parameter and pass it through:

```python
def _outcome(
    index: int,
    guard: Guard,
    entity: str,
    world: World,
    held: Action | None,
    target: Target | None,
) -> Outcome:
```

and at line 339 `return Outcome(action=resolve_action(guard.then, world, target), **common)`.

At both call sites, **a `target` variable is already in scope** — `screen()` builds it at `guards.py:220` and `review()` at `guards.py:277`, both inside the very loop that calls `_outcome`. So pass `target`; do not re-derive ownership. (`screen()` also `continue`s past any entity not in `owners` at line 215, so the target is always a real `Target`, never `None`, at both sites.)

**One more change in this file, which the rest of this plan missed.** `_direction_matches` at `guards.py:388` guards the invariant that a decision reaching a guard has its refs already resolved:

```python
    if isinstance(position, Ref):
        raise GuardError(...)
```

An unresolved `SlatAngle` must hit that same invariant, or it falls through to the position comparison below and an object gets compared against an int. Widen it to `isinstance(position, (Ref, SlatAngle))` and extend the error message so it names either kind rather than only a `Ref`. Add a test in `tests/test_guards.py` that an unresolved `SlatAngle` in a `decided` action raises `GuardError`, mirroring whatever test covers the `Ref` case today.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_engine.py tests/test_guards.py -q`
Expected: PASS

- [ ] **Step 5: Add the guard-side test**

Append to `tests/test_guards.py` a test that a `force` guard whose `then` holds a `slat_angle` ref resolves per blind, mirroring the engine test above: two blinds on different facades in the guard's `targets`, one `force` guard, assert the two outcomes carry different tilt ints.

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_guards.py -q`
Expected: PASS

- [ ] **Step 6: Run both full suites**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/ -q && .venv/bin/python -m pytest tests/ -q`
Expected: PASS including the parity gate.

- [ ] **Step 7: Commit**

```bash
cd /config/dev/_wt/slat-angle
git add custom_components/cover_logic/engine.py custom_components/cover_logic/guards.py tests/
git commit -m "Resolve a computed slat angle relative to the blind being decided"
```

---

### Task 4: Readiness and capabilities

**Files:**
- Modify: `custom_components/cover_logic/readiness.py:288-299` (`_action_reads`)
- Modify: `custom_components/cover_logic/capabilities.py:60-88` (`_position_feature`, `_tilt_feature`, `_is_number`)
- Modify: `custom_components/cover_logic/config_schema.py` — the `values:` branch of the entity-reporting walk (see line 848 docstring)
- Test: `tests/test_readiness.py`, `tests/test_capabilities.py`, `tests/test_config_schema.py`

**Interfaces:**
- Consumes: `model.SlatAngle`.
- Produces: no new names; extends existing behaviour so a `SlatAngle` is visible to the readiness gate, the capability check and `referenced_entities`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_readiness.py`:

```python
def test_a_slat_angle_axis_blocks_the_blind_when_the_sun_entity_is_unavailable(make_world):
    """Same rule as a Ref axis: a stated default is not a readiness answer."""
    from cover_logic.config_schema import load_config
    from cover_logic.readiness import assess

    config = load_config(
        {
            "blinds": [
                {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
            ],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 50}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    world = make_world(states={"sun.sun": "unavailable", "sensor.sun_solar_azimuth": "180"})
    readiness = assess(config, world)
    assert "cover.a" in readiness.blocked_by("sun.sun") or not readiness.ready
```

Append to `tests/test_capabilities.py`:

```python
def test_a_slat_angle_tilt_axis_needs_the_tilt_setter():
    from cover_logic.capabilities import SET_TILT_POSITION, required_features
    from cover_logic.config_schema import load_config

    config = load_config(
        {
            "blinds": [
                {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
            ],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 50}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    assert required_features(config)["cover.a"] & SET_TILT_POSITION
```

Append to `tests/test_config_schema.py`:

```python
def test_a_slat_angle_value_reports_the_sun_entities_it_reads():
    from cover_logic.config_schema import load_config, referenced_entities

    config = load_config(
        {
            "blinds": [
                {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
            ],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 50}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    named = referenced_entities(config)
    assert "sun.sun" in named
    assert "sensor.sun_solar_azimuth" in named
```

`referenced_entities` returns `set[str | tuple[str, str]]` — a plain string is a state read, a `(entity_id, attribute)` tuple is an attribute read. So the elevation read appears as `("sun.sun", "elevation")`; add `assert ("sun.sun", "elevation") in named` to the test above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_readiness.py tests/test_capabilities.py tests/test_config_schema.py -q -k slat_angle`
Expected: FAIL — the new value type is invisible to all three.

- [ ] **Step 3: Write minimal implementation**

In `readiness.py`, replace `_action_reads`:

```python
def _action_reads(action: Action) -> set[Read]:
    """The reads an action's axes perform, for per-blind attribution.

    A `Ref` axis falls back to its own `default` when the helper is unreadable
    and a `SlatAngle` axis does the same, but neither default answers the
    readiness question -- see `docs/rationale.md` -- "Why a `values:` default
    is not an answer to readiness".
    """
    reads: set[Read] = set()
    for axis in (action.position, action.tilt):
        if isinstance(axis, Ref):
            reads.add(Read(axis.entity))
        elif isinstance(axis, SlatAngle):
            reads.add(Read(axis.sun_entity))
            reads.add(Read(axis.azimuth_entity, axis.azimuth_attribute))
            reads.add(Read(axis.elevation_entity, axis.elevation_attribute))
    return reads
```

Import `SlatAngle` there. In `capabilities.py`, make each of `_position_feature`, `_tilt_feature` and `_is_number` treat `SlatAngle` exactly as they treat `Ref` (a `SlatAngle` is "could be any number", so it charges the setter). Import `SlatAngle`.

In `config_schema.py`, the `values:` branch of `referenced_reads()` needs the same three reads, **undefaulted**. **This may already be done:** Task 2's fix round had to repair it, because the widened `Config.values` made its unconditional `ref.entity` crash with `AttributeError` on a `SlatAngle`. Read it first — if the three reads are already emitted there, add nothing and say so in your report; only close what is actually missing. The same applies to `config_store.subentries_from_config()`, repaired in the same fix round.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_readiness.py tests/test_capabilities.py tests/test_config_schema.py -q`
Expected: PASS

- [ ] **Step 5: Run both full suites**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/ -q && .venv/bin/python -m pytest tests/ -q`
Expected: PASS including the parity gate and `tests/ha/test_capabilities_mirror.py`.

- [ ] **Step 6: Commit**

```bash
cd /config/dev/_wt/slat-angle
git add custom_components/cover_logic/readiness.py custom_components/cover_logic/capabilities.py custom_components/cover_logic/config_schema.py tests/
git commit -m "Make a computed slat angle visible to readiness, capabilities and entity reporting"
```

---

### Task 5: Validation warnings

**Files:**
- Modify: `custom_components/cover_logic/validation.py`
- Modify: `custom_components/cover_logic/strings.json` and every file in `custom_components/cover_logic/translations/`
- Test: `tests/test_validation.py`, `tests/test_translations.py`

**Interfaces:**
- Consumes: `model.SlatAngle`.
- Produces: two WARNING codes, `"slat_angle_without_geometry"` and `"slat_angle_on_position"`, which `__init__.py::_check_config_warnings` turns into repair issues with no further wiring.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_validation.py`:

```python
def test_a_slat_angle_on_a_blind_without_geometry_warns():
    from cover_logic.config_schema import load_config
    from cover_logic.validation import validate

    config = load_config(
        {
            "blinds": [{"entity": "cover.a", "facade_azimuth": 180}],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 50}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    codes = [problem.code for problem in validate(config)]
    assert "slat_angle_without_geometry" in codes


def test_a_slat_angle_on_a_tiltless_blind_warns_only_about_the_tilt():
    """One fault, one warning: the geometry is complete, so only the tilt check fires."""
    from cover_logic.config_schema import load_config
    from cover_logic.validation import validate

    config = load_config("""
blinds:
  - {entity: cover.a, facade_azimuth: 180, slat_distance: 60, slat_depth: 80, has_tilt: false}
zones:
  z: {members: [cover.a]}
values:
  angle: {type: slat_angle, default: 50}
modes:
  - {id: day}
rules:
  day.z:
    - {then: {tilt: !ref angle}}
""")
    codes = [problem.code for problem in validate(config)]
    assert "tilt_on_tiltless_blind" in codes
    assert "slat_angle_without_geometry" not in codes


def test_a_slat_angle_on_the_position_axis_warns():
    from cover_logic.config_schema import load_config
    from cover_logic.validation import validate

    config = load_config(
        {
            "blinds": [
                {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
            ],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 50}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"position": {"ref": "angle"}}}],
        }
    )
    codes = [problem.code for problem in validate(config)]
    assert "slat_angle_on_position" in codes


def test_a_fully_specified_slat_angle_warns_about_nothing():
    from cover_logic.config_schema import load_config
    from cover_logic.validation import validate

    config = load_config(
        {
            "blinds": [
                {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
            ],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 50}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    codes = [problem.code for problem in validate(config)]
    assert "slat_angle_without_geometry" not in codes
    assert "slat_angle_on_position" not in codes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_validation.py -q -k slat_angle`
Expected: FAIL — the codes are never emitted.

- [ ] **Step 3: Write minimal implementation**

Add to `validation.py` a check that walks every rule's and guard's `then` (reusing whatever walk the module already has for the existing action-shaped checks — read the file and follow its pattern, do not add a second walk):

- For each action axis holding a `SlatAngle`, resolve which blinds can receive it (the rule's zone members, or `guard_blinds` for a guard). Emit **one** `slat_angle_without_geometry` WARNING per offending blind, naming the blind and which of `facade_azimuth` / `slat_distance` / `slat_depth` is missing.

  **Do NOT include `has_tilt` in that check.** `_check_tilt_on_tiltless_blinds` at `validation.py:153` already warns (`tilt_on_tiltless_blind`) on *any* non-`KEEP` tilt reaching a blind with no tilt, and a `SlatAngle` is a non-`KEEP` tilt — so covering it here too would emit two warnings for one fault. Follow that function's shape: it catches `EngineError` from `resolve_ownership` and returns `[]`, because `validate` must never raise on the malformed configurations it exists to report on.

  **Deliberately do NOT register either new code in `subentry_flow._CODE_OWNERS`.** That dict decides which form a problem *blocks*, and per `tests/ha/test_subentry_flows.py`'s own docstring a code in neither it nor `_ATTRIBUTED_CODES` "silently blocks nothing at all". That is the correct outcome here and matches `tilt_on_tiltless_blind`, which is also absent from it: both codes describe a configuration that still works and merely does less than it reads as, so a save should not be blocked. The user is still told — `__init__.py::_check_config_warnings` turns every `validate()` WARNING into a repair issue regardless of `_CODE_OWNERS`. Say this in a one-line comment next to the codes so the absence reads as a decision rather than an oversight.
- If the `SlatAngle` sits on the `position` axis, emit `slat_angle_on_position` naming the rule or guard.

Follow the module's existing `Problem` construction and severity constants exactly; both new codes are WARNING, not ERROR, because a stated `default` means the house still gets a decision.

Add matching text for both codes to `strings.json` under the same key path the existing warning codes use, and to every file in `translations/`. `tests/test_translations.py` enforces that the key sets match across files — run it to find the exact paths rather than guessing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_validation.py tests/test_translations.py -q`
Expected: PASS

- [ ] **Step 5: Run both full suites**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/ -q && .venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd /config/dev/_wt/slat-angle
git add custom_components/cover_logic/validation.py custom_components/cover_logic/strings.json custom_components/cover_logic/translations tests/
git commit -m "Warn when a computed slat angle cannot apply to the blinds that would receive it"
```

---

### Task 6: The UI — value form, blind form, subentry store

**Files:**
- Modify: `custom_components/cover_logic/subentry_flow.py` (the `value` flow and the `blind` flow)
- Read only (needs no change, see Step 3): `custom_components/cover_logic/config_store.py`
- Modify: `custom_components/cover_logic/strings.json`, `translations/*.json`
- Test: `tests/test_config_store.py`, `tests/ha/test_options_flow.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: a `value` subentry may carry `{"type": "slat_angle", "default": int, "scale": str}` and a `blind` subentry may carry `slat_distance` / `slat_depth`; `config_store.build_config` turns both into the same `Config` the YAML path produces.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_store.py`:

```python
def test_a_slat_angle_value_subentry_builds_the_same_object_as_yaml():
    """One owner, two doors: the UI path and the YAML path must agree."""
    from cover_logic.config_schema import load_config
    from cover_logic.model import SlatAngle

    from_yaml = load_config(
        {
            "blinds": [
                {"entity": "cover.a", "facade_azimuth": 180, "slat_distance": 60, "slat_depth": 80}
            ],
            "zones": {"z": {"members": ["cover.a"]}},
            "values": {"angle": {"type": "slat_angle", "default": 50, "scale": "half"}},
            "modes": [{"id": "day"}],
            "rules": [{"mode": "day", "zone": "z", "then": {"tilt": {"ref": "angle"}}}],
        }
    )
    assert from_yaml.values["angle"] == SlatAngle(
        default=50,
        scale="half",
        sun_entity="sun.sun",
        azimuth_entity="sensor.sun_solar_azimuth",
        azimuth_attribute=None,
        elevation_entity="sun.sun",
        elevation_attribute="elevation",
    )
    assert from_yaml.blinds["cover.a"].slat_distance == 60.0
```

Then extend the existing subentry→`Config` test in that file (find the test that builds a `Config` from a list of fake subentries and copy its style) with a `value` subentry of `{"id": "angle", "type": "slat_angle", "default": 50}` and a `blind` subentry carrying `slat_distance`/`slat_depth`, asserting the resulting `Config.values["angle"]` is the `SlatAngle` above and the blind carries the geometry.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_config_store.py -q -k slat_angle`
Expected: FAIL — `config_store` drops the unknown keys or raises.

- [ ] **Step 3: Write minimal implementation**

**`config_store.py` needs NO change — verified, not assumed.** `_build_values` (`config_store.py:207`) strips `_ID_KEY` and hands the rest straight to `config_schema._parse_values`, which Task 2 taught to dispatch on `type`; and the `blind` branch already passes unknown keys through. Proven by direct call against a stand-in entry: a `value` subentry `{"id": "angle", "type": "slat_angle", "default": 50, "scale": "full"}` builds a correct `SlatAngle`, and `{"id": "poz", "entity": "input_number.x", "default": 34}` still builds a `Ref`. The export direction was already fixed in Task 2's fix round. **Read it, confirm it, add nothing, and say so in your report.**

In `subentry_flow.py`:
- The `value` add/edit form gains a `type` `SelectSelector` with options `entity` and `slat_angle`. When `slat_angle` is chosen, the form shows `default` (`NumberSelector` 0–100) and `scale` (`SelectSelector` `half`/`full`) and **not** `entity`; when `entity` is chosen it shows today's fields. Follow the file's existing two-step pattern if it already has one (read `_build_schema` / `_to_data` / `_to_form_values` first); if it does not, add a menu step that picks the type and then routes to the right form.
- The `blind` form gains optional `slat_distance` and `slat_depth` `NumberSelector` fields, in millimetres, described as "measured on the blind".
- `_to_data` must not write `slat_distance: None` — omit an unset field, or `test_subentry_conformance.py` will see drift against the fixture.

Add every new form label, description and selector option to `strings.json` and all `translations/*.json`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/test_config_store.py -q && .venv/bin/python -m pytest tests/ha/test_options_flow.py tests/test_translations.py -q`
Expected: PASS

- [ ] **Step 5: Add a flow test**

Append to `tests/ha/test_options_flow.py` a test that walks the `value` subentry flow choosing `slat_angle`, submits `default: 50` and `scale: half`, and asserts the created subentry's `data` equals `{"id": ..., "type": "slat_angle", "default": 50, "scale": "half"}`. Mirror the assertions and helper usage of the neighbouring `value` flow test in that file.

Run: `cd /config/dev/_wt/slat-angle && .venv/bin/python -m pytest tests/ha/test_options_flow.py -q`
Expected: PASS

- [ ] **Step 6: Run both full suites**

Run: `cd /config/dev/_wt/slat-angle && python3 -m pytest tests/ -q && .venv/bin/python -m pytest tests/ -q`
Expected: PASS. `tests/parity/test_subentry_conformance.py` must still pass — it reads the real `.storage`, which this task does not touch.

- [ ] **Step 7: Commit**

```bash
cd /config/dev/_wt/slat-angle
git add custom_components/cover_logic tests/
git commit -m "Let a computed slat angle and slat geometry be configured from the UI"
```

---

### Task 7: Prove the capability changes nothing, then document and release

**Files:**
- Modify: `MODELS.md` (**§4 The configuration format** — the `values:` section; §3 is the decision model)
- Modify: `README.md` (feature list)
- Modify: `docs/rationale.md` (one new section, see below)
- Create: `docs/example-slat-angle.md` **only if** `README.md` has no room for a worked example; otherwise skip and put it in the README.

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: a released version carrying an unused capability, and documentation a stranger can follow.

- [ ] **Step 1: Prove the house is unaffected**

Run the migration gate alone and record the number:

```bash
cd /config/dev/_wt/slat-angle && .venv/bin/python -m pytest tests/parity/test_migration_gate.py -q
```

Expected: PASS, the same scenario count as before this plan (92 160). **This is the gate for the whole of Phase 1**: no `slat_angle` value exists in the house's fixture yet, so every decision must be identical.

- [ ] **Step 2: Prove the deployed house config is untouched**

```bash
cd /config/dev/_wt/slat-angle && .venv/bin/python -m pytest tests/parity/test_subentry_conformance.py -q
```

Expected: PASS — the live `.storage` still matches `fixtures/dom_peter.yaml`, because Phase 1 added no subentry.

- [ ] **Step 3: Document the format**

In `MODELS.md` §4, under `values:`, document both shapes:

```yaml
values:
  kvety_poz:                    # type: entity (the default) -- read a helper
    entity: input_number.kvety_pozicia_zaluzie
    default: 34
  uhol_lamiel:                  # type: slat_angle -- computed from solar geometry
    type: slat_angle
    default: 50                 # used when the geometry has no answer
    scale: half                 # half: 0..90 deg maps to 0..100; full: 0..180
```

State the three facts a reader needs: the geometry comes from the **blind being decided** (`facade_azimuth`, `slat_distance`, `slat_depth`), so one entry serves every facade; `default` applies when the sun is down, off the facade, or the slats cannot block the beam; and the percentage is clamped to 0..100 while a `Ref` is not.

In `README.md`, add one bullet to the feature list and a three-line worked example.

- [ ] **Step 4: Note what this does NOT do**

Append to `docs/rationale.md` a section **"Why a computed slat angle does not imply a computed height"**: the vertical-shading formula (`distance / cos(gamma) * tan(elevation)`) is deliberately not implemented, because measurement on this house closed the question of daytime height control — 31 height changes in the 9–18 window over 14 days, 19 of them by a person — and a computed height would re-open a fight the owner already decided. Two or three sentences.

- [ ] **Step 5: Run everything one more time**

```bash
cd /config/dev/_wt/slat-angle && python3 -m pytest tests/ -q && .venv/bin/python -m pytest tests/ -q && ruff check . && ruff format --check .
```

Expected: all PASS.

- [ ] **Step 6: Commit and release per MODELS.md**

```bash
cd /config/dev/_wt/slat-angle
git add MODELS.md README.md docs/
git commit -m "Document the computed slat_angle value"
```

**Note for whoever runs Task 7:** the release, the deploy and the merge happen in the MAIN
checkout (`/config/dev/cover-logic`) after this branch is merged via PR — not in the worktree.
Every `cd` above points at the worktree because Tasks 1-6 live there.

Then release. `MODELS.md` has **no** release section, so the process is the repo's convention, confirmed by `git log`: bump `"version"` in `custom_components/cover_logic/manifest.json` (currently `0.4.0` → `0.5.0`, since this adds a capability), commit as `Release 0.5.0`, tag `v0.5.0`, push the tag, then `gh release create`. **`git push --tags` is not a release** — HACS goes by releases, so verify with `gh release list`, not `git ls-remote --tags`.

The release note leads with: a new computed `values:` type that derives a venetian blind's tilt from solar geometry, **unused until a rule references it**, so upgrading changes nothing. Name the two new `blind` fields, and say that a blind missing them raises a repair issue only if a rule actually points a `slat_angle` at it.

- [ ] **Step 7: Deploy to the house, unused**

```bash
/config/dev/_tools/deploy-cover-logic.sh
```

Then restart Home Assistant and verify the three post-deployment checks from `/config/CLAUDE.md`: (1) `sensor.cover_logic_mode`'s `trace` names the house's **own** zones, (2) `matica_diff` is `[]`, (3) the `rule` subentry count in `.storage` equals the fixture's. Also verify **zero blinds moved** across the restart, and that no new repair issue appeared.

---

### Task 8: Phase 2 gate — measure before switching any rule

**Files:**
- Create: `/config/docs/superpowers/specs/2026-XX-XX-slat-angle-switch-measurement.md` (date it the day it is run)

**Interfaces:**
- Consumes: the released, unused capability from Task 7.
- Produces: a decision, with numbers, on whether to switch `horucava`'s nine `tilt: 50` rules — and nothing else. **This task changes no config.**

- [ ] **Step 1: Measure what the change would do**

Replay the last 14 days of `sun.sun` elevation and `sensor.sun_solar_azimuth` history against `geometry.slat_angle_percent` for each of the three facades (180°, 90°, 270°) using the house's measured slat geometry. Produce, per facade: the distribution of computed angles during the hours when `sun_hits_target` is true, and the difference from the constant 50.

Read history with `/api/history/period` and **remember `significant_changes_only=0` and an explicit `end_time`** (see `/config/CLAUDE.md`), or via the recorder DB directly.

- [ ] **Step 2: Establish the tilt direction empirically**

**This is the step that can invalidate the whole idea.** `adaptive-cover` ships an `inverse_state` option because vendors disagree on which end of the tilt range is "closed". Before any rule changes, on **one** blind, at a known sun position, set tilt to the computed value and to `100 - computed` and look at which one actually blocks the beam. Record the answer. If the house's motors are inverted relative to the formula, the fix belongs in `geometry.py` as an explicit `invert` argument on the value — not as arithmetic sprinkled in the rules.

- [ ] **Step 3: Write the measurement up and stop**

Write the spec file with: the per-facade distributions, the direction finding, and an explicit recommendation. Then **stop and ask the owner**. Switching the rules means the 92 160-scenario gate can no longer prove `horucava` parity — the old Jinja matrix returns the constant — so its `horucava` basis has to be rewritten, and that is a decision, not a step.

- [ ] **Step 4: Commit the measurement**

```bash
cd /config && git add docs/superpowers/specs/*slat-angle-switch-measurement.md 2>/dev/null || true
```

(`/config` is not a git repository; if the command fails, that is expected — the file simply lives on disk.)

---

## Self-Review

**Spec coverage.** The spec's "Doplnok" finding 1 (the hard-coded `tilt: 50` in nine `horucava` rules) is what Tasks 1–7 build the capability for and Task 8 gates the use of. Finding 2 (`pocasie_otvorene` decides from a state enum, not from `cloud_coverage`) is **deliberately not in this plan** — it needs no new capability, it is a one-condition edit, and bundling it would put two unrelated behaviour changes behind one parity rewrite. It stays a separate piece of work. The spec's "Čo netreba brať" list is honoured: no min/max elevation (already expressible), no `delta_time`, no interpolation, no climate mode. The spec's verified note about `sensor.sun_solar_azimuth` being reported through an implicit default is the mechanism Task 4 extends.

**Placeholder scan.** Three steps deliberately say "read the file first and follow its pattern" rather than quoting code: `validation.py`'s existing action walk (Task 5 Step 3), `subentry_flow.py`'s form builders (Task 6 Step 3), and the `make_world` fixture shape (Task 3 Step 1). These are cases where quoting a guess would be worse than naming the pattern to match — the surrounding code is the specification, and it is long. Every other code step carries the actual code.

**Type consistency.** `SlatAngle` field names are used identically in Tasks 2, 3, 4, 5 and 6 (`default`, `scale`, `sun_entity`, `azimuth_entity`, `azimuth_attribute`, `elevation_entity`, `elevation_attribute`). `slat_angle_percent`'s signature `(elevation, gamma_deg, slat_distance, slat_depth, scale)` is the same in Task 1's implementation and Task 3's call. `resolve_action`'s third parameter is `target` in both the engine and `guards.py`. `Blind.slat_distance` / `Blind.slat_depth` are the same names in Tasks 2, 3, 5 and 6. The validation codes `slat_angle_without_geometry` and `slat_angle_on_position` are spelled identically in Task 5's tests, implementation and translations.

**One risk this plan cannot remove.** Task 8 Step 2 may find the house's tilt direction is inverted relative to the formula. That does not invalidate Tasks 1–7 — the capability, the UI and the docs stand — but it adds an `invert` field before any rule can use it.
