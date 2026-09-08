import datetime as dt

import jinja2
import pytest

from cover_logic.conditions import evaluate_condition
from cover_logic.model import Blind, Zone
from cover_logic.world import Event, SunTimes, Target, World

NOW = dt.datetime(2026, 8, 19, 13, 0)


def world(states=None, attributes=None, now=NOW, event=None, sun=None) -> World:
    return World(
        states=states or {},
        attributes=attributes or {},
        now=now,
        event=event or Event(),
        sun=sun or SunTimes(),
    )


def target(azimuth: float | None = 90.0, occupants=()) -> Target:
    return Target(
        blind=Blind(entity="cover.a", facade_azimuth=azimuth),
        zone=Zone(id="z", members=("cover.a",), occupants=tuple(occupants)),
    )


def test_none_means_otherwise():
    assert evaluate_condition(None, world()) is True


def test_list_is_and():
    w = world({"a": "on", "b": "off"})
    yes = [
        {"condition": "state", "entity_id": "a", "state": "on"},
        {"condition": "state", "entity_id": "b", "state": "off"},
    ]
    no = [
        {"condition": "state", "entity_id": "a", "state": "on"},
        {"condition": "state", "entity_id": "b", "state": "on"},
    ]
    assert evaluate_condition(yes, w) is True
    assert evaluate_condition(no, w) is False


def test_state_accepts_a_list_of_acceptable_states():
    w = world({"weather.x": "cloudy"})
    cond = {"condition": "state", "entity_id": "weather.x", "state": ["cloudy", "rainy"]}
    assert evaluate_condition(cond, w) is True


def test_state_of_a_missing_entity_is_false_not_an_error():
    cond = {"condition": "state", "entity_id": "nope", "state": "on"}
    assert evaluate_condition(cond, world()) is False


def test_numeric_state_uses_default_when_the_sensor_is_dead():
    # Mirrors `| float(999)` — a missing wind sensor must read as a gale,
    # never as calm.
    w = world({"sensor.wind": "unavailable"})
    cond = {"condition": "numeric_state", "entity_id": "sensor.wind", "below": 40, "default": 999}
    assert evaluate_condition(cond, w) is False


def test_numeric_state_reads_an_attribute():
    w = world({"weather.f": "sunny"}, {("weather.f", "wind_speed"): 21.0})
    cond = {
        "condition": "numeric_state",
        "entity_id": "weather.f",
        "attribute": "wind_speed",
        "below": 30,
        "default": 999,
    }
    assert evaluate_condition(cond, w) is True


def test_numeric_state_without_default_raises():
    # `default` is parity-critical (mirrors `| float(999)`): a config that
    # omits it must fail loudly rather than silently pick a side.
    w = world({"sensor.wind": "unavailable"})
    cond = {"condition": "numeric_state", "entity_id": "sensor.wind", "below": 40}
    with pytest.raises(KeyError):
        evaluate_condition(cond, w)


def test_time_after_is_inclusive():
    cond = {"condition": "time", "after": "13:00"}
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 13, 0))) is True
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 12, 59))) is False


def test_time_before_alone_is_exclusive():
    cond = {"condition": "time", "before": "06:00"}
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 5, 59))) is True
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 6, 0))) is False


def test_time_after_and_before_same_day_window():
    cond = {"condition": "time", "after": "08:00", "before": "18:00"}
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 12, 0))) is True
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 7, 59))) is False
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 18, 0))) is False


def test_time_window_crossing_midnight():
    # after > before means the window wraps past midnight, e.g. 22:00-06:00
    # covers the overnight hours rather than nothing at all.
    cond = {"condition": "time", "after": "22:00", "before": "06:00"}
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 21, 59))) is False
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 23, 0))) is True
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 3, 0))) is True


def test_time_after_honours_seconds():
    cond = {"condition": "time", "after": "13:00:30"}
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 13, 0, 29))) is False
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 13, 0, 30))) is True


