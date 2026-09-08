"""The English example configuration must be a valid, complete config.

See `docs/example-config.yaml` and `MODELS.md` ("The configuration format")
for what this file is: a worked example for a different, invented house, not
a translation of `fixtures/dom_peter.yaml` -- that file is simultaneously the
live house's configuration and the migration gate's fixture (see the root
`CLAUDE.md`) and must not be touched for this purpose. An example that does
not parse or validate is worse than no example at all.
"""

from pathlib import Path

import pytest

from cover_logic.config_schema import load_config_file
from cover_logic.validation import ERROR, validate

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "docs" / "example-config.yaml"


@pytest.fixture(scope="module")
def config():
    return load_config_file(EXAMPLE_CONFIG)


def test_example_config_has_no_validation_errors(config):
    errors = [p for p in validate(config) if p.severity == ERROR]
    assert errors == [], errors


def test_example_config_is_a_different_house(config):
    """Different entities, rooms and modes than `fixtures/dom_peter.yaml`."""
    assert "cover.kuchyna_zaluzia_3_6" not in config.blinds
    assert set(config.zones) == {"living_room", "bedroom", "office", "kitchen"}
    assert [mode.id for mode in config.modes] == ["away", "night", "home"]
