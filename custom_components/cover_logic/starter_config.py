"""Pure builder for `blinds_now`'s starter configuration.

Split out of `config_flow.py` (phase 6 task 4 follow-up) for exactly one
reason: `tests/test_blinds_now_starter_config.py` is the only test that can
tell "the shading rule fires" from "the shading rule can never fire" --
proven by mutation, removing `azimuth_entity`/`azimuth_attribute` from
`_build_starter_config`'s output makes it fail with `100 == 20` -- and that
test needs a real `evaluate()` run against a `World` that actually has an
azimuth to read, not merely `validate()`'s shape check
(`tests/ha/test_config_flow.py`'s `test_blinds_now_summary_creates_a_
configuration_that_decides_something` already does the latter and would
pass either way). `config_flow.py` imports `homeassistant` unconditionally
at module level, so a test importing `_build_starter_config` from there
needed `pytest.importorskip("homeassistant")` just to reach the function --
which meant it was skipped on every CI leg and on the system-Python run,
`[dev]` installing only pytest and hypothesis, never `homeassistant`. A test
that runs nowhere it matters is exactly the defect class this project keeps
shipping (see `MODELS.md` Sec. 9's translation-key-reference and rule-
grouping entries for two other instances of it). Moving the pure part here
-- no `homeassistant` import, ever, enforced by `tests/test_purity.py`'s
`PURE_MODULES` -- lets that test drop the guard and run everywhere.

`config_flow.py` imports `_build_starter_config` and the four action
constants (`_SHADE_POSITION`, `_SHADE_TILT`, `_OPEN_POSITION`, `_OPEN_TILT`)
from here rather than redefining them -- "one owner", not a second copy
kept in sync by hand.
"""

from .conditions import SUN_ENTITY
from .const import COND_SUN_HITS_TARGET, RULE_DEFAULT_ZONE
from .model import Action, Blind, Config, Mode, Rule, Zone

# `facade_azimuth` is a number of degrees (`model.Blind`, `MODELS.md` Sec. 4)
# -- correct for the engine, meaningless to someone standing in their own
# house deciding which way a window faces. Ordered clockwise from north (the
# order a compass rose is drawn in), not alphabetised, so `config_flow.py`'s
# `_FACING_SCHEMA` can pass this order straight through to a select widget
# with `sort=False` and have it read as a compass rather than a word list.
_COMPASS_TO_AZIMUTH: dict[str, float] = {
    "north": 0.0,
    "northeast": 45.0,
    "east": 90.0,
    "southeast": 135.0,
    "south": 180.0,
    "southwest": 225.0,
    "west": 270.0,
    "northwest": 315.0,
}

# The zone/mode ids `_build_starter_config` generates. Plain, short and
# English on purpose -- like every other id in this project's format
# (`MODELS.md` Sec. 4), these are internal keys read back out of subentries,
# never shown to the user translated; the *labels* around them
# (`config_flow.py`'s own `strings.json` entries) are what carry the
# explanation.
_BLINDS_NOW_ZONE = "all"
_MODE_DAY = "day"
_MODE_NIGHT = "night"

# The starter rule's shading amount: enough to cut direct sun without
# blacking the room out, on the theory that a new user tunes this to taste
# afterwards (see `config_flow.py`'s `strings.json` summary text) rather than
# never touching it -- picking values that make no difference either way
# would defeat the point of generating a working default at all.
_SHADE_POSITION = 20
_SHADE_TILT = 45
_OPEN_POSITION = 100
_OPEN_TILT = 100


