"""Zone helpers: a zone is a sender entry plus the receiver entries chosen in its options."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_RECEIVERS, DOMAIN

if TYPE_CHECKING:
    from .coordinator import MoodeCoordinator


def loaded(entry: ConfigEntry | None) -> MoodeCoordinator | None:
    if entry and entry.state is ConfigEntryState.LOADED:
        return getattr(entry, "runtime_data", None)
    return None


def receiver_ids(entry: ConfigEntry) -> list[str]:
    return list(entry.options.get(CONF_RECEIVERS, []))


def sender_for(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    for e in hass.config_entries.async_entries(DOMAIN):
        if entry_id in receiver_ids(e):
            return e
    return None


def receivers_of(hass: HomeAssistant, entry: ConfigEntry) -> list[ConfigEntry]:
    out = []
    for rid in receiver_ids(entry):
        if (e := hass.config_entries.async_get_entry(rid)) is not None:
            out.append(e)
    return out


def player_entity_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        if ent.domain == "media_player" and ent.unique_id.endswith("_player"):
            return ent.entity_id
    return None


def entry_for_entity(hass: HomeAssistant, entity_id: str) -> ConfigEntry | None:
    ent = er.async_get(hass).async_get(entity_id)
    if ent and ent.platform == DOMAIN and ent.config_entry_id:
        return hass.config_entries.async_get_entry(ent.config_entry_id)
    return None


async def zone_off(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Silence the whole zone: receivers off, rooms playing Solo stopped, sender stopped."""
    for r in receivers_of(hass, entry):
        if (c := loaded(r)) is not None:
            for step in (c.client.set_receiver(False), c.client.stop()):
                try:
                    await step
                except Exception:  # noqa: BLE001 - an offline room mustn't block the rest
                    pass
    if (c := loaded(entry)) is not None:
        await c.client.stop()


async def wake_zone_if_silent(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """If this is a sender and no room is listening (none in Zone, none Solo), switch all rooms on."""
    rooms = [c for r in receivers_of(hass, entry) if (c := loaded(r)) is not None]
    if not rooms:
        return
    for c in rooms:
        d = c.data
        if d and ((d.receiver and d.receiver.state == "On") or d.state in ("play", "pause")):
            return  # someone is listening already: don't touch the rooms
    for c in rooms:
        try:
            await c.client.set_receiver(True)
        except Exception:  # noqa: BLE001
            pass
        await c.async_request_refresh()


def remove_stale_entities(hass: HomeAssistant, entry: ConfigEntry, domain: str, prefix: str, keep: set[str]) -> None:
    """Delete this box's own entities that it no longer provides (e.g. a favourite you
    removed, or an amplifier you unlinked). Only touches this entry's moOde entities."""
    reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        if ent.domain == domain and ent.unique_id.startswith(prefix) and ent.unique_id not in keep:
            reg.async_remove(ent.entity_id)
