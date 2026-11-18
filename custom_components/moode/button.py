"""One button per favourite station (handy for NSPanels and dashboards)."""
from __future__ import annotations

import re
from urllib.parse import quote

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MoodeConfigEntry
from .api import MoodeError
from .browse import async_play
from .const import CONF_FAV_WAKE_ZONE, CONF_FAVOURITES, opt
from .entity import MoodeEntity
from .select import station_name
from .zones import remove_stale_entities, wake_zone_if_silent


async def async_setup_entry(
    hass: HomeAssistant, entry: MoodeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    buttons = [FavouriteButton(entry.runtime_data, entry, mid) for mid in entry.options.get(CONF_FAVOURITES, [])]
    async_add_entities(buttons)
    box = entry.unique_id or entry.entry_id
    remove_stale_entities(hass, entry, "button", f"{box}_fav_", {b.unique_id for b in buttons})


class FavouriteButton(MoodeEntity, ButtonEntity):
    _attr_icon = "mdi:radio"

    def __init__(self, coordinator, entry: MoodeConfigEntry, media_id: str) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._entry = entry
        self._media_id = media_id
        name = station_name(media_id)
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        self._attr_translation_key = "play_favourite"
        self._attr_translation_placeholders = {"station": name}
        # the station's moOde logo, served through HA (works on https / remote access too)
        self._attr_entity_picture = f"/api/moode/station_picture/{entry.entry_id}/{quote(name, safe='')}"
        self._attr_unique_id = f"{self._box_id}_fav_{slug}"

    async def async_press(self) -> None:
        if opt(self._entry, CONF_FAV_WAKE_ZONE):
            await wake_zone_if_silent(self.hass, self._entry)
        try:
            await async_play(self.coordinator.client, self._media_id)
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        await self.coordinator.async_request_refresh()