def test_time_window_straddling_midnight_honours_seconds():
    # Reviewer's example: truncating "23:59:30"/"00:00:30" to HH:MM collapses
    # the window to 23:59-00:00, and the wrap-around branch's second disjunct
    # (`now < before`) is then never true for any real clock reading, so the
    # intended in-window minute around midnight evaluates False. Seconds must
    # be honoured for the window to mean what it says.
    cond = {"condition": "time", "after": "23:59:30", "before": "00:00:30"}
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 23, 59, 0))) is False
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 19, 23, 59, 45))) is True
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 20, 0, 0, 15))) is True
    assert evaluate_condition(cond, world(now=dt.datetime(2026, 8, 20, 0, 0, 45))) is False


def test_and_or_not():
    w = world({"a": "on"})
    on = {"condition": "state", "entity_id": "a", "state": "on"}
    off = {"condition": "state", "entity_id": "a", "state": "off"}
    assert evaluate_condition({"condition": "or", "conditions": [on, off]}, w) is True
    assert evaluate_condition({"condition": "and", "conditions": [on, off]}, w) is False
    assert evaluate_condition({"condition": "not", "conditions": [off]}, w) is True


def test_not_with_several_subconditions_is_de_morgan_not_first_match():
    # `not` over a list is NOT(A or B or C): true only when NONE of the
    # sub-conditions hold. This matches Home Assistant's own `not` semantics
    # and is deliberate, not a bug — pin it so it can't regress silently.
    w = world({"a": "on", "b": "off", "c": "off"})
    a_on = {"condition": "state", "entity_id": "a", "state": "on"}
    b_on = {"condition": "state", "entity_id": "b", "state": "on"}
    c_on = {"condition": "state", "entity_id": "c", "state": "on"}
    # Only "a" is on -> at least one sub-condition holds -> not is False.
    assert evaluate_condition({"condition": "not", "conditions": [a_on, b_on, c_on]}, w) is False
    # None of them are on -> not is True.
    w_none = world({"a": "off", "b": "off", "c": "off"})
    assert (
        evaluate_condition({"condition": "not", "conditions": [a_on, b_on, c_on]}, w_none) is True
    )


def test_ref_resolves_through_the_registry():
    reg = {"vecer": {"condition": "state", "entity_id": "a", "state": "on"}}
    w = world({"a": "on"})
    assert evaluate_condition({"condition": "ref", "name": "vecer"}, w, registry=reg) is True


def test_ref_to_a_missing_name_raises():
    with pytest.raises(KeyError):
        evaluate_condition({"condition": "ref", "name": "nope"}, world(), registry={})


def test_ref_with_registry_none_raises():
    # registry=None is distinct from registry={}: there's nowhere at all to
    # look the name up, so this must raise rather than silently fail closed.
    with pytest.raises(KeyError):
        evaluate_condition({"condition": "ref", "name": "vecer"}, world(), registry=None)


def test_circular_ref_raises_a_clear_error():
    reg = {"a": {"condition": "ref", "name": "a"}}
    with pytest.raises(ValueError, match="circular condition reference"):
        evaluate_condition({"condition": "ref", "name": "a"}, world(), registry=reg)


@pytest.mark.parametrize(
    ("azimuth", "expected"),
    [
        (-1.0, False),
        (44.0, False),
        (45.0, True),  # lower bound is inclusive
        (134.0, True),
        (135.0, False),  # upper bound is EXCLUSIVE — this is the parity trap
        (224.0, False),
        (315.0, False),
    ],
)
def test_sun_hits_target_uses_a_half_open_sector(azimuth, expected):
    w = world({"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": str(azimuth)})
    cond = {"condition": "sun_hits_target"}
    assert evaluate_condition(cond, w, target(azimuth=90.0)) is expected


def test_sun_hits_target_is_false_below_the_horizon():
    w = world({"sun.sun": "below_horizon", "sensor.sun_solar_azimuth": "90"})
    assert evaluate_condition({"condition": "sun_hits_target"}, w, target()) is False


def test_sun_hits_target_is_false_without_a_facade():
    w = world({"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": "90"})
    assert evaluate_condition({"condition": "sun_hits_target"}, w, target(azimuth=None)) is False


def test_sun_sector_wraps_around_north():
    w = world({"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": "350"})
    assert evaluate_condition({"condition": "sun_hits_target"}, w, target(azimuth=0.0)) is True


