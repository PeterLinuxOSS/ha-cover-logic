"""Config flow for Cover Logic: the setup menu, plus registering the subentry flows.

**The `user` step is a menu, not a form.** Before phase 5 this was one text
field asking for a configuration file path -- a dead end for anyone who just
installed the integration from HACS and has no file to point at (the
owner's own words, quoted in `options_flow.py`'s module docstring). It is
now `async_show_menu` over four ways to start, each its own step below:

- `blinds_now` (recommended): a multi-select over `cover` entities, then one
  compass-direction question per blind picked, then a starter configuration
  (a zone holding every blind, a day mode that shades toward the sun and a
  night mode that leaves everything alone) generated and shown before it is
  saved. See this branch's own docstring and `_build_starter_config` for why
  a config entry that decides nothing is exactly the problem this replaces.
- `from_file`: today's original behaviour, kept for migration -- a
  configuration file path, validated on submit exactly as before.
- `from_example`: imports `docs/example-config.yaml`, the worked example for
  a different, invented house (see that file's own header and
  `tests/test_example_config.py`) -- a plausible-looking starting point
  instead of an empty one. Only offered where that file actually ships
  alongside this checkout (see `conformance.repo_example_config_path`); see
  `async_step_from_example`'s own docstring for what happens on an install
  where it does not.
- `empty`: a config entry with no subentries at all.

`async_set_unique_id`/`_abort_if_unique_id_configured` still run first, in
`async_step_user`, before the menu is even shown -- exactly where they ran
before this task, so the single-instance guarantee does not depend on which
menu item is picked. Every one of the four branches ends in
`async_create_entry`, which is what actually enforces "only one instance":
`ConfigFlow.async_step_user` is Home Assistant's own one entry point for a
brand-new flow (`source: user`), so there is no route into `blinds_now`/
`from_file`/`from_example`/`empty` that skips it.

"Whatever this creates must load, even incomplete." `empty` has nothing at
all: `config_from_subentries` (`config_store.py`) still builds a `Config`
from it -- it only rejects a structurally broken shape (a required key
missing), never an incomplete-but-well-formed one. `validate()` is what
finds `no_fallback_mode` on such a `Config`, and `__init__.async_setup_entry`
turns that into `ConfigEntryNotReady` exactly as it already does for any
subentry-backed entry with an ERROR-severity problem (this predates phase 5).
That is a deliberate line, not an oversight: `ConfigEntryNotReady` is a
normal, expected state for an entry whose subentries still need work, not a
crash -- the entry keeps existing, and its subentries (and, since the
previous task, the options-flow menu over them) stay reachable regardless of
whether setup succeeded, which is how a user actually finishes what `empty`
started. What must never happen is `config_from_subentries` or
`async_setup_entry` raising something that is *not* one of these two clean,
already-handled outcomes -- see `__init__.async_setup_entry`'s own docstring
for the one place this had to change to keep that true for an entry with
genuinely zero subentries (`entry.subentries` truthiness stopped being the
signal for "read from subentries"; `CONF_CONFIG_PATH`'s presence in
`entry.data` is, now).

`blinds_now` used to leave the same gap (zero zones, zero modes -- a blind
with nothing deciding it) until this task: a brand-new user landed on
exactly the `ConfigEntryNotReady` state described above with no explanation
of why, which is not "incomplete on purpose", it is the bug phase 6 task 4
exists to close. `blinds_now` now runs `validate()` on the configuration it
is about to create (`async_step_blinds_now_summary`, below) and refuses to
call `async_create_entry` at all if that configuration has an ERROR-severity
problem -- see `_build_starter_config`'s own docstring for why that should
never happen, and what it means if it ever does.

The four setup steps are this module's whole content. The subentry flows
that let a user build the same configuration by clicking, one
`blind`/`zone`/`value`/`condition`/`mode`/`rule` row at a time, instead of
hand-writing YAML, live in `subentry_flow.py` -- this module only registers
them (`async_get_supported_subentry_types` returns that module's
`SUBENTRY_FLOW_HANDLERS`) rather than defining them itself. See
`subentry_flow.py`'s own module docstring for why: those six classes are
also the phase 5 options-flow menu's (`options_flow.py`) second door onto
the same six types, so they belong to neither this module nor that one.
`config_store.py` is the reviewed, tested *reader* of those subentries -- it
already decides exactly which keys it reads out of each type's `data` and
which are required; the flows in `subentry_flow.py` exist only to *produce*
data in that exact shape, never to invent a spelling of their own.
`blinds_now`/`from_example` below reuse that same reader's write side
(`config_store.subentries_from_config`) rather than hand-building subentry
`data` a second time -- see each step's own docstring.

Unlike `__init__.py`, this module has no reason to defer its Home Assistant
imports: it is never imported by `cover_logic/__init__.py` itself (only
discovered and imported by Home Assistant's own config flow machinery, or
-- behind `pytest.importorskip("homeassistant")` -- by `tests/ha/`), so it
never runs anywhere `homeassistant` is not already installed. It also has no
reason to defer its import of `options_flow.py`, or vice versa: since
`subentry_flow.py` split off as the module both of them actually needed,
neither of the two imports the other any more, so `async_get_options_flow`
below imports `CoverLogicOptionsFlow` at module level like anything else.
"""

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
import voluptuous as vol

