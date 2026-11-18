"""Config flow: auto-discovered boxes (mDNS) plus manual add by IP/hostname."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

try:  # HA 2025.1+
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # pragma: no cover
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

from .api import MoodeClient, MoodeError
from .const import (
    CONF_AMPLIFIER,
    CONF_ANN_ALL_ROOMS,
    CONF_ANN_LEAD_IN,
    CONF_ANN_MAX,
    CONF_ANN_RESUME,
    CONF_FAV_WAKE_ZONE,
    CONF_VOLUME_MODE,
    CONF_FAVOURITES,
    CONF_RECEIVERS,
    CONF_SLEEP_ACTION,
    CONF_SLEEP_FADE,
    CONF_SLEEP_FADE_SECONDS,
    CONF_ZONE_NAME,
    DOMAIN,
    opt,
)
from .zones import loaded, receiver_ids, sender_for

_LOGGER = logging.getLogger(__name__)


def normalize_id(name: str) -> str:
    """'MoodeStreamer.local.' -> 'moodestreamer'."""
    name = name.strip().rstrip(".").lower()
    return name[: -len(".local")] if name.endswith(".local") else name


def pretty_name(box_id: str) -> str:
    return box_id.replace("-", " ").replace("_", " ").title() if not box_id[0].isdigit() else f"moOde {box_id}"


class MoodeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for moOde."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "MoodeOptionsFlow":
        return MoodeOptionsFlow()

    def __init__(self) -> None:
        self._host: str | None = None
        self._name: str | None = None

    async def _validate(self, host: str) -> str | None:
        client = MoodeClient(host, async_get_clientsession(self.hass))
        return await client.validate()

    # ---- manual ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            self._async_abort_entries_match({CONF_HOST: host})
            try:
                reported = await self._validate(host)
            except MoodeError as err:
                _LOGGER.debug("Validation of %s failed: %s", host, err)
                errors["base"] = "cannot_connect"
            else:
                box_id = normalize_id(reported or host)
                await self.async_set_unique_id(box_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                name = user_input.get(CONF_NAME) or pretty_name(box_id)
                return self.async_create_entry(title=name, data={CONF_HOST: host})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST): str, vol.Optional(CONF_NAME): str}
            ),
            errors=errors,
        )

    # ---- discovery ---------------------------------------------------------------

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        if discovery_info.ip_address.version == 6:
            return self.async_abort(reason="ipv6_not_supported")

        host = str(discovery_info.ip_address)
        box_id = normalize_id(discovery_info.hostname or host)

        # Same box seen again (any service type / interface) -> nothing to do.
        await self.async_set_unique_id(box_id)
        self._abort_if_unique_id_configured()
        self._async_abort_entries_match({CONF_HOST: host})

        try:
            reported = await self._validate(host)
        except MoodeError:
            return self.async_abort(reason="not_moode")

        if reported and normalize_id(reported) != box_id:
            box_id = normalize_id(reported)
            await self.async_set_unique_id(box_id)
            self._abort_if_unique_id_configured()

        self._host = host
        self._name = pretty_name(box_id)
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._host and self._name
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get(CONF_NAME) or self._name, data={CONF_HOST: self._host}
            )
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_NAME, default=self._name): str}),
            description_placeholders={"name": self._name, "host": self._host},
        )

    # ---- reconfigure (new IP etc.) ----------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                await self._validate(host)
            except MoodeError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data={CONF_HOST: host})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str}),
            errors=errors,
        )


# ---- zones (options) -------------------------------------------------------------



def _rx_state(entry: ConfigEntry) -> str | None:
    coord = loaded(entry)
    data = coord.data if coord else None
    return data.receiver.state if data and data.receiver else None


class MoodeOptionsFlow(OptionsFlow):
    """Settings menu: zone, favourites, sleep timer, announcements."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["zone", "favourites", "amplifier", "volume", "sleep", "announce"],
            description_placeholders={"box": self.config_entry.title},
        )

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    # ---- favourites ----------------------------------------------------------

    async def async_step_favourites(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self._save(**{
                CONF_FAVOURITES: user_input.get(CONF_FAVOURITES, []),
                CONF_FAV_WAKE_ZONE: user_input.get(CONF_FAV_WAKE_ZONE, True),
            })
        coord = loaded(entry)
        stations: list[SelectOptionDict] = []
        if coord:
            try:
                for i in await coord.client.lsinfo("RADIO"):
                    if i.kind != "directory":
                        stations.append(SelectOptionDict(
                            value=f"file:{i.path}", label=i.title.rsplit(".", 1)[0]))
            except MoodeError:
                pass
        if not stations:
            return self.async_abort(reason="no_stations")
        # Never silently drop a favourite: if moOde doesn't list it right now (mid-edit,
        # not rescanned yet, or deleted), keep it ticked and mark it so you can decide.
        current = list(entry.options.get(CONF_FAVOURITES, []))
        known = {o["value"] for o in stations}
        for fav in current:
            if fav not in known:
                name = fav.split(":", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[0]
                stations.append(SelectOptionDict(value=fav, label=f"{name} (not found on the box right now)"))
        return self.async_show_form(
            step_id="favourites",
            data_schema=vol.Schema({
                vol.Optional(CONF_FAVOURITES, default=current): SelectSelector(
                    SelectSelectorConfig(options=stations, multiple=True, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_FAV_WAKE_ZONE, default=opt(entry, CONF_FAV_WAKE_ZONE)): bool,
            }),
            description_placeholders={"box": entry.title},
        )

    # ---- sleep timer ---------------------------------------------------------

    async def async_step_sleep(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self._save(**user_input)
        return self.async_show_form(
            step_id="sleep",
            data_schema=vol.Schema({
                vol.Required(CONF_SLEEP_ACTION, default=opt(entry, CONF_SLEEP_ACTION)): SelectSelector(
                    SelectSelectorConfig(options=["stop", "zone_off"], translation_key="sleep_action")
                ),
                vol.Required(CONF_SLEEP_FADE, default=opt(entry, CONF_SLEEP_FADE)): bool,
                vol.Required(CONF_SLEEP_FADE_SECONDS, default=opt(entry, CONF_SLEEP_FADE_SECONDS)):
                    vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
            }),
        )

    # ---- amplifier ------------------------------------------------------------

    async def async_step_amplifier(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self._save(**{CONF_AMPLIFIER: user_input.get(CONF_AMPLIFIER) or None})
        return self.async_show_form(
            step_id="amplifier",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_AMPLIFIER,
                    description={"suggested_value": entry.options.get(CONF_AMPLIFIER)},
                ): EntitySelector(EntitySelectorConfig(domain="media_player")),
            }),
            description_placeholders={"box": entry.title},
        )

    # ---- volume ---------------------------------------------------------------

    async def async_step_volume(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self._save(**user_input)
        return self.async_show_form(
            step_id="volume",
            data_schema=vol.Schema({
                vol.Required(CONF_VOLUME_MODE, default=opt(entry, CONF_VOLUME_MODE)): SelectSelector(
                    SelectSelectorConfig(options=["auto", "fixed", "variable"], translation_key="volume_mode")
                ),
            }),
        )

    # ---- announcements -------------------------------------------------------

    async def async_step_announce(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self._save(**user_input)
        return self.async_show_form(
            step_id="announce",
            data_schema=vol.Schema({
                vol.Required(CONF_ANN_RESUME, default=opt(entry, CONF_ANN_RESUME)): bool,
                vol.Required(CONF_ANN_ALL_ROOMS, default=opt(entry, CONF_ANN_ALL_ROOMS)): bool,
                vol.Required(CONF_ANN_LEAD_IN, default=opt(entry, CONF_ANN_LEAD_IN)):
                    vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
                vol.Required(CONF_ANN_MAX, default=opt(entry, CONF_ANN_MAX)):
                    vol.All(vol.Coerce(int), vol.Range(min=5, max=600)),
            }),
        )

    # ---- zone ----------------------------------------------------------------

    async def async_step_zone(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        all_entries = self.hass.config_entries.async_entries(DOMAIN)
        others = [e for e in all_entries if e.entry_id != entry.entry_id]
        errors: dict[str, str] = {}
        placeholders = {"box": entry.title, "conflict": ""}

        if user_input is not None:
            chosen: list[str] = user_input.get(CONF_RECEIVERS, [])
            if chosen:
                if (s := sender_for(self.hass, entry.entry_id)) is not None:
                    errors["base"] = "is_receiver_in_zone"
                    placeholders["conflict"] = s.title
                elif _rx_state(entry) in ("On", "Off"):
                    errors["base"] = "box_is_receiver"
                for e in others:
                    taken = set(chosen) & set(receiver_ids(e))
                    if taken and not errors:
                        errors["base"] = "already_in_zone"
                        placeholders["conflict"] = e.title
                for rid in chosen:
                    r = self.hass.config_entries.async_get_entry(rid)
                    if r and _rx_state(r) == "Disabled" and not errors:
                        errors["base"] = "not_receiver"
                        placeholders["conflict"] = r.title
            if not errors:
                return self._save(**{
                    CONF_ZONE_NAME: (user_input.get(CONF_ZONE_NAME) or "").strip(),
                    CONF_RECEIVERS: chosen,
                })

        options = [SelectOptionDict(value=e.entry_id, label=e.title) for e in others]
        current = entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ZONE_NAME, default=current.get(CONF_ZONE_NAME, f"{entry.title} zone")
                ): str,
                vol.Optional(CONF_RECEIVERS, default=current.get(CONF_RECEIVERS, [])): SelectSelector(
                    SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
                ),
            }
        )
        return self.async_show_form(
            step_id="zone", data_schema=schema, errors=errors, description_placeholders=placeholders
        )

