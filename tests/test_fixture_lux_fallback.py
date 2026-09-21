"""Brzda `cover_down` platí len vtedy, keď sa lux senzoru dá veriť.

`Lighting SUN` píše tú brzdu z luxu, takže senzor zamrznutý nízko ju zapne za
bieleho dňa a dom zostane celý deň zatvorený so svetlami, ktoré nezhasnú.
Presne to sa už raz stalo. Za `lux_neverim` je preto len ona -- **obloha
zostáva primárna**, inak by dom bez `cover_down` a bez lux senzora nemal noc
vôbec a konfigurácia by sa nedala preniesť
(`test_night_is_derived_from_the_sky_not_from_the_helper`).

Prvý návrh bol opačný: slnko ako čistá záloha, lux ako jediný rozhodovač. Ten
test ho zastavil a mal pravdu -- bola by to regresia prenositeľnosti za cenu
troch minút ráno.

Zamrznutie sa z časov zistiť nedá (zmerané 2026-09-02): senzor hlási len pri
zmene, `last_seen` má hodnotu zo 7. augusta, a v noci nezmení hodnotu 9,2 h
legitímne. Preto plauzibilita proti výške slnka -- prahy majú rezervu
2372 lx / 2750 lx a nula falošných zásahov na 14 dňoch histórie.
"""

import datetime as dt

import pytest

from cover_logic.conditions import evaluate_condition
from cover_logic.config_schema import load_config_file
from cover_logic.world import SunTimes, World

LUX = "sensor.predsien_dvere_senzor_illuminance"


@pytest.fixture(scope="module")
def conditions(fixtures_dir):
    return load_config_file(fixtures_dir / "dom_peter.yaml").conditions


def _world(*, hodina, lux, elevacia, brzda):
    den = dt.date(2026, 9, 3)
    teraz = dt.datetime.combine(den, dt.time(hodina, 0))
    return World(
        states={LUX: str(lux), "input_boolean.cover_down": brzda},
        attributes={("sun.sun", "elevation"): elevacia},
        now=teraz,
        sun=SunTimes(
            sunrise=dt.datetime.combine(den, dt.time(5, 53, 18)),
            sunset=dt.datetime.combine(den, dt.time(19, 22, 15)),
        ),
    )


def _je_noc(conditions, world):
    return evaluate_condition(conditions["je_noc"], world, "cover.a", conditions)


def _neverim(conditions, world):
    return evaluate_condition(conditions["lux_neverim"], world, "cover.a", conditions)


# (popis, hodina, lux, elevácia, brzda, lux_neverim, je_noc)
SCENARE = [
    ("zdravý: noc, brzda on", 23, 0, -30, "on", False, True),
    ("zdravý: noc, brzda off -- rozhodne obloha", 23, 0, -30, "off", False, True),
    ("zdravý: deň, brzda off", 12, 3000, 45, "off", False, False),
    ("zdravý: deň, brzda on -- človek vždy vyhrá", 12, 3000, 45, "on", False, True),
    # Presne tá porucha, ktorú užívateľ opisoval: zamrznutý senzor zapne brzdu
    # za bieleho dňa a dom zostane celý deň zatvorený.
    ("ZAMRZOL nízko za dňa, brzda on", 12, 0, 45, "on", True, False),
    ("ZAMRZOL vysoko v noci, brzda off", 23, 3000, -30, "off", True, True),
    ("MŔTVY za dňa, brzda on", 12, "unavailable", 45, "on", True, False),
    ("MŔTVY v noci, brzda off", 23, "unavailable", -30, "off", True, True),
]


@pytest.mark.parametrize(("popis", "hod", "lux", "elev", "brzda", "cakam_n", "cakam_noc"), SCENARE)
def test_je_noc_a_doveryhodnost_luxu(conditions, popis, hod, lux, elev, brzda, cakam_n, cakam_noc):
    world = _world(hodina=hod, lux=lux, elevacia=elev, brzda=brzda)
    assert _neverim(conditions, world) is cakam_n, f"{popis}: lux_neverim"
    assert _je_noc(conditions, world) is cakam_noc, f"{popis}: je_noc"


def test_obloha_zostava_primarna_aj_ked_luxu_neverime(conditions):
    """Prenositeľnosť: dom bez brzdy a bez lux senzora musí mať noc tiež.

    Toto je dôvod, prečo za `lux_neverim` ide len brzda a nie celá derivácia.
    Nedôveryhodný lux nesmie noc *zrušiť* -- smie len odobrať brzde právo
    ju vyhlásiť.
    """
    noc = _world(hodina=23, lux=3000, elevacia=-30, brzda="off")
    assert _neverim(conditions, noc) is True
    assert _je_noc(conditions, noc) is True


def test_rano_drzi_oblohu_ako_doteraz(conditions):
    """Chovanie ráno sa NEmení, a je to zámer.

    Pôvodný návrh nechával o ráne rozhodnúť lux (~2,6 min skôr, v lete podľa
    modelu ~10 min). Bola by to zmena na hranici, ktorú `svitanie` používa na
    reset príznakov izieb -- za tri minúty to nestojí, a obloha je navyše to
    jediné, čo prežije prenos do iného domu.
    """
    pred_hranicou = _world(hodina=5, lux=400, elevacia=-7, brzda="off")
    assert _je_noc(conditions, pred_hranicou) is True


def test_vecer_sleduje_svetla_nie_lux(conditions):
    """`vecer` od 2026-09-21 číta `input_boolean.lighting_on`, lux ho neovplyvní.

    Predtým jeho OR bral skorší z lux/oblohy a `latch: daily` zapnutý za
    ranného šera ho o 12:30 spravil pravdivým aj za jasného dňa -- terasa sa
    zavrela, kým svetlá boli zhasnuté (2026-09-21). Teraz je viazaný na tie
    isté svetlá, takže `vecer` a `lighting_on` sa nemôžu rozísť.
    """
    den = dt.date(2026, 9, 3)

    def svet(*, lighting_on, lux):
        return World(
            states={
                LUX: str(lux),
                "input_boolean.lighting_on": lighting_on,
                "input_boolean.cover_down": "off",
            },
            now=dt.datetime.combine(den, dt.time(13, 0)),  # po 12:30
            sun=SunTimes(
                sunrise=dt.datetime.combine(den, dt.time(5, 53, 18)),
                sunset=dt.datetime.combine(den, dt.time(19, 22, 15)),
            ),
        )

    def vecer(world):
        return evaluate_condition(conditions["vecer"], world, "cover.a", conditions)

    # Jasný deň, svetlá zhasnuté -- nie je súmrak (presne dnešná chyba).
    assert vecer(svet(lighting_on="off", lux=3000)) is False
    # Svetlá svietia -- je súmrak, aj keď lux hlási jasno.
    assert vecer(svet(lighting_on="on", lux=3000)) is True
    # Zamrznutý nízky lux už `vecer` sám osebe nezapne.
    assert vecer(svet(lighting_on="off", lux=0)) is False