from .conditions import SUN_ENTITY
from .config_schema import ConfigError, load_config_file
from .config_store import subentries_from_config
from .conformance import repo_example_config_path
from .const import (
    COND_SUN_HITS_TARGET,
    CONF_CONFIG_PATH,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CONFIG_PATH,
    DOMAIN,
    RULE_DEFAULT_ZONE,
)
from .model import Action, Blind, Config, Mode, Rule, Zone
from .options_flow import CoverLogicOptionsFlow
from .services import _title_for
from .subentry_flow import SUBENTRY_FLOW_HANDLERS
from .validation import ERROR, validate

_LOGGER = logging.getLogger(__name__)


async def _describe_problems(hass: HomeAssistant, path: str) -> str | None:
    """Load and validate `path`; return a problem summary, or `None` if it is clean.

    "Clean" means no `ERROR`-severity problem -- a `WARNING`-only config is
    accepted here exactly as `async_setup_entry` accepts it. `load_config_file`
    does a blocking `Path.read_text`, so it runs inside
    `hass.async_add_executor_job` rather than directly on the event loop this
    step is awaited from -- the same reasoning as `__init__.async_setup_entry`.
    """
    try:
        config = await hass.async_add_executor_job(load_config_file, path)
    except ConfigError as err:
        return str(err)
    except OSError as err:
        return str(err)

    errors = [problem for problem in validate(config) if problem.severity == ERROR]
    if not errors:
        return None

    return "; ".join(f"{problem.code}: {problem.message}" for problem in errors)


_STEP_BLINDS_NOW = "blinds_now"
_STEP_FROM_FILE = "from_file"
_STEP_FROM_EXAMPLE = "from_example"
_STEP_EMPTY = "empty"

# The one field `blinds_now` asks for: which `cover` entities to build a
# `blind` subentry for, one each. Filtered to `domain="cover"` for the same
# reason `BlindSubentryFlowHandler`'s own `entity` field is (see that class's
# docstring) -- a blind is a `cover` entity by definition.
_BLINDS_NOW_FIELD = "entities"
_BLINDS_NOW_SCHEMA = vol.Schema(
    {
        vol.Required(_BLINDS_NOW_FIELD, default=list): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="cover", multiple=True)
        ),
    }
)

_STEP_BLINDS_NOW_FACING = "blinds_now_facing"
_STEP_BLINDS_NOW_SUMMARY = "blinds_now_summary"

