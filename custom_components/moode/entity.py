"""Shared base entity."""
from __future__ import annotations

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
