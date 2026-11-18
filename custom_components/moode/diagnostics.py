"""Diagnostics download (Settings → Devices & services → moOde Audio → ⋮ → Download diagnostics)."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MoodeConfigEntry

REDACT = {"host"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: MoodeConfigEntry) -> dict[str, Any]:
    coord = entry.runtime_data
    state = asdict(coord.data) if coord.data else None
    try:
        station_count = len(await coord.client.get_stations(attempts=1))
    except Exception as err:  # noqa: BLE001 - diagnostics must never fail
        station_count = f"unavailable ({err})"
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), REDACT),
            "options": dict(entry.options),
        },
        "last_update_success": coord.last_update_success,
        "update_interval_s": coord.update_interval.total_seconds() if coord.update_interval else None,
        "volume_mode": coord.volume_mode,
        "state": state,
        "recently_played": coord.recent,
        "stations_on_box": station_count,
    }
