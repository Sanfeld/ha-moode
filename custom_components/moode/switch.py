"""Multiroom receiver on/off switch."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MoodeConfigEntry
from .api import MoodeError
from .entity import MoodeEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MoodeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([MoodeReceiverSwitch(entry.runtime_data, entry.entry_id, entry.unique_id, entry.title)])


class MoodeReceiverSwitch(MoodeEntity, SwitchEntity):
    """On/off for moOde's multiroom receiver. Unavailable when the box isn't set up as a receiver."""

    _attr_translation_key = "receiver"
    _attr_icon = "mdi:speaker-wireless"

    def __init__(self, coordinator, entry_id, unique_id, title) -> None:
        super().__init__(coordinator, entry_id, unique_id, title)
        self._attr_unique_id = f"{self._box_id}_receiver"

    @property
    def available(self) -> bool:
        rx = self.coordinator.data.receiver if self.coordinator.data else None
        return super().available and bool(rx and rx.enabled)

    @property
    def is_on(self) -> bool:
        rx = self.coordinator.data.receiver if self.coordinator.data else None
        return bool(rx and rx.state == "On")

    async def _set(self, on: bool) -> None:
        try:
            d = self.coordinator.data
            if on and d and d.state in ("play", "pause"):
                await self.coordinator.client.stop()  # leaving Solo: no double audio
            await self.coordinator.client.set_receiver(on)
        except MoodeError as err:
            raise HomeAssistantError(f"moOde receiver: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
