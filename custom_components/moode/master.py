"""The zone's master volume: one slider that sets every room playing in the zone.

moOde's own master volume needs the sender's volume to be adjustable and every receiver
to have a DAC with hardware volume. This one is kept by Home Assistant instead, so it
also works with a Fixed-volume sender, and a room without volume control of its own can
follow with its amplifier. Each room spreads the master's 1-100 % over its own range."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import MoodeError
from .const import CONF_AMPLIFIER, CONF_ZONE_FOLLOW, CONF_ZONE_MAX, CONF_ZONE_MIN, opt

_LOGGER = logging.getLogger(__name__)


def follow_mode(entry: ConfigEntry, coordinator) -> str:
    """'box', 'amp' or 'none' for this room."""
    mode = opt(entry, CONF_ZONE_FOLLOW)
    if mode == "auto":
        data = coordinator.data if coordinator else None
        return "box" if data is not None and not data.fixed_volume else "none"
    if mode == "amp" and not entry.options.get(CONF_AMPLIFIER):
        return "none"
    return mode


def room_level(entry: ConfigEntry, master: float) -> int:
    """The room's volume (0-100) for a master volume of 0.0-1.0."""
    if master <= 0:
        return 0
    lo, hi = int(opt(entry, CONF_ZONE_MIN)), int(opt(entry, CONF_ZONE_MAX))
    return max(0, min(100, round(lo + (hi - lo) * min(master, 1.0))))


async def apply_to_room(hass: HomeAssistant, entry: ConfigEntry, coordinator, master: float) -> None:
    """Set one room to the master volume, the way that room follows it."""
    mode = follow_mode(entry, coordinator)
    level = room_level(entry, master)
    try:
        if mode == "box" and coordinator is not None:
            await coordinator.client.set_volume(level)
            await coordinator.async_request_refresh()
        elif mode == "amp":
            amp = entry.options[CONF_AMPLIFIER]
            state = hass.states.get(amp)
            if state is None or state.state in ("off", "standby", "unavailable", "unknown"):
                return  # an amp that's off (or watching TV) is left alone
            await hass.services.async_call(
                "media_player", "volume_set", {"entity_id": amp, "volume_level": level / 100}, blocking=True
            )
    except (MoodeError, Exception) as err:  # noqa: BLE001 - one room failing mustn't stop the others
        _LOGGER.warning("%s: couldn't follow the master volume: %s", entry.title, err)
