"""`capabilities.py` mirrors Home Assistant's own cover feature bits.

That module is pure (`tests/test_purity.py`'s `PURE_MODULES`), so it cannot
import `CoverEntityFeature` and spells the bits itself. A mirror kept by hope
is a mirror that drifts, and the drift would be silent in the worst way: the
check would go on comparing bitmasks and quietly compare the wrong bits, so a
blind that cannot tilt would pass and one that can would be flagged.

Lives in `tests/ha/` for the obvious reason -- it is the only leg where
`homeassistant` is importable.
"""

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.cover import CoverEntityFeature

from cover_logic import capabilities


def test_every_mirrored_bit_matches_home_assistant():
    ours = {name: bit for bit, name in capabilities.FEATURE_NAMES.items()}
    theirs = {feature.name: feature.value for feature in CoverEntityFeature}

    assert ours, "counter: the mirror must not be empty"
    assert {name: theirs[name] for name in ours} == ours


def test_the_mirror_names_only_features_home_assistant_has():
    """A typo in a mirrored name would otherwise sit there forever."""
    theirs = {feature.name for feature in CoverEntityFeature}

    assert set(capabilities.FEATURE_NAMES.values()) <= theirs
