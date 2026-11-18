"""Media players: one per moOde box, plus one zone player per sender with receivers."""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.components import media_source
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import MoodeConfigEntry
from .api import MoodeError, MoodeState
from .browse import async_browse, async_play, async_search
from .const import (
    CONF_AMPLIFIER,
    CONF_ANN_ALL_ROOMS,
    CONF_ANN_LEAD_IN,
    CONF_ANN_MAX,
    CONF_ANN_RESUME,
    CONF_FAVOURITES,
    CONF_ZONE_NAME,
    DOMAIN,
    MANUFACTURER,
    opt,
)
from .coordinator import MoodeCoordinator
from .entity import MoodeEntity
from .zones import entry_for_entity, loaded, player_entity_id, receivers_of, sender_for, zone_off

STATE_MAP = {
    "play": MediaPlayerState.PLAYING,
    "pause": MediaPlayerState.PAUSED,
    "stop": MediaPlayerState.IDLE,
}

TRANSPORT = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
)
try:  # HA 2025.2+: lets Assist "play <something>" search the library
    from homeassistant.components.media_player import SearchMedia, SearchMediaQuery

    TRANSPORT |= MediaPlayerEntityFeature.SEARCH_MEDIA
except ImportError:  # pragma: no cover
    SearchMedia = SearchMediaQuery = None
VOLUME_FEATURES = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
)
FEATURES = TRANSPORT | VOLUME_FEATURES | MediaPlayerEntityFeature.GROUPING


