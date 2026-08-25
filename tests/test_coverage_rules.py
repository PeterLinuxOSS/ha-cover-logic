"""Every rule must be reachable, and the suite must prove it by firing it."""

from __future__ import annotations

import pytest
from scenarios import fired_rules, worlds

from cover_logic.config_schema import load_config_file
from cover_logic.engine import evaluate


@pytest.fixture(scope="module")
def config(fixtures_dir):
    return load_config_file(fixtures_dir / "dom_peter.yaml")


@pytest.fixture(scope="module")
def all_worlds(config):
    return worlds(config)


def test_the_derived_space_is_not_trivial(all_worlds):
    assert len(all_worlds) > 50, len(all_worlds)


def test_every_rule_fires_at_least_once(config, all_worlds):
    fired = fired_rules(config, all_worlds)
    dead = [
        f"{key}#{index}"
        for key, rules in config.rules.items()
        for index in range(len(rules))
        if f"{key}#{index}" not in fired
    ]
    assert not dead, (
        "these rules never fired — either unreachable, or the derived scenario "
        f"space is too narrow: {dead}"
    )


def test_no_decision_falls_through_to_the_implicit_default(config, all_worlds):
    fallthrough = []
    for world in all_worlds:
        for entity, label in evaluate(config, world).trace.items():
            if label.endswith("#none"):
                fallthrough.append((entity, label))
    assert not fallthrough, fallthrough[:5]
