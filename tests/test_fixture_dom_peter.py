"""The fixture must be structurally sound before parity is even attempted."""

import datetime as dt

import pytest

from cover_logic.config_schema import load_config_file
from cover_logic.engine import evaluate
from cover_logic.validation import ERROR, validate
from cover_logic.world import Event, World

NOW = dt.datetime(2026, 8, 19, 13, 0)

CALM = {
    "input_boolean.cover_down": "off",
    "alarm_control_panel.alarmo": "disarmed",
    "input_boolean.teplotna_ochrana_dom": "off",
    "input_boolean.lighting_on": "off",
    "input_boolean.kvety_on": "on",
    "input_boolean.zaluzie_kuchyna_rucne": "off",
    "binary_sensor.is_home": "on",
    "input_boolean.some_sleeping": "off",
    "binary_sensor.peter_home": "on",
    "binary_sensor.mimka_home": "on",
    "binary_sensor.pavel_home": "on",
    "binary_sensor.majka_home": "off",
    "input_boolean.zaluzie_aktivna_peter": "on",
    "input_boolean.zaluzie_aktivna_mimka": "on",
    "input_boolean.zaluzie_aktivna_spalna": "on",
    "sun.sun": "above_horizon",
    "sensor.sun_solar_azimuth": "180",
    "weather.openweathermap": "sunny",
    "input_number.kvety_pozicia_zaluzie": "34",
}


@pytest.fixture(scope="module")
def config(fixtures_dir):
    return load_config_file(fixtures_dir / "dom_peter.yaml")


def test_fixture_has_no_validation_errors(config):
    errors = [p for p in validate(config) if p.severity == ERROR]
    assert errors == [], errors


def test_fixture_covers_all_ten_blinds(config):
    assert len(config.blinds) == 10
    assert len(config.zones) == 7


def test_every_mode_zone_pair_has_rules(config):
    for mode in config.modes:
        for zone_id in config.zones:
            assert f"{mode.id}.{zone_id}" in config.rules, f"{mode.id}.{zone_id}"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"input_boolean.cover_down": "on"}, "noc"),
        ({"alarm_control_panel.alarmo": "armed_vacation"}, "dovolenka"),
        ({"alarm_control_panel.alarmo": "triggered"}, "bezny_den"),
        ({"input_boolean.teplotna_ochrana_dom": "on"}, "horucava"),
        ({}, "bezny_den"),
    ],
)
def test_mode_resolution_matches_the_old_priority_order(config, overrides, expected):
    world = World(states={**CALM, **overrides}, attributes={}, now=NOW, event=Event())
    assert evaluate(config, world).mode == expected


def test_triggered_during_vacation_still_resolves_to_dovolenka(config):
    # The false-tamper case from 2026-08-13: the panel reads `triggered`,
    # the mode lives in the attribute.
    world = World(
        states={**CALM, "alarm_control_panel.alarmo": "triggered"},
        attributes={("alarm_control_panel.alarmo", "arm_mode"): "armed_vacation"},
        now=NOW,
        event=Event(),
    )
    assert evaluate(config, world).mode == "dovolenka"


def test_every_blind_gets_an_action_in_every_mode(config):
    for overrides in (
        {"input_boolean.cover_down": "on"},
        {"alarm_control_panel.alarmo": "armed_vacation"},
        {"input_boolean.teplotna_ochrana_dom": "on"},
        {},
    ):
        world = World(states={**CALM, **overrides}, attributes={}, now=NOW, event=Event())
        decision = evaluate(config, world)
        assert set(decision.targets) == set(config.blinds)
