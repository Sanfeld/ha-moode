"""Actions (services) of the moOde integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .api import MoodeError
from .const import DOMAIN
from .stations import other_boxes, sync_stations

SYNC_SCHEMA = vol.Schema({
    vol.Required("source"): cv.entity_id,
    vol.Optional("targets"): cv.entity_ids,
    vol.Optional("mirror", default=True): cv.boolean,
})


def _entry_of(hass: HomeAssistant, entity_id: str) -> str:
    ent = er.async_get(hass).async_get(entity_id)
    if ent is None or ent.platform != DOMAIN or not ent.config_entry_id:
        raise HomeAssistantError(f"{entity_id} isn't a moOde player")
    return ent.config_entry_id


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def _sync(call: ServiceCall) -> ServiceResponse:
        source = _entry_of(hass, call.data["source"])
        targets = ([_entry_of(hass, t) for t in call.data["targets"]]
                   if call.data.get("targets") else other_boxes(hass, source))
        try:
            report = await sync_stations(hass, source, targets, call.data["mirror"])
        except (ValueError, MoodeError) as err:
            raise HomeAssistantError(f"Station sync failed: {err}") from err
        titles = {e.entry_id: e.title for e in hass.config_entries.async_entries(DOMAIN)}
        return {"boxes": {titles.get(k, k): v for k, v in report.items()}}

    hass.services.async_register(DOMAIN, "sync_stations", _sync, schema=SYNC_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
