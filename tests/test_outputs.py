"""`derive_outputs` maps output helpers to their desired on/off from named conditions."""

import datetime as dt

from cover_logic.config_schema import load_config_file
from cover_logic.outputs import derive_outputs
from cover_logic.world import SunTimes, World

# The two helpers the house maps its `je_tma`/`je_noc_auto` conditions to; the
# mapping lives in the entry's options now, not in shipped code.
_OUTPUT_HELPERS = {
    "je_tma": "input_boolean.lighting_on",
    "je_noc_auto": "input_boolean.je_noc_auto",
}


def _world(hodina, lux, elevacia):
    den = dt.date(2026, 9, 3)
    return World(
        states={
            "sensor.predsien_dvere_senzor_illuminance": str(lux),
            "input_boolean.cover_down": "off",
        },
        attributes={("sun.sun", "elevation"): elevacia},
        now=dt.datetime.combine(den, dt.time(hodina, 0)),
        sun=SunTimes(
            sunrise=dt.datetime.combine(den, dt.time(5, 53, 18)),
            sunset=dt.datetime.combine(den, dt.time(19, 22, 15)),
        ),
    )


def test_bright_midday_all_outputs_off(fixtures_dir):
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    out = derive_outputs(cfg, _world(13, 3000, 45.0), _OUTPUT_HELPERS)
    assert out["input_boolean.lighting_on"] is False
    assert out["input_boolean.je_noc_auto"] is False


def test_night_sets_both(fixtures_dir):
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    out = derive_outputs(cfg, _world(21, 0, -10.0), _OUTPUT_HELPERS)
    assert out["input_boolean.lighting_on"] is True
    assert out["input_boolean.je_noc_auto"] is True


def test_empty_mapping_derives_nothing(fixtures_dir):
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    assert derive_outputs(cfg, _world(21, 0, -10.0), {}) == {}


def test_unknown_condition_is_skipped(fixtures_dir):
    cfg = load_config_file(fixtures_dir / "dom_peter.yaml")
    out = derive_outputs(cfg, _world(21, 0, -10.0), {"not_a_condition": "input_boolean.x"})
    assert out == {}