def test_sun_hits_target_is_false_for_a_north_facade_when_the_azimuth_sensor_is_missing():
    """A missing/unavailable azimuth reading falls back to `world.number`'s
    -1.0 "impossible" sentinel. Routed straight through the sector's modular
    arithmetic, that sentinel wraps right back INTO a north-facing sector --
    wrongly firing the sun rule during a sensor outage for exactly the facade
    orientation a disabled-by-default `sensor.sun_solar_azimuth` (issue #5)
    makes a realistic default-install condition, not an exotic one.
    """
    w = world({"sun.sun": "above_horizon"})  # sensor.sun_solar_azimuth not in the snapshot at all
    cond = {"condition": "sun_hits_target"}
    assert evaluate_condition(cond, w, target(azimuth=0.0)) is False
    assert evaluate_condition(cond, w, target(azimuth=350.0)) is False
    assert evaluate_condition(cond, w, target(azimuth=10.0)) is False


def test_sun_hits_target_reads_the_azimuth_from_an_attribute():
    """In stock Home Assistant the azimuth lives on `sun.sun` as an
    attribute -- `sensor.sun_solar_azimuth` is disabled by default, so a new
    installation has no working path without `azimuth_attribute`.
    """
    w = world(
        states={"sun.sun": "above_horizon"},
        attributes={("sun.sun", "azimuth"): 90.0},
    )
    cond = {
        "condition": "sun_hits_target",
        "azimuth_entity": "sun.sun",
        "azimuth_attribute": "azimuth",
    }
    assert evaluate_condition(cond, w, target(azimuth=90.0)) is True


def test_sun_hits_target_attribute_path_respects_the_sector_too():
    w = world(
        states={"sun.sun": "above_horizon"},
        attributes={("sun.sun", "azimuth"): 200.0},
    )
    cond = {
        "condition": "sun_hits_target",
        "azimuth_entity": "sun.sun",
        "azimuth_attribute": "azimuth",
    }
    assert evaluate_condition(cond, w, target(azimuth=90.0)) is False


def test_sun_hits_target_state_based_path_still_works_when_azimuth_attribute_is_absent():
    """The existing plain-state azimuth path (a separate `azimuth_entity`
    with no `attribute:`) must be unchanged by adding attribute support.
    """
    w = world({"sun.sun": "above_horizon", "sensor.sun_solar_azimuth": "90"})
    cond = {"condition": "sun_hits_target"}
    assert evaluate_condition(cond, w, target(azimuth=90.0)) is True


def test_event_targets_zone():
    w = world(event=Event(kind="arrival", person="peter"))
    assert (
        evaluate_condition({"condition": "event_targets_zone"}, w, target(occupants=["peter"]))
        is True
    )
    assert (
        evaluate_condition({"condition": "event_targets_zone"}, w, target(occupants=["mimka"]))
        is False
    )


# ---------------------------------------------------------------------------
# `manual_move`: relative to the target, exactly as `sun_hits_target` and
# `event_targets_zone` are, so the same rule works in every room.
# ---------------------------------------------------------------------------


def _moved(entity="cover.a", direction="opening"):
    return world(event=Event(kind="manual_move", blind=entity, direction=direction))


def test_manual_move_matches_this_blind_and_direction():
    cond = {"condition": "manual_move", "direction": "opening"}
    assert evaluate_condition(cond, _moved(), target()) is True


def test_manual_move_ignores_the_other_direction():
    cond = {"condition": "manual_move", "direction": "closing"}
    assert evaluate_condition(cond, _moved(direction="opening"), target()) is False


def test_manual_move_ignores_a_move_on_another_blind():
    """The point of being relative to the target: one room must not react to another."""
    cond = {"condition": "manual_move"}
    assert evaluate_condition(cond, _moved(entity="cover.elsewhere"), target()) is False


def test_manual_move_without_a_direction_means_moved_at_all():
    assert evaluate_condition({"condition": "manual_move"}, _moved(direction="closing"), target())
    assert evaluate_condition({"condition": "manual_move"}, _moved(direction="opening"), target())


def test_manual_move_accepts_any_as_an_explicit_spelling_of_that():
    cond = {"condition": "manual_move", "direction": "any"}
    assert evaluate_condition(cond, _moved(direction="closing"), target()) is True


def test_manual_move_is_false_for_an_ordinary_state_change():
    """The default event carries no blind, so nothing must match it."""
    assert evaluate_condition({"condition": "manual_move"}, world(), target()) is False


