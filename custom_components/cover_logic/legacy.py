"""Translate the old Jinja matrix's action vocabulary into `(position, tilt)`.

Pure Python, no Home Assistant imports -- this is the one mapping the
migration gate (`tests/parity/test_migration_gate.py`) is built on, and it is
also what `sensor.py`'s `matica_diff` uses to compare the engine against the
live `sensor.zaluzie_cielovy_stav` in the actual house. Both call sites use
this exact same code so a future disagreement between them cannot happen --
see `tests/parity/mapping.py`, which now only re-exports this module's
functions rather than defining its own copy.
"""

from .model import KEEP, Action


def to_action(akcia: str, hodnota, tilt, *, teplotna_ochrana: bool) -> Action:
    """Translate one legacy `(akcia, hodnota, tilt)` triple into an `Action`."""
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
            # 100 -- it follows teplotna_ochrana_dom.
            tilt = 50 if teplotna_ochrana else 100
        return Action(int(hodnota), int(tilt))
    msg = f"unknown legacy action: {akcia!r}"
    raise AssertionError(msg)


def expected_actions(item: dict, *, variant: str, teplotna_ochrana: bool) -> Action:
    """Translate one legacy `ciele` entry into the `Action` the engine should match.

    `variant` is 'state' or 'arrival'. The legacy `tilt` key is shared by both
    variants -- the template takes it from the state map even for the arrival
    one.
    """
    if variant == "state":
        return to_action(
            item["akcia"], item["hodnota"], item["tilt"], teplotna_ochrana=teplotna_ochrana
        )
    return to_action(
        item["akcia_p"], item["hodnota_p"], item["tilt"], teplotna_ochrana=teplotna_ochrana
    )