# `facade_azimuth` is a number of degrees (`model.Blind`, `MODELS.md` Sec. 4)
# -- correct for the engine, meaningless to someone standing in their own
# house deciding which way a window faces. Ordered clockwise from north (the
# order a compass rose is drawn in), not alphabetised, so the select widget
# reads as a compass rather than a word list; `_FACING_SCHEMA` below passes
# this order straight through with `sort=False` for the same reason.
_COMPASS_TO_AZIMUTH: dict[str, float] = {
    "north": 0.0,
    "northeast": 45.0,
    "east": 90.0,
    "southeast": 135.0,
    "south": 180.0,
    "southwest": 225.0,
    "west": 270.0,
    "northwest": 315.0,
}

_FACING_FIELD = "facing"
_FACING_TRANSLATION_KEY = "facing"
# `translation_key` (not just each option's raw internal id, `list(
# _COMPASS_TO_AZIMUTH)`) is what makes this dropdown's *labels* translate --
# every other `SelectSelector` in this codebase lists entity ids or a
# user-chosen id, which are inherently untranslatable, so this is the one
# spot with a fixed, closed set of options worth naming. Home Assistant
# resolves each option's shown label from `strings.json["selector"]
# [_FACING_TRANSLATION_KEY]["options"][<option>]` (and the matching
# `translations/*.json` entry) rather than the option string itself.
_FACING_SCHEMA = vol.Schema(
    {
        vol.Required(_FACING_FIELD, default="north"): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(_COMPASS_TO_AZIMUTH),
                sort=False,
                translation_key=_FACING_TRANSLATION_KEY,
            )
        ),
    }
)

# The zone/mode ids `_build_starter_config` generates. Plain, short and
# English on purpose -- like every other id in this project's format
# (`MODELS.md` Sec. 4), these are internal keys read back out of subentries,
# never shown to the user translated; the *labels* around them (this
# module's own `strings.json` entries) are what carry the explanation.
_BLINDS_NOW_ZONE = "all"
_MODE_DAY = "day"
_MODE_NIGHT = "night"

# The starter rule's shading amount: enough to cut direct sun without
# blacking the room out, on the theory that a new user tunes this to taste
# afterwards (see `strings.json`'s own summary text) rather than never
# touching it -- picking values that make no difference either way would
# defeat the point of generating a working default at all.
_SHADE_POSITION = 20
_SHADE_TILT = 45
_OPEN_POSITION = 100
_OPEN_TILT = 100