def test_manual_move_checks_the_kind_not_only_the_blind():
    """An arrival that happened to name a blind is still not a manual move.

    Written because matching on `blind` alone would look correct today and
    quietly become wrong the moment another event kind grows that field.
    """
    w = world(event=Event(kind="arrival", person="peter", blind="cover.a", direction="opening"))
    assert evaluate_condition({"condition": "manual_move"}, w, target()) is False


def test_manual_move_needs_a_target():
    """No target means nothing to be relative to -- false, never a crash."""
    assert evaluate_condition({"condition": "manual_move"}, _moved(), None) is False


def test_template_condition_sees_the_same_globals_as_home_assistant():
    w = world({"input_boolean.x": "on"})
    cond = {"condition": "template", "value_template": "{{ is_state('input_boolean.x', 'on') }}"}
    assert evaluate_condition(cond, w) is True


def test_unknown_condition_type_raises():
    with pytest.raises(ValueError, match="unknown condition"):
        evaluate_condition({"condition": "wibble"}, world())


def test_template_with_undefined_variable_propagates_instead_of_swallowing():
    # A broken user template must not silently evaluate to False -- "false"
    # can mean "leave the house open during a heatwave". Do not wrap this in
    # try/except: a future change that swallows the error should make this
    # test fail.
    cond = {"condition": "template", "value_template": "{{ this_is_undefined }}"}
    with pytest.raises(jinja2.UndefinedError):
        evaluate_condition(cond, world())


def test_state_can_compare_an_attribute():
    w = World(
        states={"alarm_control_panel.alarmo": "triggered"},
        attributes={("alarm_control_panel.alarmo", "arm_mode"): "armed_vacation"},
        now=NOW,
        event=Event(),
    )
    cond = {
        "condition": "state",
        "entity_id": "alarm_control_panel.alarmo",
        "attribute": "arm_mode",
        "state": "armed_vacation",
    }
    assert evaluate_condition(cond, w) is True

    cond_no = {**cond, "state": "armed_away"}
    assert evaluate_condition(cond_no, w) is False


# --- condition: sun --------------------------------------------------------

# 19 Aug 2026 in this house: sunrise 05:44, sunset 19:51. Stated rather than
# computed, so a test never depends on the astronomy it is checking against.
SUN = SunTimes(sunrise=dt.datetime(2026, 8, 19, 5, 44), sunset=dt.datetime(2026, 8, 19, 19, 51))


def sun_world(hour: int, minute: int = 0, sun: SunTimes | None = None) -> World:
    return world(now=dt.datetime(2026, 8, 19, hour, minute), sun=SUN if sun is None else sun)


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (19, 30, False),  # 21 min before sunset -- one minute short
        (19, 31, True),  # exactly sunset - 20 min
        (19, 32, True),
        (23, 0, True),
    ],
)
def test_sun_after_sunset_with_negative_offset(hour, minute, expected):
    """The measured `lighting_on` rule: dusk begins 20 minutes before sunset.

    Offsets are seconds, not HA's `"-00:20:00"` string -- see
    docs/rationale.md.
    """
    cond = {"condition": "sun", "after": "sunset", "after_offset": -1200}
    assert evaluate_condition(cond, sun_world(hour, minute)) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(5, 43, True), (5, 44, True), (5, 45, False), (12, 0, False)],
)
def test_sun_before_sunrise_includes_the_boundary_minute(hour, minute, expected):
    """`before: sunrise` is true *at* sunrise -- HA compares with `>`, not `>=`.

    Deliberately asymmetric with `condition: time`, where `before` excludes
    its boundary (`now < before`). Matching HA matters more than matching our
    own other condition: these bodies are copied out of HA's UI. Covered here
    because the difference is one character and invisible in review.
    """
    cond = {"condition": "sun", "before": "sunrise"}
    assert evaluate_condition(cond, sun_world(hour, minute)) is expected


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(2, True), (6, False), (13, False), (20, True), (23, True)],
)
def test_sun_before_sunrise_after_sunset_is_an_or_not_an_and(hour, expected):
    """The one place this condition is not the AND of its two bounds.

    `before: sunrise` with `after: sunset` names the dark window around
    midnight. Read as an AND it would be empty for every day of the year, so
    HA evaluates it as an OR and so does this engine. A test that only checked
    daylight would pass either way, which is why 02:00 and 23:00 are both here.
    """
    cond = {"condition": "sun", "before": "sunrise", "after": "sunset"}
    assert evaluate_condition(cond, sun_world(hour)) is expected