async def async_setup_entry(
    hass: HomeAssistant, entry: MoodeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    entities: list[MediaPlayerEntity] = [MoodeMediaPlayer(coord, entry)]
    if entry.options.get("receivers"):
        entities.append(MoodeZonePlayer(coord, entry))
    async_add_entities(entities)


# ------------------------------------------------------------------ shared playback view


class _Playback:
    """Read-only media properties taken from a MoodeState (own or sender's)."""

    _position_updated_at = None
    _last_elapsed = None

    def _src(self) -> MoodeState:  # overridden
        raise NotImplementedError

    def _track_position(self) -> None:
        d = self._src()
        if d and d.elapsed != self._last_elapsed:
            self._last_elapsed = d.elapsed
            self._position_updated_at = dt_util.utcnow()

    @property
    def media_content_type(self):
        return MediaType.MUSIC

    @property
    def media_title(self):
        return self._src().title

    @property
    def media_artist(self):
        return self._src().artist

    @property
    def media_album_name(self):
        d = self._src()
        return d.station or d.album if d.is_radio else d.album

    @property
    def media_channel(self):
        d = self._src()
        return d.station if d.is_radio else None

    @property
    def media_image_url(self):
        return self._src().cover_url

    @property
    def media_duration(self):
        d = self._src()
        return None if d.is_radio or not d.duration else int(d.duration)

    @property
    def media_position(self):
        d = self._src()
        return None if d.is_radio or d.elapsed is None else int(d.elapsed)

    @property
    def media_position_updated_at(self):
        return self._position_updated_at

    @property
    def source(self):
        return self._src().renderer or "moOde"


# ------------------------------------------------------------------ play / announce


async def _resolve(hass: HomeAssistant, entity_id: str, media_id: str) -> str:
    """media-source:// (TTS, local media) -> URL the moOde box can fetch."""
    if media_source.is_media_source_id(media_id):
        item = await media_source.async_resolve_media(hass, media_id, entity_id)
        return async_process_play_media_url(hass, item.url)
    return media_id


async def play_or_announce(
    hass: HomeAssistant, entity_id: str, entry, coord: MoodeCoordinator, media_id: str, announce: bool
) -> None:
    if media_id.startswith("media-source://tts/"):
        announce = True  # speech never replaces the music; it interrupts and resumes
    media_id = await _resolve(hass, entity_id, media_id)
    if not announce:
        await async_play(coord.client, media_id)
        return

    switched_on: list[MoodeCoordinator] = []
    async with coord.client.announce_lock:
        try:
            if opt(entry, CONF_ANN_ALL_ROOMS):
                for r in receivers_of(hass, entry):
                    c = loaded(r)
                    if c and c.data and c.data.receiver and c.data.receiver.state == "Off":
                        await c.client.set_receiver(True)
                        switched_on.append(c)
            await coord.client.announce(
                media_id,
                max_seconds=float(opt(entry, CONF_ANN_MAX)),
                lead_in=float(opt(entry, CONF_ANN_LEAD_IN)) if receivers_of(hass, entry) else 0,
                resume=bool(opt(entry, CONF_ANN_RESUME)),
            )
        finally:
            for c in switched_on:
                try:
                    await c.client.set_receiver(False)
                except MoodeError:
                    pass
                await c.async_request_refresh()


# ------------------------------------------------------------------ per-box player


class MoodeMediaPlayer(_Playback, MoodeEntity, MediaPlayerEntity):
    _attr_name = None  # takes the device name
    _attr_volume_step = 0.02

    def __init__(self, coordinator: MoodeCoordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._entry = entry
        self._attr_unique_id = f"{self._box_id}_player"

    # -- zone relations ----------------------------------------------------

    def _sender_entry(self):
        return sender_for(self.hass, self._entry.entry_id)

    def _mirroring(self) -> MoodeCoordinator | None:
        """Sender coordinator if this box is an active receiver in a zone."""
        rx = self.coordinator.data.receiver if self.coordinator.data else None
        if not (rx and rx.state == "On"):
            return None
        coord = loaded(self._sender_entry())
        return coord if coord and coord.data else None

    def _src(self) -> MoodeState:
        m = self._mirroring()
        return m.data if m else self.coordinator.data

    def _target(self) -> MoodeCoordinator:
        """Where transport commands go: the sender when mirroring."""
        return self._mirroring() or self.coordinator

    @callback
    def _handle_coordinator_update(self) -> None:
        self._track_position()
        super()._handle_coordinator_update()

    # -- state ---------------------------------------------------------------

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        d = self.coordinator.data
        return FEATURES & ~VOLUME_FEATURES if d and d.fixed_volume else FEATURES

    @property
    def state(self) -> MediaPlayerState:
        return STATE_MAP.get(self._src().state, MediaPlayerState.IDLE)

    @property
    def volume_level(self):
        d = self.coordinator.data
        return None if d.volume is None or d.fixed_volume else d.volume / 100

    @property
    def is_volume_muted(self):
        d = self.coordinator.data
        return None if d.fixed_volume else d.muted

    @property
    def group_members(self) -> list[str] | None:
        """Sender first, then receivers that are switched on."""
        if self.hass is None:
            return None
        sender = self._sender_entry() or (self._entry if self._entry.options.get("receivers") else None)
        if sender is None:
            return None
        members = [player_entity_id(self.hass, sender)]
        for r in receivers_of(self.hass, sender):
            c = loaded(r)
            if c and c.data and c.data.receiver and c.data.receiver.state == "On":
                members.append(player_entity_id(self.hass, r))
        return [m for m in members if m]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        attrs: dict[str, Any] = {}
        if d.output_format:
            attrs["output_format"] = d.output_format
        if d.mpd_error:
            attrs["mpd_error"] = d.mpd_error
        if amp := self._entry.options.get(CONF_AMPLIFIER):
            attrs["amplifier"] = amp
        if d.receiver:
            attrs["multiroom_receiver"] = d.receiver.state
        if self._entry.options.get("receivers"):
            attrs["multiroom_role"] = "sender"
        elif self._sender_entry():
            attrs["multiroom_role"] = "receiver"
            attrs["zone_sender"] = self._sender_entry().title
        return attrs

    # -- commands ------------------------------------------------------------

    async def _run(self, coord: MoodeCoordinator, coro) -> None:
        try:
            await coro
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        await coord.async_request_refresh()

    async def async_media_play(self) -> None:
        t = self._target()
        await self._run(t, t.client.play())

    async def async_media_pause(self) -> None:
        t = self._target()
        await self._run(t, t.client.pause(t.data.is_radio))

    async def async_media_stop(self) -> None:
        t = self._target()
        await self._run(t, t.client.stop())

    async def async_media_next_track(self) -> None:
        t = self._target()
        await self._run(t, t.client.next())

    async def async_media_previous_track(self) -> None:
        t = self._target()
        await self._run(t, t.client.previous())

    async def async_media_seek(self, position: float) -> None:
        t = self._target()
        await self._run(t, t.client.seek(position))

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        """media_id: stream URL, moOde path (e.g. 'RADIO/Soma FM - Groove Salad.pls'),
        a library id from Browse media, or a media-source id (TTS, local media)."""
        t = self._target()
        target_entry = self._sender_entry() if t is not self.coordinator else self._entry
        await self._run(t, play_or_announce(
            self.hass, self.entity_id, target_entry, t, media_id, bool(kwargs.get("announce"))
        ))

    async def async_search_media(self, query):
        return SearchMedia(result=await async_search(self._target().client, query.search_query))

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        return await async_browse(self._target().client, media_content_id)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._run(self.coordinator, self.coordinator.client.set_volume(round(volume * 100)))

    async def async_volume_up(self) -> None:
        await self._run(self.coordinator, self.coordinator.client.volume_step(True))

    async def async_volume_down(self) -> None:
        await self._run(self.coordinator, self.coordinator.client.volume_step(False))

    async def async_mute_volume(self, mute: bool) -> None:
        if mute != self.coordinator.data.muted:  # moOde's mute is a toggle
            await self._run(self.coordinator, self.coordinator.client.toggle_mute())

    # -- grouping (join = receiver on, unjoin = receiver off) -----------------

    async def async_join_players(self, group_members: list[str]) -> None:
        sender = self._sender_entry() or self._entry
        allowed = {r.entry_id for r in receivers_of(self.hass, sender)}
        for eid in group_members:
            e = entry_for_entity(self.hass, eid)
            if e is None or e.entry_id == sender.entry_id:
                continue
            if e.entry_id not in allowed:
                raise HomeAssistantError(
                    f"{e.title} isn't a receiver in {sender.title}'s zone. Add it under the "
                    f"moOde Audio integration → {sender.title} → Configure."
                )
            c = loaded(e)
            if c:
                if c.data and c.data.state in ("play", "pause"):
                    await self._run(c, c.client.stop())
                await self._run(c, c.client.set_receiver(True))

    async def async_unjoin_player(self) -> None:
        if self._sender_entry() is None:
            raise HomeAssistantError("Only receivers can leave a zone; turn the zone off instead.")
        await self._run(self.coordinator, self.coordinator.client.set_receiver(False))


# ------------------------------------------------------------------ zone player


class MoodeZonePlayer(_Playback, MoodeEntity, MediaPlayerEntity):
    """Sender + its receivers as one player. On/off switches the receivers."""

    _attr_name = None

    def __init__(self, coordinator: MoodeCoordinator, entry: MoodeConfigEntry) -> None:
        super().__init__(coordinator, entry.entry_id, entry.unique_id, entry.title)
        self._entry = entry
        zone_name = entry.options.get(CONF_ZONE_NAME) or f"{entry.title} zone"
        self._attr_unique_id = f"{self._box_id}_zone"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._box_id}_zone")},
            name=zone_name,
            manufacturer=MANUFACTURER,
            model="moOde multiroom zone",
            via_device=(DOMAIN, self._box_id),
        )

    def _src(self) -> MoodeState:
        return self.coordinator.data

    def _receivers(self) -> list[MoodeCoordinator]:
        return [c for r in receivers_of(self.hass, self._entry) if (c := loaded(r))]

    def _rx_on(self) -> list[MoodeCoordinator]:
        return [
            c for c in self._receivers()
            if c.data and c.data.receiver and c.data.receiver.state == "On"
        ]

    @callback
    def _handle_coordinator_update(self) -> None:
        self._track_position()
        super()._handle_coordinator_update()

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        f = TRANSPORT | MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF
        return f if self.coordinator.data.fixed_volume else f | VOLUME_FEATURES

    @property
    def state(self) -> MediaPlayerState:
        if not self._rx_on() and self.coordinator.data.state != "play":
            return MediaPlayerState.OFF
        return STATE_MAP.get(self.coordinator.data.state, MediaPlayerState.IDLE)

    @property
    def volume_level(self):
        d = self.coordinator.data
        return None if d.volume is None or d.fixed_volume else d.volume / 100

    @property
    def is_volume_muted(self):
        d = self.coordinator.data
        return None if d.fixed_volume else d.muted

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rooms = {self._entry.title: "sender"}
        for r in receivers_of(self.hass, self._entry):
            c = loaded(r)
            rooms[r.title] = (
                c.data.receiver.state if c and c.data and c.data.receiver else "unavailable"
            )
        return {"rooms": rooms, "sender_entity": player_entity_id(self.hass, self._entry)}

    async def _run(self, coro, coord: MoodeCoordinator | None = None) -> None:
        try:
            await coro
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        await (coord or self.coordinator).async_request_refresh()

    async def async_turn_on(self) -> None:
        for c in self._receivers():
            if c.data and c.data.state in ("play", "pause"):
                await self._run(c.client.stop(), c)  # a Solo room rejoins cleanly
            await self._run(c.client.set_receiver(True), c)
        await self._resume_music()

    async def _resume_music(self) -> None:
        """Rooms are on: make sure there's something to hear. Resume what was queued;
        failing that, play the most recently played station, then the first favourite."""
        client = self.coordinator.client
        if self.coordinator.data and self.coordinator.data.state == "play":
            return
        try:
            await client.play()
            for _ in range(6):
                await asyncio.sleep(0.5)
                if await client.is_playing():
                    return await self.coordinator.async_request_refresh()
            fallback = None
            if self.coordinator.recent:
                fallback = self.coordinator.recent[0]["url"]
            elif favs := self._entry.options.get(CONF_FAVOURITES):
                fallback = favs[0]
            if fallback and fallback.startswith(("http://", "https://")):
                await client.play_url(fallback)       # a recently played stream
            elif fallback:
                await async_play(client, fallback)    # a favourite (file:RADIO/...)
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Everything silent: receivers off, Solo rooms stopped, sender stopped."""
        try:
            await zone_off(self.hass, self._entry)
        except MoodeError as err:
            raise HomeAssistantError(f"moOde: {err}") from err
        for c in self._receivers():
            await c.async_request_refresh()
        await self.coordinator.async_request_refresh()

    async def async_media_play(self) -> None:
        await self._run(self.coordinator.client.play())

    async def async_media_pause(self) -> None:
        await self._run(self.coordinator.client.pause(self.coordinator.data.is_radio))

    async def async_media_stop(self) -> None:
        await self._run(self.coordinator.client.stop())

    async def async_media_next_track(self) -> None:
        await self._run(self.coordinator.client.next())

    async def async_media_previous_track(self) -> None:
        await self._run(self.coordinator.client.previous())

    async def async_media_seek(self, position: float) -> None:
        await self._run(self.coordinator.client.seek(position))

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        await self._run(play_or_announce(
            self.hass, self.entity_id, self._entry, self.coordinator, media_id, bool(kwargs.get("announce"))
        ))

    async def async_search_media(self, query):
        return SearchMedia(result=await async_search(self.coordinator.client, query.search_query))

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        return await async_browse(self.coordinator.client, media_content_id)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._run(self.coordinator.client.set_volume(round(volume * 100)))

    async def async_volume_up(self) -> None:
        await self._run(self.coordinator.client.volume_step(True))

    async def async_volume_down(self) -> None:
        await self._run(self.coordinator.client.volume_step(False))

    async def async_mute_volume(self, mute: bool) -> None:
        if mute != self.coordinator.data.muted:
            await self._run(self.coordinator.client.toggle_mute())