def _build_starter_config(entities: list[str], facings: dict[str, str]) -> Config:
    """The configuration `blinds_now` creates: one zone, a day/night split, shading rules.

    This is the whole answer to "the entry decides nothing" (see this
    module's own docstring and the phase 6 task 4 plan this implements):
    every blind picked ends up owned by one zone, and every `(mode, zone)`
    pair that zone participates in has rules that resolve to something.

    **One zone, not one per blind.** `blinds_now` has no notion yet of which
    blinds belong together -- that is exactly the kind of decision the
    options-flow menu exists for a user to make deliberately afterwards. A
    single zone holding every blind is the only grouping this step can offer
    without guessing, and it is enough for `validate()` to find nothing
    unowned.

    **Two modes, one inherited default each -- not one rule per zone.** This
    is deliberately the shape phase 6's task 1-3 added inheritance for
    (`RULE_DEFAULT_ZONE`, `"*"`): a rule filed under `f"{mode}.*"` decides
    every zone in that mode, so a single-zone starter set and a ten-zone one
    generated from the same picks would need the same two rule lists, not
    one pair per zone. It is also the clearest illustration of the feature
    this project's own `MODELS.md`/`docs/rationale.md` could ship with a
    fresh install.

    **Day is `sun.sun` in `above_horizon`, night is the fallback.** Not a
    `time` condition on a fixed clock: the house this project replaces
    recorded a DST bug from exactly that shape (comparing wall-clock times
    as strings without accounting for the UTC offset -- see `/config/
    CLAUDE.md`'s own entry on it), which is also why `MODELS.md` Sec. 6 has
    `World.now` stay naive local time rather than convert by hand. A state
    check on the sun entity needs no clock arithmetic at all to get "day"
    and "night" right through a DST transition. `night` carries `when=None`
    and is last -- `validation.
    _check_modes`'s fallback requirement -- so "no mode matched" never
    happens (`engine.evaluate` would otherwise raise `EngineError`).

    **Night's one rule is `keep`/`keep`, named, not an empty rule list.**
    `engine._apply_rules` already treats "no rules for this key" as keep/keep
    (see `MODELS.md` Sec. 3's "#none" ambiguity) -- an empty `night.*` would
    decide exactly the same thing. An explicit rule instead means `validate`
    (`_check_rule_lists`'s `missing_rule_list`/`no_catch_all`) has something
    to see and stays silent, and the decision trace names why nothing moved
    instead of showing the ambiguous "#none" a genuinely unconfigured pair
    would.

    **Day's two rules: shade when the sun hits, otherwise stay open.**
    `sun_hits_target` (`conditions._sun_hits_target`) is target-relative --
    it reads `facade_azimuth` off the blind actually being decided, not off
    the condition body -- so one rule pair generalises across every facing
    the facing step collected, exactly as `MODELS.md` Sec. 3 describes for
    a hand-written house.

    Raises nothing of its own; `Config` and `Blind` validate their own
    fields (`model.py`), and whatever `validate()` finds is this function's
    caller's problem to act on (`async_step_blinds_now_summary`), not this
    function's -- see that step's own docstring for why a problem here would
    be this function's bug, not a user-facing error.
    """
    blinds = {
        entity: Blind(entity=entity, facade_azimuth=_COMPASS_TO_AZIMUTH[facings[entity]])
        for entity in entities
    }
    zone = Zone(id=_BLINDS_NOW_ZONE, members=tuple(entities))
    modes = (
        Mode(
            id=_MODE_DAY,
            when={"condition": "state", "entity_id": SUN_ENTITY, "state": "above_horizon"},
        ),
        Mode(id=_MODE_NIGHT, when=None),
    )
    rules = {
        f"{_MODE_DAY}.{RULE_DEFAULT_ZONE}": (
            Rule(
                when={
                    "condition": COND_SUN_HITS_TARGET,
                    # Explicit, not `conditions._sun_hits_target`'s own
                    # `azimuth_entity`/`DEFAULT_AZIMUTH_ENTITY` default
                    # (`sensor.sun_solar_azimuth`): that entity is disabled by
                    # default in stock Home Assistant (`docs/phase-2-
                    # findings.md` §3), so a fresh install's `world.number`
                    # would fall back to the "impossible" sentinel every
                    # time, this condition would never once be true, and the
                    # day mode's shading rule this starter config just
                    # promised would silently never fire. `sun.sun`'s own
                    # `azimuth` attribute is what stock Home Assistant
                    # actually populates.
                    "azimuth_entity": SUN_ENTITY,
                    "azimuth_attribute": "azimuth",
                },
                then=Action(position=_SHADE_POSITION, tilt=_SHADE_TILT),
                name="shade: sun is on this side",
            ),
            Rule(
                when=None,
                then=Action(position=_OPEN_POSITION, tilt=_OPEN_TILT),
                name="otherwise: stay open",
            ),
        ),
        f"{_MODE_NIGHT}.{RULE_DEFAULT_ZONE}": (
            Rule(when=None, then=Action(), name="night: do not move"),
        ),
    }
    return Config(blinds=blinds, zones={zone.id: zone}, modes=modes, rules=rules)


