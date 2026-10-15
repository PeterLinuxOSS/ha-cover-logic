"""The solar geometry behind a computed slat angle, and every way it has no answer."""

import pytest

from cover_logic.geometry import gamma, slat_angle_percent

INF = float("inf")
NAN = float("nan")


def test_gamma_is_zero_when_the_sun_is_dead_on_the_facade():
    assert gamma(180.0, 180.0) == pytest.approx(0.0)


def test_gamma_is_signed_and_wraps_the_short_way_around_north():
    # 10 degrees east of a north-facing facade, not 350 degrees west of it.
    assert gamma(10.0, 0.0) == pytest.approx(10.0)
    assert gamma(350.0, 0.0) == pytest.approx(-10.0)


def test_it_clamps_a_past_closed_geometry_to_the_axis_maximum():
    # 45 deg elevation, sun on the normal, 60/80 mm slats: the formula gives a
    # slat angle of ~103 deg, which is past fully closed on a 90 deg scale.
    assert slat_angle_percent(45.0, 0.0, 60.0, 80.0) == 100


def test_a_low_sun_needs_less_closing_than_a_high_one():
    low = slat_angle_percent(15.0, 0.0, 60.0, 80.0)
    high = slat_angle_percent(60.0, 0.0, 60.0, 80.0)
    assert low is not None
    assert high is not None
    assert low < high


def test_the_full_scale_halves_the_percentage_of_the_half_scale():
    # Holds only while the half scale is unclamped; once it clamps to 100 the two diverge.
    half = slat_angle_percent(20.0, 0.0, 60.0, 80.0, scale="half")
    full = slat_angle_percent(20.0, 0.0, 60.0, 80.0, scale="full")
    assert half is not None
    assert full is not None
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


@pytest.mark.parametrize(
    ("elevation", "gamma_deg", "why"),
    [
        (INF, 0.0, "infinite elevation"),
        (-INF, 0.0, "negative infinite elevation"),
        (NAN, 0.0, "elevation reported as NaN"),
        (30.0, NAN, "gamma computed from a NaN azimuth"),
    ],
)
def test_a_non_finite_reading_has_no_answer_rather_than_raising(elevation, gamma_deg, why):
    # A broken sensor is an undefined case like any other, not a crash in the
    # decision layer; `engine._resolve_slat_angle` screens the azimuth but its
    # `elevation < _ELEVATION_MIN` guard is False for both inf and NaN.
    assert slat_angle_percent(elevation, gamma_deg, 60.0, 80.0) is None, why


def test_the_result_never_leaves_the_axis_range():
    for elevation in range(1, 90):
        result = slat_angle_percent(float(elevation), 0.0, 60.0, 80.0)
        assert result is None or 0 <= result <= 100
