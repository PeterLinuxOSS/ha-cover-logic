"""THE migration gate.

The new engine must produce, for every one of the 92 160 scenarios, exactly
what the live Jinja matrix produces — entity by entity, on both the state and
the arrival variant. Nothing gets switched over in the house until this is green.
"""

from __future__ import annotations

import pytest

from cover_logic.config_schema import load_config_file
from cover_logic.engine import evaluate
from cover_logic.world import Event

from . import jinja_bridge as bridge
from .mapping import expected_actions, world_from_stav

pytestmark = pytest.mark.skipif(not bridge.available(), reason="needs /config/tests/matica.py")

MAX_REPORTED = 5


@pytest.fixture(scope="module")
def config(fixtures_dir):
    return load_config_file(fixtures_dir / "dom_peter.yaml")


def scenarios():
    # Deliberately local: `test_zaluzie_matica` lives in /config/tests, not in
    # this repo, and only exists on the Home Assistant host. A module-level
    # import would raise ImportError at collection time on every other
    # machine, before `pytestmark`'s `skipif` ever gets a chance to apply.
    from test_zaluzie_matica import SCENARE  # noqa: PLC0415

    return SCENARE


def describe(stav) -> str:
    return (
        f"rezim={stav.rezim} vecer={stav.vecer} cover_down={stav.cover_down} "
        f"alarmo={stav.alarmo}/{stav.alarmo_arm_mode} "
        f"teplo={stav.teplotna_ochrana} light={stav.lighting_on} "
        f"po1230={stav.po_1230} kvety={stav.kvety_on} rucne={stav.rucne_kuchyna} "
        f"sun={stav.sun_above} az={stav.azimut} poc={stav.weather} "
        f"vietor={stav.vietor_rychlost} spia={stav.some_sleeping} "
        f"doma={stav.peter_home}/{stav.mimka_home}/{stav.pavel_home}/{stav.majka_home} "
        f"akt={stav.akt_peter}/{stav.akt_mimka}/{stav.akt_spalna}"
    )


def compare(config, scenarios_subset) -> list[str]:
    problems: list[str] = []
    for stav in scenarios_subset:
        old = bridge.ciele(stav)
        for variant, event in (("state", Event()), ("arrival", Event(kind="arrival"))):
            decision = evaluate(config, world_from_stav(stav, event))
            if decision.mode != stav.rezim:
                problems.append(
                    f"\n  {describe(stav)}\n    mode: new={decision.mode} old={stav.rezim}"
                )
            for entity, item in old.items():
                want = expected_actions(
                    item, variant=variant, teplotna_ochrana=stav.teplotna_ochrana
                )
                got = decision.targets[entity]
                if got != want:
                    problems.append(
                        f"\n  {describe(stav)}"
                        f"\n    {entity} [{variant}] new={got} old={want}"
                        f"\n    rule={decision.trace[entity]} raw={item}"
                    )
            if len(problems) >= MAX_REPORTED:
                return problems
    return problems


def test_mode_matches_on_the_whole_space(config):
    bad = [s for s in scenarios() if evaluate(config, world_from_stav(s)).mode != s.rezim]
    assert not bad, f"{len(bad)} scenarios resolved to the wrong mode"


def test_parity_on_a_sample(config):
    """Fast feedback while iterating — every 97th scenario."""
    problems = compare(config, scenarios()[::97])
    assert not problems, "".join(problems)


@pytest.mark.slow
def test_parity_on_the_whole_space(config):
    """THE GATE. All 92 160 scenarios, both variants, every entity."""
    all_scenarios = scenarios()
    assert len(all_scenarios) == 92_160, len(all_scenarios)
    problems = compare(config, all_scenarios)
    assert not problems, "".join(problems)
