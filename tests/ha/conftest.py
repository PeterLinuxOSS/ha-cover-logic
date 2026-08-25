"""Fixtures for the ha_world tests.

Everything here imports Home Assistant, so the whole package only collects
under the Python 3.14 venv (see the module-level `importorskip` in
`test_ha_world.py`). This file itself does not import `homeassistant` at
module level, so it is harmless to import under system Python 3.12 --
collection is skipped by the test module, not by this conftest.
"""

import pytest

from cover_logic.config_schema import load_config

# A minimal config that exercises all three shapes `referenced_entities`
# produces: a plain state read (`input_boolean.a`), an attribute read
# (`cover.a`, `current_position`), and a `values:` helper entity
# (`input_number.kvety_pozicia_zaluzie`).
CONFIG_TEXT = """
blinds:
  - entity: cover.a
    facade_azimuth: 270
zones:
  terasa:
    members: [cover.a]
modes:
  - {id: bezny_den}
conditions:
  position_high:
    condition: state
    entity_id: cover.a
    attribute: current_position
    state: 100
  boolean_on:
    condition: state
    entity_id: input_boolean.a
    state: "on"
values:
  kvety_poz:
    entity: input_number.kvety_pozicia_zaluzie
    default: 34
rules:
  bezny_den.terasa:
    - {then: {position: keep, tilt: keep}}
"""


@pytest.fixture
def config():
    """A `Config` whose `referenced_entities()` covers state, attribute and value reads."""
    return load_config(CONFIG_TEXT)


class FakeStateMachine:
    """Stands in for `hass.states`: only the `.get()` read path `build_world` uses."""

    def __init__(self, states):
        """Store the entity_id -> State mapping this fake serves."""
        self._states = states

    def get(self, entity_id):
        """Return the `State` for `entity_id`, or `None` if it was never set."""
        return self._states.get(entity_id)


class FakeHass:
    """A minimal stand-in for `HomeAssistant`.

    `build_world` only ever reads `hass.states.get(entity_id)`. Constructing a
    real `HomeAssistant` needs a running event loop and on-disk config
    directory -- disproportionate for exercising one read path. This fake
    carries real `homeassistant.core.State` objects (constructed standalone,
    no event loop required), so attribute types and `.attributes` semantics
    are the genuine Home Assistant behaviour, not a reimplementation of it.
    """

    def __init__(self, states):
        """Wrap `states` (entity_id -> `State`) behind a `.states.get()` façade."""
        self.states = FakeStateMachine(states)


@pytest.fixture
def fake_hass():
    """Factory: `fake_hass({"cover.a": State(...)})` -> a `FakeHass`."""
    return FakeHass
