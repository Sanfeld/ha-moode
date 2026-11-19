"""Music library backend (the dashboard's FLACs tab): browse by artist, album and genre,
search, recently added, play, shuffle and queue. Everything comes from the box's own MPD
database; cover art comes from moOde's coverart.php through an authenticated proxy."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MoodeError
from .const import DOMAIN
from .zones import receiver_ids, wake_zone_if_silent

_LOGGER = logging.getLogger(__name__)

COVER_PATH = "/api/moode/cover"
COVER_MAX_BYTES = 6_000_000
SIGN_FOR = timedelta(hours=24)

# (entry_id, album artist, album) -> first track's file, so covers don't re-query MPD
_first_file: OrderedDict[tuple[str, str, str], str | None] = OrderedDict()
_FIRST_FILE_MAX = 5000


def _entry(hass: HomeAssistant, entry_id: str):
    e = hass.config_entries.async_get_entry(entry_id)
    if not e or e.domain != DOMAIN or e.state is not ConfigEntryState.LOADED:
        raise ValueError("That moOde box isn't loaded")
    return e


def _cover(hass: HomeAssistant, entry_id: str, artist: str, album: str) -> str:
    query = urlencode({"artist": artist, "album": album})
    return async_sign_path(hass, f"{COVER_PATH}/{entry_id}?{query}", SIGN_FOR)


def _albums_out(hass: HomeAssistant, entry_id: str, albums: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{**a, "cover": _cover(hass, entry_id, a["artist"], a["album"])} for a in albums]


def _entity_for(hass: HomeAssistant, entry_id: str, suffix: str) -> str | None:
    for ent in er.async_entries_for_config_entry(er.async_get(hass), entry_id):
        if ent.domain == "media_player" and ent.unique_id.endswith(suffix):
            return ent.entity_id
    return None


def _err(connection, msg_id, err: Exception) -> None:
    connection.send_error(msg_id, "moode_error", str(err))


class MoodeCoverView(HomeAssistantView):
    """Album covers. Each URL is signed by HA (the album lists hand them out), so plain
    <img> tags work without exposing the box."""

    url = COVER_PATH + "/{entry_id}"
    name = "api:moode:cover"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        hass = request.app["hass"]
        artist, album = request.query.get("artist"), request.query.get("album")
        if artist is None or not album:
            return web.Response(status=404)
        try:
            client = _entry(hass, entry_id).runtime_data.client
        except ValueError:
            return web.Response(status=404)
        key = (entry_id, artist, album)
        if key in _first_file:
            _first_file.move_to_end(key)
            file = _first_file[key]
        else:
            try:
                file = await client.lib_first_file(artist, album)
            except MoodeError:
                return web.Response(status=404)
            _first_file[key] = file
            while len(_first_file) > _FIRST_FILE_MAX:
                _first_file.popitem(last=False)
        if not file:
            return web.Response(status=404)
        try:
            async with async_get_clientsession(hass).get(
                client.cover_url(file), timeout=ClientTimeout(total=15)
            ) as r:
                ctype = r.headers.get("Content-Type", "").split(";")[0]
                if r.status != 200 or not ctype.startswith("image/"):
                    return web.Response(status=404)
                body = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > COVER_MAX_BYTES:
                        return web.Response(status=404)
        except (ClientError, asyncio.TimeoutError, OSError):
            return web.Response(status=404)
        return web.Response(body=bytes(body), content_type=ctype,
                            headers={"Cache-Control": "private, max-age=604800"})


@callback
def async_setup_library(hass: HomeAssistant) -> None:
    hass.http.register_view(MoodeCoverView())
    for cmd in (ws_boxes, ws_artists, ws_albums, ws_genres, ws_tracks, ws_search, ws_recent, ws_play):
        websocket_api.async_register_command(hass, cmd)


@websocket_api.websocket_command({vol.Required("type"): "moode/library/boxes"})
@websocket_api.async_response
async def ws_boxes(hass, connection, msg):
    """Boxes that have music of their own, with library size and their player/zone entities."""
    out = []
    for e in hass.config_entries.async_entries(DOMAIN):
        if e.state is not ConfigEntryState.LOADED:
            continue
        try:
            stats = await e.runtime_data.client.lib_stats()
        except MoodeError:
            continue
        if not stats["songs"]:
            continue
        out.append({
            "entry_id": e.entry_id, "title": e.title, **stats,
            "sender": bool(receiver_ids(e)),
            "player": _entity_for(hass, e.entry_id, "_player"),
            "zone": _entity_for(hass, e.entry_id, "_zone"),
        })
    out.sort(key=lambda b: (not b["sender"], -b["songs"], b["title"].lower()))
    connection.send_result(msg["id"], out)


def _cmd(name: str, extra: dict | None = None):
    return websocket_api.websocket_command({vol.Required("type"): f"moode/library/{name}",
                                            vol.Required("entry_id"): str, **(extra or {})})


@_cmd("artists", {vol.Optional("genre"): str})
@websocket_api.async_response
async def ws_artists(hass, connection, msg):
    try:
        artists = await _entry(hass, msg["entry_id"]).runtime_data.client.lib_artists(msg.get("genre"))
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    connection.send_result(msg["id"], artists)


@_cmd("albums", {vol.Optional("artist"): str, vol.Optional("genre"): str})
@websocket_api.async_response
async def ws_albums(hass, connection, msg):
    try:
        client = _entry(hass, msg["entry_id"]).runtime_data.client
        albums = await client.lib_albums(msg.get("artist"), msg.get("genre"))
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    connection.send_result(msg["id"], _albums_out(hass, msg["entry_id"], albums))


@_cmd("genres")
@websocket_api.async_response
async def ws_genres(hass, connection, msg):
    try:
        genres = await _entry(hass, msg["entry_id"]).runtime_data.client.lib_genres()
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    connection.send_result(msg["id"], genres)


@_cmd("tracks", {vol.Required("artist"): str, vol.Required("album"): str})
@websocket_api.async_response
async def ws_tracks(hass, connection, msg):
    try:
        tracks = await _entry(hass, msg["entry_id"]).runtime_data.client.lib_tracks(msg["artist"], msg["album"])
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    connection.send_result(msg["id"], {
        "tracks": tracks,
        "cover": _cover(hass, msg["entry_id"], msg["artist"], msg["album"]),
    })


@_cmd("search", {vol.Required("query"): str})
@websocket_api.async_response
async def ws_search(hass, connection, msg):
    """One search, three answers: matching artists, albums and songs."""
    q = msg["query"].strip()
    if len(q) < 2:
        return connection.send_result(msg["id"], {"artists": [], "albums": [], "tracks": []})
    try:
        tracks = await _entry(hass, msg["entry_id"]).runtime_data.client.lib_search(q)
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    ql = q.casefold()
    artists = sorted({t["album_artist"] for t in tracks
                      if t["album_artist"] and ql in t["album_artist"].casefold()}, key=str.casefold)
    albums: dict[tuple[str, str], dict[str, str]] = {}
    for t in tracks:
        if t["album"] and (ql in t["album"].casefold() or ql in t["album_artist"].casefold()):
            albums.setdefault((t["album_artist"], t["album"]),
                              {"album": t["album"], "artist": t["album_artist"], "year": t["year"]})
    songs = [t for t in tracks if ql in t["title"].casefold()][:100]
    connection.send_result(msg["id"], {
        "artists": artists[:50],
        "albums": _albums_out(hass, msg["entry_id"], list(albums.values())[:60]),
        "tracks": songs,
    })


@_cmd("recent")
@websocket_api.async_response
async def ws_recent(hass, connection, msg):
    try:
        albums = await _entry(hass, msg["entry_id"]).runtime_data.client.lib_recent()
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    connection.send_result(msg["id"], _albums_out(hass, msg["entry_id"], albums))


@_cmd("play", {
    vol.Required("what"): vol.In(["album", "artist", "genre", "all"]),
    vol.Optional("artist"): str, vol.Optional("album"): str, vol.Optional("genre"): str,
    vol.Optional("position", default=0): vol.All(int, vol.Range(min=0)),
    vol.Optional("shuffle", default=False): bool,
    vol.Optional("queue", default=False): bool,
})
@websocket_api.async_response
async def ws_play(hass, connection, msg):
    try:
        entry = _entry(hass, msg["entry_id"])
        await entry.runtime_data.client.lib_play(
            msg["what"], artist=msg.get("artist"), album=msg.get("album"), genre=msg.get("genre"),
            position=msg["position"], shuffle=msg["shuffle"], queue=msg["queue"],
        )
        # Playing on a zone's sender with every room off would be silent: wake the rooms.
        await wake_zone_if_silent(hass, entry)
        await entry.runtime_data.async_request_refresh()
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    connection.send_result(msg["id"], {"ok": True})
