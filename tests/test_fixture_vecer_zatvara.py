"""Za súmraku zatvára maticu, nie `Lighting SUN`.

Vetva `light-on` zatvára **každý** tiltový cover s pozíciou > 10 (okrem
kvetov, ktoré rieši zvlášť). Matica dovtedy dávala šiestim z desiatich len
`position: keep` s lamelami 100 — takže `light-on` bola ich **jediný
vlastník** za súmraku, nie duplikát. Zmerané 2026-09-02 v noci; predtým bolo
v poznámkach napísané, že tie volania sú už len duplikát, čo bolo príliš
široké tvrdenie a týkalo sa len dverových žalúzií a kvetov.

Neodporuje to 7.7 („`bezny_den` výšku riadiť nemá"): to meranie bolo o dennom
okne 9-18 a súmrak doňho nepatrí.

Svet je tu pevný a úmyselne nečíta živý dom — pri príprave tejto zmeny sa
uprostred analýzy zmenila prítomnosť (Mimka odišla) a dve inak identické zóny
vyzerali ako keby mali rôzne pravidlá.
"""

import datetime as dt

import pytest

from cover_logic.config_schema import load_config_file
from cover_logic.engine import evaluate
from cover_logic.model import KEEP
from cover_logic.world import Event, SunTimes, World


@pytest.fixture(scope="module")
def config(fixtures_dir):
    return load_config_file(fixtures_dir / "dom_peter.yaml")


DEN = dt.date(2026, 9, 3)
SKY = SunTimes(
    sunrise=dt.datetime.combine(DEN, dt.time(5, 53, 18)),
    sunset=dt.datetime.combine(DEN, dt.time(19, 22, 15)),
)

# Šesť, ktoré `light-on` zatvárala a matica dovtedy nie.
PREVZATE = (
    "cover.kuchyna_zaluzia_3_6",
    "cover.obyvacka_zaluzia_3",
    "cover.mimka_zal",
    "cover.peter_zal",
    "cover.spalna_zaluzia_2",
    "cover.spalna_zaluzia_dvere_1",
)


def _svet(*, hodina, lux=1500, elevacia=3.0, doma=(), odovzdane=("peter", "mimka", "spalna")):
    """Brzda ešte off, po 12:30. `lux`/`elevacia` rozhodujú, či je to súmrak.

    Defaulty sú súmrak (1500 lx, slnko 3 stupne nad horizontom). Denný svet
    musí dať skutočnú dennú hodnotu -- senzor je na streche a cez deň
    saturuje na ~3000, takže 1500 lx o 13:00 čítá matica ako súmrak a robí to
    správne: `light-on` má na to ten istý prah (zamračené popoludnie
    prekročí 2800 skôr, 4 z 13 večerov).
    """
    states = {
        "input_boolean.cover_down": "off",
        "input_boolean.teplotna_ochrana_dom": "off",
        "input_boolean.kvety_on": "on",
        "input_boolean.zaluzie_kuchyna_rucne": "off",
        "input_number.kvety_pozicia_zaluzie": "34",
        "alarm_control_panel.alarmo": "disarmed",
        "sun.sun": "above_horizon",
        "sensor.predsien_dvere_senzor_illuminance": str(lux),
        "binary_sensor.obyvacka_dvere_senzor": "off",
        "binary_sensor.spalna_dvere_senzor_2": "off",
        "binary_sensor.sauna_running": "off",
        "weather.forecast_home": "sunny",
        "weather.openweathermap": "sunny",
    }
    for kto in ("peter", "mimka", "pavel", "majka"):
        states[f"binary_sensor.{kto}_home"] = "on" if kto in doma else "off"
    states["binary_sensor.is_home"] = "on" if doma else "off"
    for izba in ("peter", "mimka", "spalna"):
        states[f"input_boolean.zaluzie_aktivna_{izba}"] = "on" if izba in odovzdane else "off"
    teraz = dt.datetime.combine(DEN, dt.time(hodina, 5))
    return World(
        states=states,
        attributes={
            ("sun.sun", "elevation"): elevacia,
            ("weather.forecast_home", "wind_speed"): 5,
        },
        now=teraz,
        event=Event(),
        sun=SKY,
        since=dict.fromkeys(states, teraz - dt.timedelta(hours=2)),
    )


@pytest.mark.parametrize("blind", PREVZATE)
def test_vecer_zatvara_to_co_predtym_zatvarala_len_automatizacia(config, blind):
    """Prevzatie vlastníctva: bez tohto by po odobraní `light-on` nezatvoril nikto."""
    akcia = evaluate(config, _svet(hodina=19)).targets[blind]

    assert akcia.position == 0, f"{blind} sa za súmraku nezatvára"
    assert akcia.tilt == 100, f"{blind} nemá lamely 100 — obývačka by bola potme"


@pytest.mark.parametrize("blind", PREVZATE)
def test_cez_den_sa_vyska_nadalej_neriadi(config, blind):
    """Kontrola k 7.7: mimo súmraku zostáva výška na človeku.

    Bez tohto by sa z „zatvor za súmraku" nepozorovane stalo „riaď výšku
    celý deň", čo je presne to, čo meranie 31 zmien výšky (19 od ľudí)
    zamietlo.
    """
    akcia = evaluate(config, _svet(hodina=13, lux=3000, elevacia=45.0)).targets[blind]

    assert akcia.position is KEEP, f"{blind} dostal cez deň pozíciu"


def test_obyvana_neodovzdana_izba_zostava_na_pokoji_aj_za_sumraku(config):
    """Jediný zámerný rozdiel proti `light-on`, a je na správnej strane.

    `light-on` zatvárala bez ohľadu na príznak izby. Matica do izby, ktorej
    obyvateľ je doma a neodovzdal ju, nesiaha — nové pravidlo je preto ZA
    odovzdávacím riadkom, nie pred ním. Rozdiel nastane len keď je žalúzia
    v takej izbe hore, a to podľa modelu ručného zásahu znamená, že izba
    odovzdaná byť mala.
    """
    svet = _svet(hodina=19, doma=("peter",), odovzdane=())
    akcia = evaluate(config, svet).targets["cover.peter_zal"]

    assert akcia.position is KEEP
    assert akcia.tilt is KEEP
