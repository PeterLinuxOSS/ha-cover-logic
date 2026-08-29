"""Proves `blinds_now`'s starter configuration actually decides something.

`_build_starter_config` (`config_flow.py`) lives in a module that imports
`homeassistant` unconditionally at module level (see that module's own
docstring for why), so this needs `pytest.importorskip("homeassistant")`
purely to be able to import the function at all -- nothing asserted below
needs a running Home Assistant, a real config flow, or any of
`tests/ha/conftest.py`'s fixtures. `Config`/`World`/`evaluate` are all pure
(enforced by `tests/test_purity.py`), and everything here does is call those,
directly, against the in-memory `Config` `_build_starter_config` returns --
which is why this file lives next to the rest of the pure-core tests rather
than in `tests/ha/`.

Why `tests/ha/test_config_flow.py`'s existing
`test_blinds_now_summary_creates_a_configuration_that_decides_something`
did not already catch the bug this file is built to catch: that test only
runs the generated configuration through `validate()`, which checks
*shape*, not "can this ever evaluate true" -- a `sun_hits_target` condition
missing `azimuth_entity`/`azimuth_attribute` is a perfectly valid shape
(`validation._REQUIRED_CONDITION_KEYS[COND_SUN_HITS_TARGET] == ()`), so it
passed identically whether the day-mode shading rule could ever fire or not.
Only running the config through the real engine, against a `World` that
actually has an azimuth to read, tells the two cases apart -- see
`docs/phase-2-findings.md` §3, which is exactly the bug this class of
config once already had (`sun_hits_target` reading the azimuth from
`sensor.sun_solar_azimuth`, disabled by default in stock Home Assistant,
instead of `sun.sun`'s own `azimuth` attribute).
"""

import pytest

pytest.importorskip("homeassistant")

from cover_logic.config_flow import (  # noqa: E402
    _OPEN_POSITION,
    _OPEN_TILT,
    _SHADE_POSITION,
    _SHADE_TILT,
    _build_starter_config,
)
from cover_logic.engine import evaluate  # noqa: E402
from cover_logic.world import World  # noqa: E402


def test_the_starter_config_actually_shades_a_blind_the_sun_is_on():
    """South-facing blind, sun due south (azimuth 180) and above the
    horizon: squarely inside the facade's default 45-degree tolerance, so the
    day mode's shading rule must fire, not the "stay open" fallback.
    """
    config = _build_starter_config(["cover.a"], {"cover.a": "south"})
    world = World(
        states={"sun.sun": "above_horizon"},
        attributes={("sun.sun", "azimuth"): 180.0},
    )

    decision = evaluate(config, world)

    assert decision.targets["cover.a"].position == _SHADE_POSITION
    assert decision.targets["cover.a"].tilt == _SHADE_TILT


def test_the_starter_config_stays_open_when_the_sun_is_on_the_far_side():
    """Same south-facing blind, sun due north (azimuth 0): outside the
    facade's sector on the opposite side of the house, so the day mode's
    fallback ("stay open") must fire instead.
    """
    config = _build_starter_config(["cover.a"], {"cover.a": "south"})
    world = World(
        states={"sun.sun": "above_horizon"},
        attributes={("sun.sun", "azimuth"): 0.0},
    )

    decision = evaluate(config, world)

    assert decision.targets["cover.a"].position == _OPEN_POSITION
    assert decision.targets["cover.a"].tilt == _OPEN_TILT