def _describe_starter_config(entities: list[str], facings: dict[str, str]) -> str:
    """The plain-language explanation `async_step_blinds_now_summary` shows before saving.

    Names no mode/zone/rule by its internal id or by the words "mode",
    "zone" or "rule" themselves -- see the phase 6 task 4 plan's "concepts
    are not shown until needed". What it does name is exactly what
    `_build_starter_config` above builds, so the two cannot drift apart the
    way a hand-maintained description of generated code always eventually
    does: change one function's behaviour and the other's wording goes
    stale silently. A future change to either should keep reading the other
    before touching wording, not treat them as independent.

    Does not repeat each blind's facing next to its name (an earlier version
    of this text printed the raw `_COMPASS_TO_AZIMUTH` key, e.g. "(facing
    north)") -- `facings` is still accepted for that reason, but no longer
    read here. This is a plain, synchronous helper with no `hass`/language
    available to it, so it cannot resolve `_FACING_TRANSLATION_KEY`'s
    localized label the way the dropdown that collected the answer does
    (see `_FACING_SCHEMA`'s own comment); printing the internal compass id
    verbatim here would reintroduce, in this summary, exactly the
    untranslatable-label problem that dropdown exists to avoid. The user
    just answered this question, one screen per blind, immediately before
    reaching this one -- so this summary's job is to say what will happen,
    not to redisplay an answer already given on a translated screen.
    """
    blind_list = "\n".join(f"- {entity}" for entity in entities)
    return (
        f"{blind_list}\n\n"
        f"At night, nothing moves.\n"
        f"During the day, each blind closes partway (to position "
        f"{_SHADE_POSITION}, tilt {_SHADE_TILT}) whenever the sun is shining "
        f"on its own side of the house, and stays fully open (position "
        f"{_OPEN_POSITION}) the rest of the time."
    )


def _subentry_data(
    subentry_type: str, data: dict[str, Any], title: str
) -> config_entries.ConfigSubentryData:
    """Build one `ConfigSubentryData` entry for `async_create_entry`'s `subentries=`.

    A `TypedDict`, not a real object -- `ConfigFlow.async_create_entry`
    wraps each of these into a real `ConfigSubentry` itself once the entry is
    actually created, the same way `hass.config_entries.async_add_subentry`
    does for `services._async_import_config` (see that function's own
    docstring); this step just needs to hand back plain, JSON-shaped data in
    that exact shape, not build a `ConfigSubentry` by hand.
    """
    return {"data": data, "subentry_type": subentry_type, "title": title, "unique_id": None}


class CoverLogicConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cover Logic: the setup menu, and its four branches."""

    VERSION = CONFIG_ENTRY_VERSION

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the setup menu -- see the module docstring for its four branches.

        Checks "already configured" first, before even showing the menu --
        this integration supports exactly one instance, and every branch
        below ends in `async_create_entry`, so there is no route past this
        check into a second entry.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        return self.async_show_menu(
            step_id="user",
            menu_options=[_STEP_BLINDS_NOW, _STEP_FROM_FILE, _STEP_FROM_EXAMPLE, _STEP_EMPTY],
        )

    # Set by `async_step_blinds_now`, consumed and updated by
    # `async_step_blinds_now_facing`; all three are `None` until that branch
    # is actually picked, and reset to `None` on that first submit (never
    # partially populated from some earlier, abandoned attempt) so a second
    # trip through `blinds_now` after backing out never inherits state left
    # over from a first.
    _blinds_now_entities: list[str] | None = None
    _blinds_now_pending: list[str] | None = None
    _blinds_now_facings: dict[str, str] | None = None

    async def async_step_blinds_now(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Set up blinds now, step one: which `cover` entities to build a `blind` for.

        The recommended branch -- the one thing a brand-new user can answer
        without knowing anything about zones, modes or rules yet. Does not
        create the entry itself any more (see this module's own docstring
        for why leaving it at "blinds with nothing deciding them" was the
        bug this task closes): a non-empty pick moves on to
        `async_step_blinds_now_facing`, which asks the one further question
        (which way each blind faces) `_build_starter_config` needs, then
        `async_step_blinds_now_summary`, which builds, validates and shows
        that configuration before saving it.

        At least one entity must be picked: zero would produce the same
        entry `empty` already offers on purpose, under a menu item that
        promises blinds.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            entities: list[str] = user_input[_BLINDS_NOW_FIELD]
            if not entities:
                errors["base"] = "no_blinds_selected"
            else:
                self._blinds_now_entities = entities
                self._blinds_now_pending = list(entities)
                self._blinds_now_facings = {}
                return await self.async_step_blinds_now_facing(None)

        schema = self.add_suggested_values_to_schema(_BLINDS_NOW_SCHEMA, user_input)
        return self.async_show_form(step_id=_STEP_BLINDS_NOW, data_schema=schema, errors=errors)

    async def async_step_blinds_now_facing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Set up blinds now, step two: which compass direction each blind faces.

        One screen per blind rather than one field per blind on a single
        screen: a form's fields come from a `vol.Schema` built with literal
        `vol.Required("...")` calls (`tests/test_translations.py`'s
        `test_top_level_config_flow_step_fields_are_declared` derives every
        step's translatable fields by walking that step method's own source
        for exactly that literal shape), so a field name assembled at
        runtime per entity (`vol.Required(entity_id)`) would have no static
        label to check against and would be invisible to that guard. Reusing
        one fixed field (`_FACING_FIELD`) and one fixed `step_id`, asked once
        per entry in `self._blinds_now_pending` with the current blind named
        in `description_placeholders`, keeps the field itself static while
        still asking about each blind by name -- for the handful of blinds a
        single `blinds_now` pick realistically selects, one click per screen
        is not the burden a form with an entity's full id as its field label
        would be either way.

        `self._blinds_now_pending` is a queue: the entity named on the
        screen just answered is always its front (`[0]`), popped here before
        checking what remains, because nothing else runs between "this
        screen is shown for entity X" and "this screen's answer is submitted
        for entity X" in one single-user, synchronous flow -- there is no
        way for the queue's front to change out from under a specific
        pending answer.
        """
        if user_input is not None:
            entity = self._blinds_now_pending.pop(0)
            self._blinds_now_facings[entity] = user_input[_FACING_FIELD]

        if not self._blinds_now_pending:
            return await self.async_step_blinds_now_summary(None)

        entity = self._blinds_now_pending[0]
        return self.async_show_form(
            step_id=_STEP_BLINDS_NOW_FACING,
            data_schema=_FACING_SCHEMA,
            description_placeholders={"blind": entity},
        )

    async def async_step_blinds_now_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Set up blinds now, step three: show the generated configuration, then save it.

        Builds `_build_starter_config`'s `Config` and runs `validate()`
        against it on *every* render of this step, not only once when the
        answers first arrive -- "validate() must report no errors on what is
        generated, checked as part of the flow, not only by a test" is a
        hard requirement of the task this implements. An ERROR-severity
        problem here can only be this module's own bug (`_build_starter_
        config`'s output is fully determined by this function's own code and
        the compass answers just collected, never by anything else a user
        typed that could be wrong on its own), so this refuses to create the
        entry and aborts loudly with a message that says so, rather than
        inventing an `errors["base"]` a user could not act on -- a config
        this flow does not understand well enough to show correctly is not
        one it should silently create either.

        The submit branch below guards `subentries_from_config` the same
        way, for the same reason: its own round-trip self-check
        (`config_store.py`) can raise `ConfigError` on a `Config` that has
        already passed `validate()` clean, which by that function's own
        docstring means `subentries_from_config`/`config_from_subentries`
        themselves disagree with each other, not that `config` is wrong.
        Same bug class, same `starter_config_invalid` abort -- an
        unhandled `ConfigError` here would otherwise escape as Home
        Assistant's generic "Unknown error occurred" instead.

        Description-only summary before the one confirming submit --
        `_describe_starter_config` -- is what makes this step answer "what
        was created and why" instead of a bare "done", the plan's own
        complaint about a config that "appears without explanation".
        """
        entities = self._blinds_now_entities
        facings = self._blinds_now_facings
        config = _build_starter_config(entities, facings)
        problems = [problem for problem in validate(config) if problem.severity == ERROR]
        if problems:
            _LOGGER.error(
                "blinds_now generated a starter configuration validate() rejects: %s", problems
            )
            return self.async_abort(reason="starter_config_invalid")

        if user_input is not None:
            try:
                items = subentries_from_config(config)
            except ConfigError as err:
                # Same bug class as the `validate()` guard above, caught one
                # step later: `subentries_from_config`'s own round-trip
                # self-check (`config_store.py`) raises `ConfigError` if what
                # it is about to hand back does not read back to an equal
                # `Config` -- by its own docstring, that can only be a bug in
                # `subentries_from_config`/`config_from_subentries`
                # themselves, never in this already-`validate()`-clean
                # `config`. Left uncaught, this `ConfigError` would escape
                # the flow as Home Assistant's generic "Unknown error
                # occurred" with a traceback instead of the same loud,
                # actionable abort the sibling guard above already gives.
                _LOGGER.error(
                    "blinds_now generated a starter configuration "
                    "subentries_from_config could not round-trip: %s",
                    err,
                )
                return self.async_abort(reason="starter_config_invalid")

            subentries = [
                _subentry_data(subentry_type, data, _title_for(subentry_type, data))
                for subentry_type, data in items
            ]
            return self.async_create_entry(
                title="Cover Logic",
                data={"guards": list(config.guards)},
                subentries=subentries,
            )

        return self.async_show_form(
            step_id=_STEP_BLINDS_NOW_SUMMARY,
            data_schema=vol.Schema({}),
            description_placeholders={"summary": _describe_starter_config(entities, facings)},
        )

    async def async_step_from_file(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Load a configuration from a YAML file: the original `user` step, unchanged.

        Kept for migration -- moving a configuration from one house/instance
        to another, or restoring one written by hand -- not removed just
        because it is no longer the first thing a new user sees. Validated
        on submit exactly as before this task: loaded and run through
        `validate()`, so a broken path never becomes a broken entry.
        """
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] | None = None

        if user_input is not None:
            path = user_input[CONF_CONFIG_PATH]
            problem = await _describe_problems(self.hass, path)
            if problem is None:
                return self.async_create_entry(title="Cover Logic", data=user_input)

            errors["base"] = "invalid_config"
            description_placeholders = {"error_detail": problem}
            _LOGGER.debug("cover_logic config %s rejected: %s", path, problem)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CONFIG_PATH,
                    default=(user_input or {}).get(CONF_CONFIG_PATH, DEFAULT_CONFIG_PATH),
                ): str,
            }
        )
        return self.async_show_form(
            step_id=_STEP_FROM_FILE,
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_from_example(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Start from the example configuration: import `docs/example-config.yaml`.

        That file ships only in this project's own development checkout
        (see `conformance.repo_example_config_path`'s docstring) -- a HACS
        install or a manual copy of just `custom_components/cover_logic` has
        no sibling `docs/` directory at all. Rather than offering a menu
        button that can only ever fail there, or aborting the whole flow the
        one time it is picked on such an install (which would force starting
        the entire setup over, the opposite of "always land somewhere you
        can continue from"), this delegates to `async_step_example_not_
        available` -- a real step of its own, not just a different
        `step_id` rendered from here: Home Assistant's own flow manager
        checks, on *every* step result, that `result["step_id"]` names a
        real `async_step_<step_id>` method (`FlowManager._raise_if_step_
        does_not_exist`) -- including the render that shows a form, not only
        the resume that follows it -- so reusing this method's own name for
        that other form's `step_id` would raise `UnknownStep` the moment a
        real installation (not this project's own tests, which call each
        method directly and so never hit the manager's check) rendered it.

        Where the file *is* available, reuses `config_store.
        subentries_from_config` -- the exact function `services.
        _async_import_config` already uses to turn an imported `Config` into
        subentries -- rather than a second, hand-rolled conversion; see that
        function's own docstring for the ordering (assign strictly
        increasing `order` to modes/rules, then self-check the result reads
        back to an equal `Config`) this reuses unchanged. `services.
        _title_for` supplies the same cosmetic subentry titles
        `_async_import_config`/`async_migrate_entry` already give an
        imported subentry, for the same reason those two reuse it instead of
        inventing a third copy.
        """
        example_path = repo_example_config_path()
        if example_path is None:
            return await self.async_step_example_not_available(user_input)

        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] | None = None

        if user_input is not None:
            try:
                config = await self.hass.async_add_executor_job(load_config_file, example_path)
                items = subentries_from_config(config)
            except (ConfigError, OSError) as err:
                errors["base"] = "invalid_config"
                description_placeholders = {"error_detail": str(err)}
            else:
                subentries = [
                    _subentry_data(subentry_type, data, _title_for(subentry_type, data))
                    for subentry_type, data in items
                ]
                # `guards` (see `config_store.py`'s own docstring) is not a
                # subentry type -- it is carried through in `entry.data`
                # unchanged, the same way `services._async_import_config`
                # carries it for an import onto an *existing* entry. The
                # example config has none today, but a future one might.
                return self.async_create_entry(
                    title="Cover Logic",
                    data={"guards": list(config.guards)},
                    subentries=subentries,
                )

        return self.async_show_form(
            step_id=_STEP_FROM_EXAMPLE,
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_example_not_available(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """The dead end `async_step_from_example` delegates to when there is no example to load.

        A real step of its own, reachable only from `async_step_from_example`
        -- never listed in the `user` step's own `menu_options` -- so Home
        Assistant's flow manager has a genuine `async_step_example_not_
        available` to dispatch a resubmission to; see the caller's own
        docstring for why that is not optional. On submit, returns to the
        main menu (`async_step_user` again) rather than aborting the flow
        outright, so picking this menu item on an install that cannot honour
        it costs nothing more than one extra screen.
        """
        if user_input is not None:
            return await self.async_step_user(None)
        return self.async_show_form(step_id="example_not_available", data_schema=vol.Schema({}))

    async def async_step_empty(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Start empty: a config entry with no subentries at all.

        `validate()` will report `no_fallback_mode` (there is not even a
        fallback mode yet) for the resulting empty `Config` -- again
        `ConfigEntryNotReady`, not a crash; see the module docstring's
        "whatever this creates must load, even incomplete" section, and
        `__init__.async_setup_entry`'s own docstring for the change that
        keeps *this* branch specifically (zero subentries, no
        `CONF_CONFIG_PATH`) from crashing outright.

        Still a form, not a direct `async_create_entry` off the menu choice
        itself: an empty schema still needs one submit, so a user reaches
        this deliberately rather than by a single misclick on the menu.
        """
        if user_input is not None:
            return self.async_create_entry(title="Cover Logic", data={}, subentries=[])
        return self.async_show_form(step_id=_STEP_EMPTY, data_schema=vol.Schema({}))

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return the subentry flows this integration supports."""
        return SUBENTRY_FLOW_HANDLERS

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow: the main menu over the same subentries.

        Imported at module level like anything else -- `options_flow.py`
        imports the six subentry-type classes and their shared machinery
        from `subentry_flow.py`, not from here, so there is no cycle left
        for a deferred import to work around. See this module's own
        docstring and `subentry_flow.py`'s for why that module, not either
        door, is where those classes now live.
        """
        return CoverLogicOptionsFlow()
