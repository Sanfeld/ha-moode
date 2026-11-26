"""Actions (services) of the moOde integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service

from .api import MoodeError
from .const import DOMAIN
from .provision import apply, discover_receivers, join_zone, plan_receiver, plan_sender
from .stations import other_boxes, sync_stations
from .zones import loaded, receivers_of

SYNC_SCHEMA = vol.Schema({
    vol.Required("source"): cv.entity_id,
    vol.Optional("targets"): cv.entity_ids,
    vol.Optional("mirror", default=True): cv.boolean,
})


PROVISION_SCHEMA = vol.Schema({
    vol.Required("box"): cv.entity_id,
    vol.Required("role"): vol.In(["receiver", "sender"]),
    vol.Optional("sender"): cv.entity_id,
    vol.Optional("copy_from"): cv.entity_id,
    vol.Optional("dry_run", default=False): cv.boolean,
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

    # Both change the boxes' configuration, so (like the Stations panel) admin only.
    # Automations and scripts are not affected: calls without a user are always allowed.
    async_register_admin_service(hass, DOMAIN, "sync_stations", _sync, SYNC_SCHEMA,
                                 SupportsResponse.OPTIONAL)

    def _client(entry_id: str):
        entry = hass.config_entries.async_get_entry(entry_id)
        if (c := loaded(entry)) is None:
            raise HomeAssistantError(f"{entry.title if entry else entry_id} isn't connected right now")
        return entry, c.client

    async def _provision(call: ServiceCall) -> ServiceResponse:
        box, client = _client(_entry_of(hass, call.data["box"]))
        dry = call.data["dry_run"]
        try:
            if call.data["role"] == "sender":
                plan = await plan_sender(client)
                sender = None
            else:
                if not call.data.get("sender"):
                    raise HomeAssistantError("Choose the sender (the zone) this box should join")
                sender, sender_client = _client(_entry_of(hass, call.data["sender"]))
                if call.data.get("copy_from"):
                    _, template = _client(_entry_of(hass, call.data["copy_from"]))
                else:  # a room that already works in this zone
                    template = next((c.client for r in receivers_of(hass, sender)
                                     if r.entry_id != box.entry_id and (c := loaded(r)) is not None), None)
                plan = await plan_receiver(client, sender_client, template)
            done = [s.label for s in plan.steps] if dry else await apply(client, plan)
            if sender is not None and not dry:
                if plan.steps:
                    await discover_receivers(sender_client)
                join_zone(hass, sender, box.entry_id)
        except MoodeError as err:
            raise HomeAssistantError(f"Multiroom setup of {box.title} failed: {err}") from err
        return {"box": box.title, "role": call.data["role"], "dry_run": dry,
                "changes": done or ["Nothing to change: already set up"], "notes": plan.notes}

    async_register_admin_service(hass, DOMAIN, "provision_multiroom", _provision, PROVISION_SCHEMA,
                                 SupportsResponse.OPTIONAL)
