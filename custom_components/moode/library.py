"""Music library backend (the dashboard's FLACs tab): browse by artist, album and genre,
search, recently added, play, shuffle and queue. Everything comes from the box's own MPD
database; cover art comes from moOde's coverart.php through an authenticated proxy."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
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

from .api import MoodeError, artist_key, deezer_pick
from .const import CONF_ARTIST_IMAGES, DOMAIN, opt
from .zones import receiver_ids, wake_zone_if_silent

_LOGGER = logging.getLogger(__name__)

COVER_PATH = "/api/moode/cover"
ARTIST_PATH = "/api/moode/artist"
DEEZER_SEARCH = "https://api.deezer.com/search/artist"
NO_PHOTO_RETRY = 30 * 24 * 3600  # ask Deezer again about artists it had no photo for
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


def _artist_img(hass: HomeAssistant, entry_id: str, name: str) -> str:
    return async_sign_path(hass, f"{ARTIST_PATH}/{entry_id}?{urlencode({'name': name})}", SIGN_FOR)


def _artists_out(hass: HomeAssistant, entry_id: str, names: list[str]) -> list[dict[str, str]]:
    return [{"name": n, "image": _artist_img(hass, entry_id, n)} for n in names]


def _albums_out(hass: HomeAssistant, entry_id: str, albums: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{**a, "cover": _cover(hass, entry_id, a["artist"], a["album"])} for a in albums]


def _entity_for(hass: HomeAssistant, entry_id: str, suffix: str) -> str | None:
    for ent in er.async_entries_for_config_entry(er.async_get(hass), entry_id):
        if ent.domain == "media_player" and ent.unique_id.endswith(suffix):
            return ent.entity_id
    return None


def _err(connection, msg_id, err: Exception) -> None:
    connection.send_error(msg_id, "moode_error", str(err))


async def _download_image(hass: HomeAssistant, url: str, limit: int = COVER_MAX_BYTES) -> tuple[bytes, str] | None:
    try:
        async with async_get_clientsession(hass).get(url, timeout=ClientTimeout(total=15)) as r:
            ctype = r.headers.get("Content-Type", "").split(";")[0]
            if r.status != 200 or not ctype.startswith("image/"):
                return None
            body = bytearray()
            async for chunk in r.content.iter_chunked(64 * 1024):  # the WHOLE image
                body.extend(chunk)
                if len(body) > limit:
                    return None
            return bytes(body), ctype
    except (ClientError, asyncio.TimeoutError, OSError):
        return None


async def _album_cover(hass: HomeAssistant, entry_id: str, client, artist: str, album: str) -> tuple[bytes, str] | None:
    key = (entry_id, artist, album)
    if key in _first_file:
        _first_file.move_to_end(key)
        file = _first_file[key]
    else:
        try:
            file = await client.lib_first_file(artist, album)
        except MoodeError:
            return None
        _first_file[key] = file
        while len(_first_file) > _FIRST_FILE_MAX:
            _first_file.popitem(last=False)
    return await _download_image(hass, client.cover_url(file)) if file else None


def _image_response(img: tuple[bytes, str] | None, max_age: int = 604800) -> web.Response:
    if not img:
        return web.Response(status=404)
    return web.Response(body=img[0], content_type=img[1], headers={"Cache-Control": f"private, max-age={max_age}"})


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
        return _image_response(await _album_cover(hass, entry_id, client, artist, album))


class ArtistPhotos:
    """Artist photos from Deezer, kept on disk under .storage/moode/artists so each artist
    is looked up once. No photo (or no exact name match) is remembered too, and retried
    after a month."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.folder = Path(hass.config.path(".storage", "moode", "artists"))
        self.index_file = self.folder / "index.json"
        self.index: dict[str, dict[str, Any]] | None = None
        self.locks: dict[str, asyncio.Lock] = {}
        self.limit = asyncio.Semaphore(3)  # be gentle with Deezer
        self._save_handle = None

    async def _load(self) -> None:
        if self.index is None:
            def read() -> dict:
                try:
                    return json.loads(self.index_file.read_text("utf-8"))
                except (OSError, ValueError):
                    return {}
            self.index = await self.hass.async_add_executor_job(read)

    def _schedule_save(self) -> None:
        if self._save_handle:
            self._save_handle.cancel()
        self._save_handle = self.hass.loop.call_later(
            5, lambda: self.hass.async_create_task(self._save())
        )

    async def _save(self) -> None:
        data = json.dumps(self.index)
        def write() -> None:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self.index_file.with_suffix(".tmp")
            tmp.write_text(data, "utf-8")
            tmp.replace(self.index_file)
        await self.hass.async_add_executor_job(write)

    async def get(self, name: str) -> bytes | None:
        key = artist_key(name)
        if not key:
            return None
        await self._load()
        async with self.locks.setdefault(key, asyncio.Lock()):
            hit = self.index.get(key)
            if hit and hit.get("file"):
                path = self.folder / hit["file"]
                data = await self.hass.async_add_executor_job(_read_bytes, path)
                if data:
                    return data
            elif hit and time.time() - hit.get("checked", 0) < NO_PHOTO_RETRY:
                return None
            data = await self._lookup(name)
            entry: dict[str, Any] = {"name": name, "checked": int(time.time()), "file": None}
            if data:
                fname = hashlib.sha1(key.encode()).hexdigest()[:16] + ".jpg"
                await self.hass.async_add_executor_job(_write_bytes, self.folder / fname, data)
                entry["file"] = fname
            self.index[key] = entry
            self._schedule_save()
            return data

    async def _lookup(self, name: str) -> bytes | None:
        async with self.limit:
            try:
                async with async_get_clientsession(self.hass).get(
                    DEEZER_SEARCH, params={"q": name, "limit": "10"}, timeout=ClientTimeout(total=10)
                ) as r:
                    if r.status != 200:
                        return None
                    url = deezer_pick(name, await r.json(content_type=None))
            except (ClientError, asyncio.TimeoutError, OSError, ValueError):
                return None
            if not url:
                return None
            img = await _download_image(self.hass, url, 1_000_000)
            return img[0] if img else None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class MoodeArtistView(HomeAssistantView):
    """Artist pictures: a photo from Deezer (if allowed in the box's options), otherwise
    the cover of one of the artist's albums."""

    url = ARTIST_PATH + "/{entry_id}"
    name = "api:moode:artist"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        hass = request.app["hass"]
        name = request.query.get("name")
        if not name:
            return web.Response(status=404)
        try:
            entry = _entry(hass, entry_id)
        except ValueError:
            return web.Response(status=404)
        if opt(entry, CONF_ARTIST_IMAGES):
            photo = await hass.data[DOMAIN + "_artist_photos"].get(name)
            if photo:
                return _image_response((photo, "image/jpeg"))
        client = entry.runtime_data.client
        try:
            albums = await client.lib_albums(name)
        except MoodeError:
            return web.Response(status=404)
        for a in albums[:3]:
            if img := await _album_cover(hass, entry_id, client, a["artist"], a["album"]):
                return _image_response(img, 86400)
        return web.Response(status=404)


@callback
def async_setup_library(hass: HomeAssistant) -> None:
    hass.data[DOMAIN + "_artist_photos"] = ArtistPhotos(hass)
    hass.http.register_view(MoodeCoverView())
    hass.http.register_view(MoodeArtistView())
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
    connection.send_result(msg["id"], _artists_out(hass, msg["entry_id"], artists))


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
        "artist_image": _artist_img(hass, msg["entry_id"], msg["artist"]),
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
        "artists": _artists_out(hass, msg["entry_id"], artists[:50]),
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
