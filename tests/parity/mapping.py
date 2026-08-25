"""Translate the old action vocabulary into the new (position, tilt) pair."""

from __future__ import annotations

from cover_logic.model import KEEP, Action
from cover_logic.world import Event, World

from .jinja_bridge import now_for


def to_action(akcia: str, hodnota, tilt, *, teplotna_ochrana: bool) -> Action:
    if akcia == "nechat":
        return Action(KEEP, KEEP)
    if akcia == "zavriet":
        return Action(0, 0 if hodnota is None else int(hodnota))
    if akcia == "tilt":
        return Action(KEEP, int(hodnota))
    if akcia == "hore":
        return Action(100, KEEP)
    if akcia == "pozicia":
        if tilt is None:
            # scripts.yaml, `pozicia_tilt_ciel`: a missing tilt is NOT a fixed
            # 100 — it follows teplotna_ochrana_dom.
            tilt = 50 if teplotna_ochrana else 100
        return Action(int(hodnota), int(tilt))
    msg = f"unknown legacy action: {akcia!r}"
    raise AssertionError(msg)


def expected_actions(item: dict, *, variant: str, teplotna_ochrana: bool) -> Action:
    """`variant` is 'state' or 'arrival'.

    The legacy `tilt` key is shared by both variants — the template takes it
    from the state map even for the arrival one.
    """
    if variant == "state":
        return to_action(item["akcia"], item["hodnota"], item["tilt"],
                         teplotna_ochrana=teplotna_ochrana)
    return to_action(item["akcia_p"], item["hodnota_p"], item["tilt"],
                     teplotna_ochrana=teplotna_ochrana)


def world_from_stav(stav, event: Event | None = None) -> World:
    """Feed the engine exactly the state the Jinja render saw."""
    return World(
        states=stav.entity(),
        attributes=stav.atributy(),
        now=now_for(stav),
        event=event or Event(),
    )
