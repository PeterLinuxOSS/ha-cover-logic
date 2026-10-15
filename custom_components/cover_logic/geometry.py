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
_EDGE_ON_DEGREES = 90.0


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
    # A broken sensor is an undefined case, not a crash: inf raises in tan(), NaN in int().
    if not all(map(math.isfinite, (elevation, gamma_deg, slat_distance, slat_depth))):
        return None
    if elevation <= 0.0 or slat_depth <= 0.0 or slat_distance < 0.0:
        return None
    # At or past edge-on there is no beam to block, and cos(gamma) would flip sign.
    if abs(gamma_deg) >= _EDGE_ON_DEGREES:
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
