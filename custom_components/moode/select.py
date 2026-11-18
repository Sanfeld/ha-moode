"""Select entities: favourite station picker and sleep timer."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from . import MoodeConfigEntry
from .api import MoodeError
from .browse import async_play
from .const import CONF_AMPLIFIER, CONF_FAV_WAKE_ZONE, CONF_FAVOURITES, CONF_SLEEP_ACTION, CONF_SLEEP_FADE, CONF_SLEEP_FADE_SECONDS, opt
from .entity import MoodeEntity
from .zones import loaded, remove_stale_entities, sender_for, wake_zone_if_silent, zone_off

_LOGGER = logging.getLogger(__name__)

SLEEP_OPTIONS = {"Off": 0, "15 min": 15, "30 min": 30, "45 min": 45, "60 min": 60, "90 min": 90, "120 min": 120}


def station_name(media_id: str) -> str:
    return media_id.split(":", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[0]


async def async_setup_entry(
    hass: HomeAssistant, entry: MoodeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    ents: list[SelectEntity] = [
        SleepTimerSelect(coord, entry),
        RoomModeSelect(coord, entry),
        RoomStationSelect(coord, entry),
        AllStationsSelect(coord, entry),
        RecentSelect(coord, entry),
    ]
    if entry.options.get(CONF_AMPLIFIER):
        ents.append(AmpInputSelect(coord, entry))
    box = entry.unique_id or entry.entry_id
    if not entry.options.get(CONF_AMPLIFIER):
        remove_stale_entities(hass, entry, "select", f"{box}_amp_input", set())
    if not entry.options.get(CONF_FAVOURITES):
        remove_stale_entities(hass, entry, "select", f"{box}_favourite", set())
    if entry.options.get(CONF_FAVOURITES):
        ents.append(FavouriteSelect(coord, entry))
    async_add_entities(ents)


class FavouriteSelect(MoodeEntity, SelectEntity):
    """Pick a favourite station -> it starts playing (on the zone, if this is a sender)."""

    _attr_translation_key = "favourite"
    _attr_icon = "mdi:radio"

    def __init__(self, coordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._entry = entry
        self._attr_unique_id = f"{self._box_id}_favourite"
        self._by_name = {station_name(m): m for m in entry.options.get(CONF_FAVOURITES, [])}
        self._attr_options = list(self._by_name)

    @property
    def current_option(self) -> str | None:
        d = self.coordinator.data
        if not d or not d.file:
            return None
        for name, mid in self._by_name.items():
            path = mid.split(":", 1)[1]
            if d.file == path or station_name(d.file) == name or (d.station and d.station == name):
                return name
        return None

    async def async_select_option(self, option: str) -> None:
        if opt(self._entry, CONF_FAV_WAKE_ZONE):
            await wake_zone_if_silent(self.hass, self._entry)
        try:
            await async_play(self.coordinator.client, self._by_name[option])
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        await self.coordinator.async_request_refresh()


class SleepTimerSelect(MoodeEntity, SelectEntity):
    """Sleep timer. Fades out (if volume isn't fixed) then stops / turns the zone off."""

    _attr_translation_key = "sleep_timer"
    _attr_icon = "mdi:timer-sand"
    _attr_options = list(SLEEP_OPTIONS)

    def __init__(self, coordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._entry = entry
        self._attr_unique_id = f"{self._box_id}_sleep_timer"
        self._choice = "Off"
        self._ends_at = None
        self._cancel = None
        self._fade_task: asyncio.Task | None = None

    @property
    def current_option(self) -> str:
        return self._choice

    @property
    def extra_state_attributes(self):
        return {"ends_at": self._ends_at.isoformat() if self._ends_at else None}

    def _clear(self) -> None:
        if self._cancel:
            self._cancel()
            self._cancel = None
        if self._fade_task and not self._fade_task.done():
            self._fade_task.cancel()
        self._choice, self._ends_at = "Off", None

    async def async_will_remove_from_hass(self) -> None:
        self._clear()

    async def async_select_option(self, option: str) -> None:
        self._clear()
        minutes = SLEEP_OPTIONS[option]
        if minutes:
            self._choice = option
            self._ends_at = dt_util.utcnow() + timedelta(minutes=minutes)
            self._cancel = async_call_later(self.hass, minutes * 60, self._fire)
        self.async_write_ha_state()

    @callback
    def _fire(self, _now) -> None:
        self._cancel = None
        self._fade_task = self.hass.async_create_task(self._sleep_now())

    async def _sleep_now(self) -> None:
        client = self.coordinator.client
        d = self.coordinator.data
        restore = None
        try:
            if opt(self._entry, CONF_SLEEP_FADE) and d and not d.fixed_volume and d.volume:
                restore = d.volume
                secs = int(opt(self._entry, CONF_SLEEP_FADE_SECONDS))
                steps = max(1, min(secs, restore))
                for i in range(1, steps + 1):
                    await client.set_volume(round(restore * (1 - i / steps)))
                    await asyncio.sleep(secs / steps)

            if sender_for(self.hass, self._entry.entry_id) is not None:
                await client.set_receiver(False)  # a receiver just leaves the zone
            elif opt(self._entry, CONF_SLEEP_ACTION) == "zone_off":
                await zone_off(self.hass, self._entry)
            else:
                await client.stop()
        except (MoodeError, asyncio.CancelledError) as err:
            _LOGGER.debug("Sleep timer on %s: %s", self._entry.title, err)
        finally:
            if restore is not None:
                try:
                    await client.set_volume(restore)  # ready for next time
                except MoodeError:
                    pass
            self._choice, self._ends_at = "Off", None
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()


# ------------------------------------------------------------------ rooms (receivers)

MODE_ZONE, MODE_SOLO, MODE_OFF = "Zone", "Solo", "Off"


class _RoomBase(MoodeEntity, SelectEntity):
    """Only available while this box is a receiver in some zone."""

    def __init__(self, coordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._entry = entry

    def _sender(self):
        return sender_for(self.hass, self._entry.entry_id) if self.hass else None

    @property
    def available(self) -> bool:
        d = self.coordinator.data
        return (
            super().available
            and self._sender() is not None
            and bool(d and d.receiver and d.receiver.enabled)
        )

    def _mode(self) -> str:
        d = self.coordinator.data
        if d and d.receiver and d.receiver.state == "On":
            return MODE_ZONE
        if d and d.state in ("play", "pause"):
            return MODE_SOLO
        return MODE_OFF

    async def _resolve_station(self, media_id: str) -> str:
        """Favourite media_id (a .pls on the sender) -> playable URL for this room."""
        path = media_id.split(":", 1)[-1]
        sender = loaded(self._sender())
        if sender is not None and path.lower().endswith((".pls", ".m3u")):
            try:
                url = await sender.client.stream_url(path)
            except MoodeError as err:
                _LOGGER.warning("Couldn't read %s from %s: %s", path, self._sender().title, err)
                url = None
            if url:
                _LOGGER.debug("%s: %s -> %s", self._entry.title, path, url)
                return url
            _LOGGER.warning("No stream URL found in %s on the sender; trying the path on %s",
                            path, self._entry.title)
        return path

    async def _go_solo(self, item: str) -> None:
        client = self.coordinator.client
        d = self.coordinator.data
        if d and d.receiver and d.receiver.state == "On":
            await client.set_receiver(False)
        if item.startswith(("http://", "https://")):
            await client.play_url(item)
        else:
            await client.play_item(item)
        # moOde/MPD can accept a request and then not (keep) playing: check, and say why.
        started = False
        for _ in range(16):
            await asyncio.sleep(0.5)
            playing = await client.is_playing()
            started = started or playing
            if started and not playing:
                break
        else:
            if started:
                return  # still playing after 8 s: good
        err = await client.mpd_error()
        _LOGGER.warning("%s: couldn't play %s (MPD error: %s)", self._entry.title, item, err)
        raise MoodeError(
            f"{self._entry.title} couldn't play the station"
            + (f": {err}" if err else " (it stopped right away)")
            + f" [{item}]"
        )

    async def _wrap(self, coro) -> None:
        try:
            await coro
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        await self.coordinator.async_request_refresh()


class RoomModeSelect(_RoomBase):
    """Zone = follow the zone. Solo = play on its own. Off = silent."""

    _attr_translation_key = "room_mode"
    _attr_icon = "mdi:speaker-multiple"
    _attr_options = [MODE_ZONE, MODE_SOLO, MODE_OFF]

    def __init__(self, coordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._box_id}_room_mode"

    @property
    def current_option(self) -> str:
        return self._mode()

    async def async_select_option(self, option: str) -> None:
        client = self.coordinator.client
        if option == MODE_ZONE:
            async def go():
                await client.stop()            # no local music on top of the zone
                await client.set_receiver(True)
            await self._wrap(go())
        elif option == MODE_OFF:
            async def go():
                await client.set_receiver(False)
                await client.stop()
            await self._wrap(go())
        else:  # Solo: take whatever the zone is playing and continue it here, on its own
            sender = loaded(self._sender())
            sd = sender.data if sender else None
            if sd and sd.file and sd.file.startswith(("http://", "https://")):
                await self._wrap(self._go_solo(sd.file))
            elif sd and sd.file:
                await self._wrap(self._go_solo(sd.file))
            else:
                await self._wrap(client.set_receiver(False))


class RoomStationSelect(_RoomBase):
    """Pick a favourite (from the zone sender's list): this room plays it solo."""

    _attr_translation_key = "room_station"
    _attr_icon = "mdi:radio"

    def __init__(self, coordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._box_id}_room_station"
        self._chosen: str | None = None

    def _favs(self) -> dict[str, str]:
        s = self._sender()
        return {station_name(m): m for m in (s.options.get(CONF_FAVOURITES, []) if s else [])}

    @property
    def options(self) -> list[str]:
        return list(self._favs()) or ["(no favourites)"]

    @property
    def current_option(self) -> str | None:
        return self._chosen if self._mode() == MODE_SOLO and self._chosen in self._favs() else None

    async def async_select_option(self, option: str) -> None:
        favs = self._favs()
        if option not in favs:
            raise HomeAssistantError("Set favourite stations on the zone's sender first.")
        url = await self._resolve_station(favs[option])
        self._chosen = option
        await self._wrap(self._go_solo(url))


# ------------------------------------------------------------------ all stations


class AllStationsSelect(MoodeEntity, SelectEntity):
    """Every station in this box's moOde radio list. Not offered on zone receivers
    (they use Room station instead), so a zone never gets double audio."""

    _attr_translation_key = "all_stations"
    _attr_icon = "mdi:radio-tower"

    def __init__(self, coordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._entry = entry
        self._attr_unique_id = f"{self._box_id}_all_stations"
        self._stations: dict[str, str] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._reload()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._reload, timedelta(minutes=10))
        )

    async def _reload(self, _now=None) -> None:
        try:
            items = await self.coordinator.client.lsinfo("RADIO")
        except MoodeError as err:
            _LOGGER.debug("%s: couldn't list stations: %s", self._entry.title, err)
            return
        stations = {}
        for i in items:
            if i.kind != "directory":
                stations[i.title.rsplit(".", 1)[0]] = f"file:{i.path}"
        self._stations = dict(sorted(stations.items(), key=lambda kv: kv[0].casefold()))
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return (
            super().available
            and bool(self._stations)
            and sender_for(self.hass, self._entry.entry_id) is None
        )

    @property
    def options(self) -> list[str]:
        return list(self._stations) or ["(no stations)"]

    @property
    def current_option(self) -> str | None:
        d = self.coordinator.data
        if not d or not d.file:
            return None
        for name, mid in self._stations.items():
            if d.file == mid[5:] or (d.station and d.station == name):
                return name
        return None

    async def async_select_option(self, option: str) -> None:
        if option not in self._stations:
            raise HomeAssistantError(f"Unknown station: {option}")
        if opt(self._entry, CONF_FAV_WAKE_ZONE):
            await wake_zone_if_silent(self.hass, self._entry)
        try:
            await async_play(self.coordinator.client, self._stations[option])
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        await self.coordinator.async_request_refresh()


# ------------------------------------------------------------------ amplifier input


class AmpInputSelect(MoodeEntity, SelectEntity):
    """Mirror of the linked amplifier's input list, so dashboards can show a plain dropdown."""

    _attr_translation_key = "amp_input"
    _attr_icon = "mdi:import"

    def __init__(self, coordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._amp = entry.options[CONF_AMPLIFIER]
        self._attr_unique_id = f"{self._box_id}_amp_input"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._amp], self._amp_changed)
        )

    @callback
    def _amp_changed(self, _event) -> None:
        self.async_write_ha_state()

    def _amp_state(self):
        return self.hass.states.get(self._amp) if self.hass else None

    @property
    def available(self) -> bool:
        s = self._amp_state()
        return bool(s and s.state not in ("unavailable", "unknown") and s.attributes.get("source_list"))

    @property
    def options(self) -> list[str]:
        s = self._amp_state()
        return list(s.attributes.get("source_list") or []) if s else []

    @property
    def current_option(self) -> str | None:
        s = self._amp_state()
        return s.attributes.get("source") if s else None

    @property
    def extra_state_attributes(self):
        return {"amplifier": self._amp}

    async def async_select_option(self, option: str) -> None:
        await self.hass.services.async_call(
            "media_player", "select_source", {"entity_id": self._amp, "source": option}, blocking=True
        )


