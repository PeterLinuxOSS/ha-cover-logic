"""Placeholders shared by every flow screen and repair issue."""

from collections.abc import Mapping

from .const import DOCS_URL


def docs_placeholders(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return `extra` plus `docs_url`, for `description_placeholders`/`translation_placeholders`."""
    return {"docs_url": DOCS_URL, **(extra or {})}
