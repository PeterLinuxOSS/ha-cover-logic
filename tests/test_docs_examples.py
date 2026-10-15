"""Every ```yaml block marked `title="config"` in the site must load."""

from pathlib import Path
import re

import pytest

from cover_logic.config_schema import load_config

_SITE = Path(__file__).parent.parent / "docs" / "site"
_BLOCK = re.compile(r'```yaml title="config"\n(.*?)```', re.DOTALL)
_CASES = [
    (p.relative_to(_SITE), b) for p in _SITE.rglob("*.md") for b in _BLOCK.findall(p.read_text())
]


@pytest.mark.parametrize(("page", "block"), _CASES, ids=[str(c[0]) for c in _CASES])
def test_config_example_loads(page, block):
    load_config(block)


def test_there_are_examples():
    assert len(_CASES) >= 7
