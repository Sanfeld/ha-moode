"""The moOde Audio integration."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .api import MoodeClient
from .const import CONF_VOLUME_MODE, DOMAIN, LIBRARY_URL, PANEL_URL, PLATFORMS, STATIC_URL, STRATEGY_URL
from .coordinator import MoodeCoordinator
from .library import async_setup_library
from .stations import async_setup_stations
from .zones import role

type MoodeConfigEntry = ConfigEntry[MoodeCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Frontend pieces: the self-building dashboard and the Stations manager card."""
    folder = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(folder), False)])
    version = (await async_get_integration(hass, DOMAIN)).version
    add_extra_js_url(hass, f"{STRATEGY_URL}?v={version}")

    # Stations manager: websocket API + the card the dashboard's Stations tab uses
    async_setup_stations(hass)
    add_extra_js_url(hass, f"{PANEL_URL}?v={version}")

    # Music library (FLACs tab): websocket API, cover art proxy and its card
    async_setup_library(hass)
    add_extra_js_url(hass, f"{LIBRARY_URL}?v={version}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MoodeConfigEntry) -> bool:
    client = MoodeClient(entry.data[CONF_HOST], async_get_clientsession(hass))
    coordinator = MoodeCoordinator(hass, client, entry, entry.options.get(CONF_VOLUME_MODE, "auto"))
    await coordinator.async_load_recent()
    await coordinator.async_config_entry_first_refresh()
    coordinator.start_push()
    entry.runtime_data = coordinator
    coordinator.role = role(hass, entry)  # which controls this box got (see select/switch)
    coordinator.release = await client.get_release()  # shown on the device page
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _check_release(_now=None) -> None:
        """moOde updates in place: show the new version without a restart."""
        rel = await client.get_release()
        if rel and rel != coordinator.release:
            coordinator.release = rel
            reg = dr.async_get(hass)
            if dev := reg.async_get_device(identifiers={(DOMAIN, entry.unique_id or entry.entry_id)}):
                reg.async_update_device(dev.id, sw_version=rel)

    entry.async_on_unload(async_track_time_interval(hass, _check_release, timedelta(hours=6)))
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: MoodeConfigEntry) -> None:
    """Options changed: reload this box (zone player, favourites...), and every other box
    whose role changed with it (a room added to or taken out of a zone gets or loses its
    room controls)."""
    await hass.config_entries.async_reload(entry.entry_id)
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id or other.state is not ConfigEntryState.LOADED:
            continue
        if getattr(other.runtime_data, "role", None) != role(hass, other):
            await hass.config_entries.async_reload(other.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MoodeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: MoodeConfigEntry) -> None:
    """Box removed from HA: drop its Repairs warnings and saved history."""
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.storage import Store

    for key in ("box_offline", "no_internet", "receiver_disabled"):
        ir.async_delete_issue(hass, DOMAIN, f"{key}_{entry.entry_id}")
    await Store(hass, 1, f"{DOMAIN}.recent.{entry.entry_id}").async_remove()
