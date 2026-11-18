"""Coordinator for one moOde box: state, instant updates, recently played, health checks."""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MoodeClient, MoodeError, MoodeState
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

RECENT_MAX = 10
OFFLINE_AFTER = 120  # seconds unreachable before a Repairs warning
NET_ERROR = re.compile(r"(failed to connect|could ?n[o']t resolve|could not connect|timed out|connection refused)", re.I)


class MoodeCoordinator(DataUpdateCoordinator[MoodeState]):
    """Polls a box every SCAN_INTERVAL and refreshes instantly when MPD reports a change."""

    def __init__(
        self, hass: HomeAssistant, client: MoodeClient, entry: ConfigEntry, volume_mode: str = "auto"
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.title}",
            update_interval=SCAN_INTERVAL,
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=0.4, immediate=True),
        )
        self.client = client
        self.entry = entry
        self.volume_mode = volume_mode
        self.recent: list[dict[str, str]] = []  # [{"name":..., "url":...}], newest first
        self._store: Store = Store(hass, 1, f"{DOMAIN}.recent.{entry.entry_id}")
        self._fail_since: float | None = None
        self._last_url: str | None = None

    # ---- lifecycle -------------------------------------------------------------

    async def async_load_recent(self) -> None:
        data = await self._store.async_load() or {}
        self.recent = [
            r for r in data.get("recent", [])
            if r.get("url") and "unknown station" not in str(r.get("name", "")).lower()
        ][:RECENT_MAX]

    def start_push(self) -> None:
        """Start the MPD idle listener; it's cancelled automatically on unload."""
        self.entry.async_create_background_task(
            self.hass, self.client.idle_forever(self._mpd_changed), f"{DOMAIN} idle {self.entry.title}"
        )

    @callback
    def _mpd_changed(self, _subsystems: set[str]) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    # ---- polling ---------------------------------------------------------------

    async def _async_update_data(self) -> MoodeState:
        try:
            st = await self.client.fetch_state()
        except MoodeError as err:
            self._fail_since = self._fail_since or time.monotonic()
            if time.monotonic() - self._fail_since > OFFLINE_AFTER:
                self._issue("box_offline", host=self.client.host)
            raise UpdateFailed(str(err)) from err
        self._fail_since = None
        self._clear("box_offline")

        if self.volume_mode == "fixed":
            st.fixed_volume = True
        elif self.volume_mode == "variable":
            st.fixed_volume = False

        await self._remember(st)
        self._check_health(st)
        return st

    # ---- recently played -------------------------------------------------------

    async def _remember(self, st: MoodeState) -> None:
        if st.state != "play" or not st.is_radio or not st.file or st.file == self._last_url:
            return
        self._last_url = st.file
        name = await self._station_name(st)
        if not name:
            return  # don't store streams we can't name
        self.recent = [{"name": name, "url": st.file}] + [
            r for r in self.recent if r["url"] != st.file and r["name"] != name
        ]
        self.recent = self.recent[:RECENT_MAX]
        self._store.async_delay_save(lambda: {"recent": self.recent}, 5)

    async def _station_name(self, st: MoodeState) -> str | None:
        """The station's name in moOde, found by its stream URL. moOde shows streams it
        doesn't know as 'Unknown station', so never trust that label."""
        try:
            for row in await self.client.get_stations(attempts=1):
                if row.get("station") == st.file:
                    return row["name"]
        except MoodeError:
            pass
        for cand in (st.station, st.album):
            if cand and cand.strip() and "unknown station" not in cand.lower():
                return cand.strip()
        return None

    # ---- health / Repairs ------------------------------------------------------

    def _check_health(self, st: MoodeState) -> None:
        err = st.mpd_error or ""
        if "http" in err.lower() and NET_ERROR.search(err):
            self._issue("no_internet", host=self.client.host)
        elif st.state == "play" and st.is_radio:
            self._clear("no_internet")

        from .zones import sender_for  # local import: zones imports this module's type

        sender = sender_for(self.hass, self.entry.entry_id)
        if sender is not None and st.receiver and st.receiver.state == "Disabled":
            self._issue("receiver_disabled", zone=sender.title)
        else:
            self._clear("receiver_disabled")

    def _issue(self, key: str, **placeholders: Any) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{key}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders={"box": self.entry.title, **placeholders},
        )

    def _clear(self, key: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{self.entry.entry_id}")

    def clear_all_issues(self) -> None:
        for key in ("box_offline", "no_internet", "receiver_disabled"):
            self._clear(key)