def test_sun_window_between_sunrise_and_sunset_is_an_and():
    """`after: sunrise` with `before: sunset` is the ordinary daylight AND."""
    cond = {"condition": "sun", "after": "sunrise", "before": "sunset"}
    assert evaluate_condition(cond, sun_world(13)) is True
    assert evaluate_condition(cond, sun_world(3)) is False
    assert evaluate_condition(cond, sun_world(21)) is False


@pytest.mark.parametrize("missing", ["sunrise", "sunset"])
def test_sun_is_not_satisfied_when_that_event_does_not_happen_today(missing):
    """A polar day has no sunrise; answer False rather than raising.

    Only the event actually named is allowed to veto: a missing sunrise must
    not silently decide a condition that asks about sunset, or a polar summer
    would answer every sun condition the same way.
    """
    polar = SunTimes(
        sunrise=None if missing == "sunrise" else SUN.sunrise,
        sunset=None if missing == "sunset" else SUN.sunset,
    )
    asked_about = {"condition": "sun", "after": missing}
    assert evaluate_condition(asked_about, sun_world(13, sun=polar)) is False

    # 21:00 is past both events, so whichever one survives answers True --
    # the point being that the missing one did not get to answer at all.
    other = "sunset" if missing == "sunrise" else "sunrise"
    unrelated = {"condition": "sun", "after": other}
    assert evaluate_condition(unrelated, sun_world(21, sun=polar)) is True


def test_sun_offset_zero_is_the_default():
    cond = {"condition": "sun", "after": "sunset"}
    assert evaluate_condition(cond, sun_world(19, 50)) is False
    assert evaluate_condition(cond, sun_world(19, 51)) is True


# --- condition: state with `for:` ------------------------------------------


def held(entity, seconds, now=NOW):
    """A `since` map putting `entity`'s last change `seconds` ago."""
    return {entity: now - dt.timedelta(seconds=seconds)}


@pytest.mark.parametrize(
    ("held_seconds", "expected"),
    [(0, False), (119, False), (120, True), (600, True)],
)
def test_state_for_requires_the_state_to_have_lasted(held_seconds, expected):
    """The debounce a bed sensor needs: thirty seconds of flicker must not count."""
    cond = {
        "condition": "state",
        "entity_id": "binary_sensor.bed",
        "state": "off",
        "for": 120,
    }
    w = World(
        states={"binary_sensor.bed": "off"},
        now=NOW,
        event=Event(),
        since=held("binary_sensor.bed", held_seconds),
    )
    assert evaluate_condition(cond, w) is expected


def test_state_for_is_not_consulted_when_the_state_does_not_match():
    """A wrong state is false regardless of how long it has been wrong."""
    cond = {"condition": "state", "entity_id": "binary_sensor.bed", "state": "off", "for": 120}
    w = World(
        states={"binary_sensor.bed": "on"},
        now=NOW,
        event=Event(),
        since=held("binary_sensor.bed", 9999),
    )
    assert evaluate_condition(cond, w) is False


def test_state_without_for_ignores_since_entirely():
    """Every pre-existing condition keeps its exact meaning."""
    cond = {"condition": "state", "entity_id": "binary_sensor.bed", "state": "off"}
    w = World(
        states={"binary_sensor.bed": "off"},
        now=NOW,
        event=Event(),
        since=held("binary_sensor.bed", 0),
    )
    assert evaluate_condition(cond, w) is True


def test_missing_since_ignores_for_rather_than_failing():
    """Deliberate: no timing information must not change what the condition means.

    `ha_world` fills `since` for every entity it snapshots, so this is the
    shape a hand-built test world has, never one a live house produces. Pinned
    because the alternative -- answering False -- would silently disable every
    rule behind a `for:` if that fill ever regressed.
    """
    cond = {"condition": "state", "entity_id": "binary_sensor.bed", "state": "off", "for": 120}
    w = World(states={"binary_sensor.bed": "off"}, now=NOW, event=Event())
    assert evaluate_condition(cond, w) is True
