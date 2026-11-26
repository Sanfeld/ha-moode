"""Shared base entity."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MoodeCoordinator


class MoodeEntity(CoordinatorEntity[MoodeCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MoodeCoordinator, entry_id: str, unique_id: str, title: str) -> None:
        super().__init__(coordinator)
        self._box_id = unique_id or entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._box_id)},
            name=title,
            manufacturer=MANUFACTURER,
            model="moOde audio player",
            sw_version=getattr(coordinator, "release", None),
            configuration_url=coordinator.client.base_url,
        )
        if model := getattr(coordinator, "pi_model", None):
            self._attr_device_info["hw_version"] = model


_PI_MODELS: dict[str, str] = {}  # entry_id -> Pi model; it doesn't change, so read it once


@callback
def show_pi_model(hass: HomeAssistant, entry: ConfigEntry, coordinator: MoodeCoordinator) -> None:
    """Put which Raspberry Pi this is on the device page. moOde takes a few seconds to build
    its System info page, so it's read in the background, once per HA start."""
    if model := _PI_MODELS.get(entry.entry_id):
        coordinator.pi_model = model
        return

    async def _read() -> None:
        if model := await coordinator.client.get_pi_model():
            _PI_MODELS[entry.entry_id] = coordinator.pi_model = model
            reg = dr.async_get(hass)
            if dev := reg.async_get_device(identifiers={(DOMAIN, entry.unique_id or entry.entry_id)}):
                reg.async_update_device(dev.id, hw_version=model)

    entry.async_create_background_task(hass, _read(), f"moode pi model {entry.entry_id}")