# ------------------------------------------------------------------ recently played


class RecentSelect(MoodeEntity, SelectEntity):
    """The last stations this box played (kept across restarts). Not on zone receivers."""

    _attr_translation_key = "recent"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._entry = entry
        self._attr_unique_id = f"{self._box_id}_recent"

    @property
    def available(self) -> bool:
        return (
            super().available
            and bool(self.coordinator.recent)
            and sender_for(self.hass, self._entry.entry_id) is None
        )

    @property
    def options(self) -> list[str]:
        return [r["name"] for r in self.coordinator.recent] or ["(nothing yet)"]

    @property
    def current_option(self) -> str | None:
        d = self.coordinator.data
        if d and d.state == "play" and d.file:
            for r in self.coordinator.recent:
                if r["url"] == d.file:
                    return r["name"]
        return None

    async def async_select_option(self, option: str) -> None:
        item = next((r for r in self.coordinator.recent if r["name"] == option), None)
        if item is None:
            raise HomeAssistantError(f"Not in recently played: {option}")
        if opt(self._entry, CONF_FAV_WAKE_ZONE):
            await wake_zone_if_silent(self.hass, self._entry)
        client = self.coordinator.client
        try:
            # Prefer moOde's own station entry (keeps its name/logo); else the raw stream.
            names = {i.title.rsplit(".", 1)[0]: i.path for i in await client.lsinfo("RADIO") if i.kind != "directory"}
            if item["name"] in names:
                await client.play_item(names[item["name"]])
            else:
                await client.play_url(item["url"])
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        await self.coordinator.async_request_refresh()
