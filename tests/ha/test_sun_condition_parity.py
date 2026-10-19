"""`conditions._sun` must answer exactly what Home Assistant's own would.

The pure engine cannot call `homeassistant.components.sun.condition.sun` --
that needs a `hass` for the astral maths, which is the whole reason the sun
times are resolved in `ha_world` and handed over as plain datetimes instead.
That port is a re-reading of someone else's code, so it is checked
differentially rather than by re-stating my reading of it: both are run over
the same grid of times and offsets and every answer has to match.

This caught nothing on the day it was written, which is the point -- it is
here so the next person to touch `_sun` finds out immediately, and so the
boundary quirks (`before` includes its edge; `before: sunrise` with
`after: sunset` is an OR) cannot be "tidied up" into a difference.
"""

import datetime as dt
from unittest import mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.sun import condition as ha_sun
from homeassistant.const import SUN_EVENT_SUNRISE
from homeassistant.util import dt as dt_util

from cover_logic.conditions import evaluate_condition
from cover_logic.world import Event, SunTimes, World

# One ordinary mid-latitude day, stated rather than computed.
DAY = dt.date(2026, 8, 19)
SUNRISE_LOCAL = dt.datetime(2026, 8, 19, 5, 44)
SUNSET_LOCAL = dt.datetime(2026, 8, 19, 19, 51)

# Every minute would be 1440 cases per shape; these are the ones where a
# boundary bug can hide, plus a few plainly-inside points.
MINUTES = [
    (0, 0),
    (5, 43),
    (5, 44),
    (5, 45),
    (6, 4),
    (13, 0),
    (19, 30),
    (19, 31),
    (19, 50),
    (19, 51),
    (19, 52),
    (23, 59),
]

SHAPES = [
    {"after": "sunset"},
    {"after": "sunset", "after_offset": -1200},
    {"after": "sunrise"},
    {"after": "sunrise", "after_offset": 600},
    {"before": "sunrise"},
    {"before": "sunset"},
    {"before": "sunset", "before_offset": -1800},
    {"after": "sunrise", "before": "sunset"},
    {"before": "sunrise", "after": "sunset"},
    {"before": "sunrise", "after": "sunset", "after_offset": -1200},
]

TZ = dt.timezone(dt.timedelta(hours=2))


def _aware(naive: dt.datetime) -> dt.datetime:
    return naive.replace(tzinfo=TZ)


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: ",".join(f"{k}={v}" for k, v in s.items()))
@pytest.mark.parametrize(("hour", "minute"), MINUTES)
def test_pure_sun_condition_matches_home_assistant(shape, hour, minute):
    """Same instant, same sun, same shape -- the two implementations must agree."""
    now_local = dt.datetime(2026, 8, 19, hour, minute)
    now_utc = _aware(now_local).astimezone(dt.UTC)

    def fake_astral(_hass, event, _date=None):
        moment = SUNRISE_LOCAL if event == SUN_EVENT_SUNRISE else SUNSET_LOCAL
        return _aware(moment).astimezone(dt.UTC)

    kwargs = {
        "before": shape.get("before"),
        "after": shape.get("after"),
    }
    for key in ("before_offset", "after_offset"):
        if key in shape:
            kwargs[key] = dt.timedelta(seconds=shape[key])

    with (
        mock.patch.object(ha_sun.dt_util, "utcnow", return_value=now_utc),
        mock.patch.object(ha_sun, "get_astral_event_date", fake_astral),
        mock.patch.object(dt_util, "DEFAULT_TIME_ZONE", TZ),
    ):
        expected = ha_sun.sun(hass=None, **kwargs)

    ours = evaluate_condition(
        {"condition": "sun", **shape},
        World(
            states={},
            now=now_local,
            event=Event(),
            sun=SunTimes(sunrise=SUNRISE_LOCAL, sunset=SUNSET_LOCAL),
        ),
    )

    assert ours is expected, (
        f"{shape} at {hour:02d}:{minute:02d} -- Home Assistant says {expected}, we say {ours}"
    )
