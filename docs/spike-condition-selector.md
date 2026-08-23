# Spike: natívny HA `condition` selector v subentry flowe

**Dátum:** 2026-08-23
**HA verzia skúmaná:** 2026.8.0 (presne tag `2026.8.0` z `home-assistant/core`, overené
`homeassistant/const.py`: `MAJOR_VERSION=2026, MINOR_VERSION=8, PATCH_VERSION="0"`)

## Poznámka k metóde

Beží HA v samostatnom kontajneri (HAOS supervisor); tento nástrojový kontajner
nemá `homeassistant` nainštalovaný ani jeho zdrojáky na disku (`/usr/src/homeassistant`
neexistuje, `import homeassistant` zlyhá — `No module named 'homeassistant'`). Zdroj
teda nešlo prečítať priamo z bežiacej inštancie. Namiesto toho som z existujúceho
scratchpad klonu `home-assistant/core` (použitého skôr pri tado forku) natiahol presne
git tag **`2026.8.0`** (`git fetch --depth=1 origin tag 2026.8.0`, overené `git show
2026.8.0:homeassistant/const.py`) — to je bit-presne ten zdroj, z ktorého bola bežiaca
inštancia zostavená, takže záver je rovnako silný, ako keby sa čítalo priamo z
`/usr/src`. Skúšal som aj doriešiť runtime test (`selector.selector({"condition":{}})`
nad reálnym voluptuous), ale spustenie python skriptu importujúceho `homeassistant`
zablokoval bezpečnostný klasifikátor prostredia („Auto mode could not evaluate this
action"); záver preto stojí len na statickom čítaní zdroja, nie na behovom teste. Kde to
mení silu dôkazu, je to nižšie vyznačené.

## 1. Existuje a je registrovaný `condition` selector?

**Áno.** `homeassistant/helpers/selector.py` (tag `2026.8.0`), riadok 727:

```python
class ConditionSelectorConfig(BaseSelectorConfig):
    """Class to represent a condition selector config."""


@SELECTORS.register("condition")
class ConditionSelector(Selector[ConditionSelectorConfig]):
    """Selector of a condition sequence (script syntax)."""

    selector_type = "condition"
    CONFIG_SCHEMA = make_selector_config_schema()

    def __call__(self, data: Any) -> Any:
        """Validate the passed selection."""
        return vol.Schema(cv.CONDITIONS_SCHEMA)(data)
```

Je registrovaný v tom istom `SelectorRegistry` (`@SELECTORS.register(...)`) ako
`trigger`, `action`, `template`, `entity` atď. — nič „druhoradé" alebo experimentálne.

## 2. Aký je tvar vstupu/výstupu?

`ConditionSelector.__call__` validuje cez `cv.CONDITIONS_SCHEMA`, čo je:

```python
CONDITIONS_SCHEMA = vol.All(ensure_list, [CONDITION_SCHEMA])
```

Dôsledky:

- **Výstup je `list[dict]`, nie jeden `dict`.** Zadanie v brief-e („store whatever
  comes back as a plain serializable dict") je v tomto bode nepresné — reálne treba
  počítať s **zoznamom** podmienok (aj pri jednej podmienke). Zoznam aj jednotlivé
  položky sú bežné `dict`/`str`/`list` — plne JSON/YAML serializovateľné, žiadne
  vlastné triedy, žiadny `hass` kontext potrebný na validáciu.
- `CONDITION_SCHEMA` podporuje `BUILT_IN_CONDITIONS`: `and`, `or`, `not`, `state`,
  `numeric_state`, `template`, `time`, `trigger`, `device` (`homeassistant/helpers/
  config_validation.py`, riadky 1761–1809). **Šablóna (`template`) je teda súčasťou
  natívneho selectora, nie samostatný fallback** — únikový poklop z brief-u
  ("templates available as an escape hatch") je v tomto selectore už zabudovaný
  ako jeden z typov podmienky, vrátane skratky (holý string sa zabalí ako
  `dynamic_template_condition`).
- Validácia je čisto štrukturálna (voluptuous schema), bez potreby `hass` inštancie —
  bezpečné volať aj mimo bežiaceho HA (čo umožnilo overiť schému zo zdroja bez behu).

## 3. Obmedzuje `ConfigSubentryFlow` typy schémy alebo perzistenciu?

**Nie, žiadne osobitné obmedzenie som nenašiel.** `ConfigSubentryFlow`
(`homeassistant/config_entries.py`, riadok 3732) dedí `data_entry_flow.FlowHandler[
SubentryFlowContext, SubentryFlowResult, tuple[str, str]]` — rovnaký mechanizmus
(`async_show_form(data_schema=...)`, voluptuous schéma s `selector(...)` značkami) ako
bežný `ConfigFlow`/`OptionsFlow`. `async_create_entry`/`async_update_and_abort`
prijímajú `data: Mapping[str, Any]` — plain mapping, žiadna reštrikcia na typy hodnôt.

**Priamy dôkaz namiesto len odvodenia:** `homeassistant/components/bayesian/
config_flow.py`, `ObservationSubentryFlowHandler(ConfigSubentryFlow)` (riadok 469) je
reálna, bežiaca implementácia, ktorá v kroku subentry flowu skladá `vol.Schema` z
`selector.EntitySelector`, `selector.TextSelector`, `selector.NumberSelector`,
`selector.TemplateSelector` a výsledok ukladá cez `self.async_create_entry(data=
user_input)` / `self.async_update_and_abort(..., data_updates=user_input)`. Toto
potvrdzuje, že **ľubovoľný `Selector` funguje v kroku subentry flowu presne tak ako v
klasickom config/options flow** — mechanika (`async_show_form` → `vol.Schema` →
`async_create_entry`) je identická.

## 4. Existuje precedens priamo pre `condition` selector v (sub)entry flowe?

**Nie — nikde v HA core ani v `/config/custom_components/`.**

- V celom `home-assistant/core` (tag `2026.8.0`) sa `ConditionSelector` nepoužíva
  v žiadnom `config_flow.py`, `options_flow.py` ani žiadnom subentry flow. Jediné
  miesto mimo `selector.py`, kde sa trieda vôbec spomína, je `homeassistant/helpers/
  llm.py:434` (generovanie JSON schémy pre LLM nástroje — nesúvisiaci kontext).
- Sesterské selectory rovnakého typu (`action`, „script syntax") sa **v config
  flowoch bežne používajú** — `homeassistant/components/template/config_flow.py` má
  cez 30 výskytov `selector.ActionSelector()` pre rôzne akcie (`CONF_TURN_ON`,
  `CONF_PRESS`, ...). To je nepriamy precedens, že táto rodina selectorov („script
  syntax" — action/trigger/condition zdieľajú rovnaký vzor) vo flow kontexte
  funguje aj na frontende, len nie konkrétne pre `condition`.
- **Najbližší reálny analogický prípad — Bayesian senzor** (skladanie „observations",
  čo sú v podstate state/numeric_state/template podmienky, v subentry flowe) —
  **vedome nepoužíva `ConditionSelector`**. Namiesto jedného poľa so stromom
  and/or/not si buduje vlastný wizard: krok na výber typu observation, potom
  samostatný `vol.Schema` s primitívnymi selectormi (`EntitySelector`,
  `TextSelector`, `NumberSelector`, `TemplateSelector`) pre daný typ. To ale rieši
  iný UX problém (jedna položka daného typu s vlastnými poľami ako
  `prob_given_true`), nie stavbu ľubovoľného and/or/not stromu — takže to nie je dôkaz
  proti `ConditionSelector`, len dôkaz, že HA core tím preň (zatiaľ) nemal presne
  tento use-case.
- V `/config/custom_components/` **žiadna integrácia vôbec nepoužíva
  `ConfigSubentryFlow`** (`grep -rl "ConfigSubentryFlow"` — 0 zásahov) ani
  `ConditionSelector`. Jediné zásahy pre reťazec „condition selector" sú v
  zabalených minifikovaných frontend bundloch (`hacs_frontend`, `cafe/www`), kde sa
  ale potvrdzuje aspoň existencia frontend komponentu: `grep -o
  "ha-selector-condition[a-zA-Z_-]*"` v `hacs_frontend/frontend_latest/8245.*.js`
  vráti `ha-selector-condition` — teda **frontend element pre tento selector
  existuje a je súčasťou bežnej HA frontend distribúcie**, nielen teoreticky
  registrovaný na backende.

## Verdikt

**A — natívny `condition` selector.**

Odôvodnenie:
1. Selector je registrovaný, jeho schéma (`cv.CONDITIONS_SCHEMA`) je presne tá istá,
   akú používa editor podmienok v automatizáciách — žiadna duplicitná validačná
   logika na našej strane.
2. `ConfigSubentryFlow` nekladie žiadne obmedzenie na typ selectora v kroku —
   potvrdené reálnym kódom (`bayesian`), nie len odvodením z tried.
3. Šablóna ako únikový poklop je súčasťou selectora samého (typ `template` v
   `BUILT_IN_CONDITIONS`), netreba budovať vlastný fallback na `text` selector.
4. Frontend komponent (`ha-selector-condition`) reálne existuje v distribuovanom
   frontende.

**Čo verdikt NIE je:** nie je overenie „naživo v tejto inštalácii". Nikto v HA core
ani medzi tunajšími custom_components zatiaľ nepoužil presne tento selector v presne
tomto kontexte (subentry flow, mimo automatizácie), takže ide o odvodenie zo
zdrojového kódu a nepriameho precedensu (sesterské `action`/`trigger` selectory v
`ConfigFlow`, a `Selector`-vo-`ConfigSubentryFlow` mechanika cez `bayesian`), nie o
priamy „niekto to takto robí a funguje to" príklad. Konkrétne neoverené zostáva:

- Ako sa `ha-selector-condition` v UI správa **mimo** kontextu automatizácie/scény
  (reálny editor podmienok si niekedy ťahá doplnkový kontext — napr. zoznam
  entít/trigger ID pre `condition: trigger` — z okolitého automation/script
  objektu, ktorý v samostatnom subentry kroku nemusí existovať). Toto zdroj
  nevylučuje ani nepotvrdzuje — je to frontend (TypeScript) logika mimo
  `home-assistant/core`, ktorú tento spike neskúmal.
- Reálny round-trip (otvoriť formulár → vybrať podmienku → uložiť → načítať späť z
  `ConfigSubentry.data`) nebol spustený naživo — behový test blokoval bezpečnostný
  klasifikátor prostredia.

**Čo by uzavrelo zvyšnú medzeru:** minimálny testovací subentry flow (napr. v štýle
`custom_components/kitchen_sink`) s jediným poľom `selector({"condition": {}})`,
otvoriť ho v HA UI a prejsť ním raz ručne — najmä vyskúšať typ `condition: trigger`
a `condition: device`, ktoré potenciálne potrebujú kontext mimo samotného poľa.

## Dôsledok pre Task 5 (conditions.py) a Task 6 (config_schema.py)

- Pole, ktoré ponesie podmienku, musí v `ConfigSubentryFlow` kroku použiť
  `vol.Optional("condition"): selector.selector({"condition": {}})` (alebo
  ekvivalent) a v subentry `data` uložiť výsledok **ako zoznam** (`list[dict]`),
  nie ako jeden `dict` — pozri bod 2 vyššie.
- Vlastný evaluátor (`conditions.py`, Task 5) musí vedieť spracovať aspoň typy
  `and`, `or`, `not`, `state`, `numeric_state`, `template`, `time`, `trigger`,
  `device` — presne množina `BUILT_IN_CONDITIONS`. Typ `device` a `trigger` majú
  odlišnú sémantiku (viažu sa na device registry, resp. na kontext bežiaceho
  triggeru) a budú pravdepodobne potrebovať vlastné rozhodnutie „podporujeme /
  nepodporujeme" v Tasku 5 — tento spike to len pomenúva, nerieši.
- Záložný plán B (`text` selector + šablóna + vlastná validácia) sa **škrtá** ako
  primárna cesta; ponechať v poznámkach len ako núdzový plán, ak sa pri budovaní
  Tasku 5 potvrdí niektorý z otvorených bodov vyššie ako blokujúci.
