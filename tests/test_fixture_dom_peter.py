"""The fixture must be structurally sound before parity is even attempted."""

import datetime as dt

import pytest

from cover_logic.config_schema import load_config_file
from cover_logic.const import RULE_DEFAULT_ZONE
from cover_logic.engine import evaluate
from cover_logic.model import KEEP
from cover_logic.validation import validate
from cover_logic.world import Event, SunTimes, World

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
    """The fixture's own comment claims zero problems of *any* severity, not
    just zero errors -- filtering to `ERROR` before asserting would let a
    warning (e.g. an `unreachable_rule` or `no_catch_all`) creep in on the
    live house's own configuration unnoticed. Assert the whole list.
    """
    assert validate(config) == []


def test_fixture_covers_all_ten_blinds(config):
    assert len(config.blinds) == 10
    assert len(config.zones) == 7


def test_every_mode_zone_pair_has_rules(config):
    """Every (mode, zone) must be decided -- either by its own rule list or by
    the mode's inherited default (fixture's `noc` collapsed its 7 identical
    zone lists into a single `noc.*` default, phase 6 task 3), never by
    neither.
    """
    for mode in config.modes:
        default_key = f"{mode.id}.{RULE_DEFAULT_ZONE}"
        has_default = default_key in config.rules
        for zone_id in config.zones:
            own_key = f"{mode.id}.{zone_id}"
            assert own_key in config.rules or has_default, own_key


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


# One ordinary day for this house: sunrise 05:50, sunset 19:26. Stated, never
# computed, so these tests never depend on the astronomy they check against.
DAY = dt.date(2026, 9, 1)
SKY = SunTimes(sunrise=dt.datetime(2026, 9, 1, 5, 50), sunset=dt.datetime(2026, 9, 1, 19, 26))


def _at(hour, minute=0, **overrides):
    return World(
        states={**CALM, **overrides},
        attributes={},
        now=dt.datetime(2026, 9, 1, hour, minute),
        event=Event(),
        sun=SKY,
    )


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (13, 0, "bezny_den"),  # midday
        (19, 25, "bezny_den"),  # a minute before sunset
        (19, 27, "noc"),  # a minute after it
        (2, 0, "noc"),  # middle of the night
        (5, 28, "noc"),  # sunrise-22min: still night
        (5, 30, "bezny_den"),  # sunrise-20min: night is over
    ],
)
def test_night_is_derived_from_the_sky_not_from_the_helper(config, hour, minute, expected):
    """`cover_down` is `off` throughout `CALM`, so only the sky can say `noc`.

    The morning boundary is 21 minutes before sunrise, and that offset is
    measured rather than chosen: the helper this replaces went off a median of
    20.9 min ahead of sunrise (range 11.3-24.9, 15 dawns, measured against
    Home Assistant's own `sun.sun`). The two rows either side of it are the
    only reason a change to that offset cannot pass silently -- so if this
    fails, re-read `docs/rationale.md` before editing the numbers.
    """
    assert evaluate(config, _at(hour, minute)).mode == expected


def test_the_manual_brake_still_forces_night_in_broad_daylight(config):
    """The half of `cover_down` that stays a helper: a command, not a state.

    Pinned separately from the derived half because the whole point of
    splitting them is that the person can always win -- if this ever starts
    depending on the sky, the user has lost their emergency brake.
    """
    assert evaluate(config, _at(13, 0)).mode == "bezny_den"
    assert evaluate(config, _at(13, 0, **{"input_boolean.cover_down": "on"})).mode == "noc"


def test_night_leaves_every_blind_alone(config):
    """`noc` means nothing moves -- the brake would be useless otherwise."""
    decision = evaluate(config, _at(2, 0))
    assert all(
        action.position is KEEP and action.tilt is KEEP for action in decision.targets.values()
    )