def _build_starter_config(entities: list[str], facings: dict[str, str]) -> Config:
    """The configuration `blinds_now` creates: one zone, a day/night split, shading rules.

    This is the whole answer to "the entry decides nothing" (see
    `config_flow.py`'s module docstring and the phase 6 task 4 plan this
    implements): every blind picked ends up owned by one zone, and every
    `(mode, zone)` pair that zone participates in has rules that resolve to
    something.

    **One zone, not one per blind.** `blinds_now` has no notion yet of which
    blinds belong together -- that is exactly the kind of decision the
    options-flow menu exists for a user to make deliberately afterwards. A
    single zone holding every blind is the only grouping this step can offer
    without guessing, and it is enough for `validate()` to find nothing
    unowned.

    **Two modes, one inherited default each -- not one rule per zone.** This
    is deliberately the shape phase 6's task 1-3 added inheritance for
    (`RULE_DEFAULT_ZONE`, `"*"`): a rule filed under `f"{mode}.*"` decides
    every zone in that mode, so a single-zone starter set and a ten-zone one
    generated from the same picks would need the same two rule lists, not
    one pair per zone. It is also the clearest illustration of the feature
    this project's own `MODELS.md`/`docs/rationale.md` could ship with a
    fresh install.

    **Day is `sun.sun` in `above_horizon`, night is the fallback.** Not a
    `time` condition on a fixed clock: the house this project replaces
    recorded a DST bug from exactly that shape (comparing wall-clock times
    as strings without accounting for the UTC offset -- see `/config/
    CLAUDE.md`'s own entry on it), which is also why `MODELS.md` Sec. 6 has
    `World.now` stay naive local time rather than convert by hand. A state
    check on the sun entity needs no clock arithmetic at all to get "day"
    and "night" right through a DST transition. `night` carries `when=None`
    and is last -- `validation._check_modes`'s fallback requirement -- so
    "no mode matched" never happens (`engine.evaluate` would otherwise raise
    `EngineError`).

    **Night's one rule is `keep`/`keep`, named, not an empty rule list.**
    `engine._apply_rules` already treats "no rules for this key" as keep/keep
    (see `MODELS.md` Sec. 3's "#none" ambiguity) -- an empty `night.*` would
    decide exactly the same thing. An explicit rule instead means `validate`
    (`_check_rule_lists`'s `missing_rule_list`/`no_catch_all`) has something
    to see and stays silent, and the decision trace names why nothing moved
    instead of showing the ambiguous "#none" a genuinely unconfigured pair
    would.

    **Day's two rules: shade when the sun hits, otherwise stay open.**
    `sun_hits_target` (`conditions._sun_hits_target`) is target-relative --
    it reads `facade_azimuth` off the blind actually being decided, not off
    the condition body -- so one rule pair generalises across every facing
    the facing step collected, exactly as `MODELS.md` Sec. 3 describes for
    a hand-written house.

    Raises nothing of its own; `Config` and `Blind` validate their own
    fields (`model.py`), and whatever `validate()` finds is this function's
    caller's problem to act on
    (`config_flow.CoverLogicConfigFlow.async_step_blinds_now_summary`), not
    this function's -- see that step's own docstring for why a problem here
    would be this function's bug, not a user-facing error.
    """
    blinds = {
        entity: Blind(entity=entity, facade_azimuth=_COMPASS_TO_AZIMUTH[facings[entity]])
        for entity in entities
    }
    zone = Zone(id=_BLINDS_NOW_ZONE, members=tuple(entities))
    modes = (
        Mode(
            id=_MODE_DAY,
            when={"condition": "state", "entity_id": SUN_ENTITY, "state": "above_horizon"},
        ),
        Mode(id=_MODE_NIGHT, when=None),
    )
    rules = {
        f"{_MODE_DAY}.{RULE_DEFAULT_ZONE}": (
            Rule(
                when={
                    "condition": COND_SUN_HITS_TARGET,
                    # Explicit, not `conditions._sun_hits_target`'s own
                    # `azimuth_entity`/`DEFAULT_AZIMUTH_ENTITY` default
                    # (`sensor.sun_solar_azimuth`): that entity is disabled by
                    # default in stock Home Assistant (`docs/phase-2-
                    # findings.md` §3), so a fresh install's `world.number`
                    # would fall back to the "impossible" sentinel every
                    # time, this condition would never once be true, and the
                    # day mode's shading rule this starter config just
                    # promised would silently never fire. `sun.sun`'s own
                    # `azimuth` attribute is what stock Home Assistant
                    # actually populates.
                    "azimuth_entity": SUN_ENTITY,
                    "azimuth_attribute": "azimuth",
                },
                then=Action(position=_SHADE_POSITION, tilt=_SHADE_TILT),
                name="shade: sun is on this side",
            ),
            Rule(
                when=None,
                then=Action(position=_OPEN_POSITION, tilt=_OPEN_TILT),
                name="otherwise: stay open",
            ),
        ),
        f"{_MODE_NIGHT}.{RULE_DEFAULT_ZONE}": (
            Rule(when=None, then=Action(), name="night: do not move"),
        ),
    }
    return Config(blinds=blinds, zones={zone.id: zone}, modes=modes, rules=rules)
