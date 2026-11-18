"""Station manager backend: websocket commands for the Stations sidebar panel.

Everything goes through moOde's own Radio Manager endpoints (command/radio.php),
so moOde's database, .pls files and logos stay consistent.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import voluptuous as vol
from aiohttp import ClientConnectorError, ClientError, ClientTimeout, InvalidURL, web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MoodeError, clean_station_name
from .const import CONF_FAVOURITES, DOMAIN

_LOGGER = logging.getLogger(__name__)

RADIO_BROWSER = (
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
)
UA = {"User-Agent": "HomeAssistant-moOde/1.0"}
LOGO_PATH = "/api/moode/logo"


# ------------------------------------------------------------------ helpers


def _entry(hass: HomeAssistant, entry_id: str):
    e = hass.config_entries.async_get_entry(entry_id)
    if not e or e.domain != DOMAIN or e.state is not ConfigEntryState.LOADED:
        raise ValueError("That moOde box isn't loaded")
    return e


def _client(hass: HomeAssistant, entry_id: str):
    return _entry(hass, entry_id).runtime_data.client


def _fav_names(entry) -> set[str]:
    out = set()
    for m in entry.options.get(CONF_FAVOURITES, []):
        out.add(m.split(":", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[0])
    return out


async def _fetch_data_url(hass: HomeAssistant, url: str, limit: int = 512_000) -> str | None:
    """Download an image and return it as a data: URL (for moOde's logo upload)."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        async with async_get_clientsession(hass).get(
            url, timeout=ClientTimeout(total=8), headers=UA
        ) as r:
            if r.status != 200:
                return None
            ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
            if not ctype.startswith("image/"):
                return None
            body = bytearray()
            async for chunk in r.content.iter_chunked(64 * 1024):   # the whole image
                body.extend(chunk)
                if len(body) > limit:
                    return None
            if not body:
                return None
            body = bytes(body)
            return f"data:{ctype};base64,{base64.b64encode(body).decode()}"
    except (ClientError, asyncio.TimeoutError, OSError):
        return None


async def probe_stream(hass: HomeAssistant, url: str) -> dict[str, str]:
    """Read bitrate/format from a stream's headers (icy-br, Content-Type)."""
    fmt_map = {"mpeg": "MP3", "mp3": "MP3", "aac": "AAC", "aacp": "AAC+", "ogg": "OGG",
               "opus": "Opus", "flac": "FLAC"}
    try:
        async with async_get_clientsession(hass).get(
            url, timeout=ClientTimeout(total=8), headers={**UA, "Icy-MetaData": "0"}
        ) as r:
            out = {}
            br = r.headers.get("icy-br", "").split(",")[0].strip()
            if br.isdigit():
                out["bitrate"] = br
            ctype = r.headers.get("Content-Type", "").lower()
            for k, v in fmt_map.items():
                if k in ctype:
                    out["format"] = v
                    break
            return out
    except (ClientError, asyncio.TimeoutError, OSError, ValueError):
        return {}


async def check_stream(hass: HomeAssistant, url: str, timeout: float = 10, _depth: int = 0) -> str:
    """'ok' (answers with data), 'dead' (definite failure: HTTP error, unknown host,
    refused, invalid URL) or 'unsure' (timed out / too slow; could be a busy server)."""
    if not url.startswith(("http://", "https://")):
        return "dead"
    try:
        async with async_get_clientsession(hass).get(
            url,
            timeout=ClientTimeout(total=timeout, sock_read=timeout * 0.7),
            headers={**UA, "Icy-MetaData": "0"},
            allow_redirects=True,
        ) as r:
            if r.status >= 400:
                return "dead"
            ctype = r.headers.get("Content-Type", "").lower()
            chunk = await r.content.read(2048)
            if not chunk:
                return "unsure"
            is_list = any(t in ctype for t in ("mpegurl", "scpls")) or url.lower().split("?")[0].endswith(
                (".pls", ".m3u")
            )
            if is_list and _depth == 0:
                for line in chunk.decode("utf-8", "ignore").splitlines():
                    line = line.split("=", 1)[-1].strip() if line.lower().startswith("file") else line.strip()
                    if line.startswith(("http://", "https://")):
                        return await check_stream(hass, line, timeout, 1)
                return "dead"
            return "ok"
    except asyncio.TimeoutError:
        return "unsure"
    except (ClientConnectorError, InvalidURL) as err:
        # unknown host / refused: definite, unless it smells like a local network hiccup
        return "unsure" if isinstance(getattr(err, "os_error", None), TimeoutError) else "dead"
    except (ClientError, OSError, ValueError):
        return "unsure"


def _station_out(hass: HomeAssistant, entry, row: dict[str, Any], favs: set[str]) -> dict[str, Any]:
    name = row.get("name", "")
    logo = async_sign_path(
        hass, f"{LOGO_PATH}/{entry.entry_id}/{quote(name, safe='')}", timedelta(hours=12)
    )
    return {
        "id": row.get("id"),
        "name": name,
        "url": row.get("station", ""),
        "genre": row.get("genre", ""),
        "country": row.get("country", ""),
        "bitrate": row.get("bitrate", ""),
        "format": row.get("format", ""),
        "type": row.get("type", ""),
        "home_page": row.get("home_page", ""),
        "favourite": name in favs,
        "builtin": _is_builtin(row),
        "logo": logo,
    }


def _is_builtin(row: dict[str, Any]) -> bool:
    """moOde ships its stations with ids below 500; user-added ones start at 500."""
    try:
        return int(row.get("id") or 0) < 500
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------ logo proxy


class MoodeLogoView(HomeAssistantView):
    """Serves moOde station logos through HA (avoids http-on-https blocking)."""

    url = LOGO_PATH + "/{entry_id}/{name}"
    name = "api:moode:logo"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, name: str) -> web.Response:
        hass = request.app["hass"]
        # Only plain station names: no path tricks that could reach other moOde URLs.
        if not name or "/" in name or "\\" in name or ".." in name or len(name) > 120:
            return web.Response(status=404)
        try:
            client = _client(hass, entry_id)
        except ValueError:
            return web.Response(status=404)
        try:
            async with async_get_clientsession(hass).get(
                client.logo_url(name), timeout=ClientTimeout(total=8)
            ) as r:
                if r.status != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                    return web.Response(status=404)
                # read the WHOLE image (content.read(n) may return just the first chunk)
                body = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        return web.Response(status=404)
                body = bytes(body)
                return web.Response(
                    body=body,
                    content_type=r.headers.get("Content-Type", "image/jpeg").split(";")[0],
                    headers={"Cache-Control": "no-cache"},  # logos can change (Edit station)
                )
        except (ClientError, asyncio.TimeoutError, OSError):
            return web.Response(status=404)


# ------------------------------------------------------------------ websocket commands


class MoodeStationPictureView(MoodeLogoView):
    """Station logos for entity pictures (dashboard buttons). Picture <img> requests can't
    carry HA auth, so this route is open, but it only ever serves radio-logo thumbnails
    from a configured moOde box (public station artwork), nothing else."""

    url = "/api/moode/station_picture/{entry_id}/{name}"
    name = "api:moode:station_picture"
    requires_auth = False


@callback
def async_setup_stations(hass: HomeAssistant) -> None:
    hass.http.register_view(MoodeLogoView())
    hass.http.register_view(MoodeStationPictureView())
    for cmd in (
        ws_boxes, ws_list, ws_check, ws_delete, ws_add, ws_search,
        ws_favourite, ws_play, ws_sync, ws_backup, ws_restore, ws_update,
    ):
        websocket_api.async_register_command(hass, cmd)


def _err(connection, msg_id, err: Exception) -> None:
    connection.send_error(msg_id, "moode_error", str(err))


@websocket_api.websocket_command({vol.Required("type"): "moode/boxes"})
@websocket_api.async_response
async def ws_boxes(hass, connection, msg):
    boxes = []
    for e in hass.config_entries.async_entries(DOMAIN):
        if e.state is ConfigEntryState.LOADED:
            boxes.append({"entry_id": e.entry_id, "title": e.title,
                          "host": e.runtime_data.client.host,
                          "sender": bool(e.options.get("receivers"))})
    boxes.sort(key=lambda b: (not b["sender"], b["title"].lower()))
    connection.send_result(msg["id"], boxes)


@websocket_api.websocket_command({vol.Required("type"): "moode/stations", vol.Required("entry_id"): str})
@websocket_api.async_response
async def ws_list(hass, connection, msg):
    try:
        entry = _entry(hass, msg["entry_id"])
        rows = await entry.runtime_data.client.get_stations()
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    favs = _fav_names(entry)
    out = [_station_out(hass, entry, r, favs) for r in rows]
    out.sort(key=lambda s: s["name"].casefold())
    connection.send_result(msg["id"], out)


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/check", vol.Required("urls"): [str]}
)
@websocket_api.async_response
async def ws_check(hass, connection, msg):
    """Pass 1: quick parallel test. Pass 2: anything not 'ok' is retried one at a time
    with a longer timeout, so a slow or briefly busy server isn't called broken."""
    urls = list(dict.fromkeys(msg["urls"]))
    sem = asyncio.Semaphore(6)

    async def quick(u):
        async with sem:
            return u, await check_stream(hass, u, 10)

    result = dict(await asyncio.gather(*(quick(u) for u in urls)))
    for u, st in result.items():
        if st != "ok":
            result[u] = await check_stream(hass, u, 20)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/delete", vol.Required("entry_id"): str,
     vol.Required("names"): [str]}
)
@websocket_api.async_response
async def ws_delete(hass, connection, msg):
    try:
        entry = _entry(hass, msg["entry_id"])
    except ValueError as err:
        return _err(connection, msg["id"], err)
    client = entry.runtime_data.client
    replies: dict[str, str] = {}
    failed = []
    for name in msg["names"]:
        try:
            replies[name] = str(await client.delete_station(name) or "")[:200]
        except MoodeError as err:
            failed.append({"name": name, "error": str(err)})
    await client.refresh_radio_library()
    # moOde's del_station answers nothing either way, so check what's really gone.
    try:
        remaining = {r["name"] for r in await client.get_stations()}
    except MoodeError as err:
        return _err(connection, msg["id"], Exception(f"Deleted, but couldn't verify: {err}"))
    done = []
    for name, reply in replies.items():
        if name in remaining:
            _LOGGER.warning("moOde did not delete station %r (moOde replied: %r)", name, reply)
            failed.append({"name": name, "error": f"moOde kept it (reply: {reply or 'empty'})"})
        else:
            done.append(name)
    # drop deleted stations from favourites
    favs = [m for m in entry.options.get(CONF_FAVOURITES, [])
            if m.split(":", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[0] not in set(done)]
    if favs != entry.options.get(CONF_FAVOURITES, []):
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_FAVOURITES: favs})
    connection.send_result(msg["id"], {"deleted": done, "failed": failed})


STATION_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("url"): str,
        vol.Optional("genre", default=""): str,
        vol.Optional("country", default=""): str,
        vol.Optional("bitrate", default=""): vol.Coerce(str),
        vol.Optional("format", default=""): str,
        vol.Optional("home_page", default=""): str,
        vol.Optional("logo_url", default=""): str,
        vol.Optional("logo_data", default=""): str,
    },
    extra=vol.REMOVE_EXTRA,
)


async def _add_many(hass, client, stations: list[dict], existing: set[str]) -> tuple[list, list]:
    added, failed = [], []
    for st in stations:
        name = clean_station_name(st["name"])
        if name.casefold() in existing:
            failed.append({"name": name, "error": "already exists"})
            continue
        data_url = st.get("logo_data") if str(st.get("logo_data", "")).startswith("data:image/") else None
        data_url = data_url or await _fetch_data_url(hass, st.get("logo_url", ""))
        try:
            staged = await client.stage_station_logo(name, data_url) if data_url else False
            reply = await client.add_station({
                "name": name, "url": st["url"], "genre": st.get("genre", ""),
                "country": st.get("country", ""), "bitrate": st.get("bitrate", ""),
                "format": st.get("format", ""), "home_page": st.get("home_page", ""),
            })
            if reply not in (None, "", "OK"):
                raise MoodeError(str(reply))
            if staged:
                await client.wait_logo_applied(name)
            existing.add(name.casefold())
            added.append(name)
        except MoodeError as err:
            failed.append({"name": name, "error": str(err)})
    await client.refresh_radio_library()
    return added, failed


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/add", vol.Required("entry_id"): str,
     vol.Required("stations"): [STATION_SCHEMA]}
)
@websocket_api.async_response
async def ws_add(hass, connection, msg):
    try:
        client = _client(hass, msg["entry_id"])
        existing = {r["name"].casefold() for r in await client.get_stations()}
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    added, failed = await _add_many(hass, client, msg["stations"], existing)
    connection.send_result(msg["id"], {"added": added, "failed": failed})


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/search", vol.Optional("name", default=""): str,
     vol.Optional("country", default=""): str, vol.Optional("tag", default=""): str,
     vol.Optional("limit", default=60): vol.All(int, vol.Range(min=1, max=200))}
)
@websocket_api.async_response
async def ws_search(hass, connection, msg):
    params = {"hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": str(msg["limit"])}
    if msg["name"]:
        params["name"] = msg["name"]
    if msg["country"]:
        c = msg["country"].strip()
        params["countrycode" if len(c) == 2 else "country"] = c
    if msg["tag"]:
        params["tag"] = msg["tag"]
    session = async_get_clientsession(hass)
    last = None
    for base in RADIO_BROWSER:
        try:
            async with session.get(f"{base}/json/stations/search", params=params,
                                   timeout=ClientTimeout(total=12), headers=UA) as r:
                if r.status != 200:
                    last = f"HTTP {r.status}"
                    continue
                rows = await r.json(content_type=None)
                break
        except (ClientError, asyncio.TimeoutError, OSError, ValueError) as err:
            last = str(err)
    else:
        return _err(connection, msg["id"], Exception(f"Station directory unreachable ({last})"))
    out = []
    for s in rows:
        url = s.get("url_resolved") or s.get("url")
        if not url:
            continue
        out.append({
            "name": (s.get("name") or "").strip(),
            "url": url,
            "genre": ", ".join(t for t in (s.get("tags") or "").split(",")[:3] if t),
            "country": s.get("country") or s.get("countrycode") or "",
            "bitrate": str(s.get("bitrate") or ""),
            "format": s.get("codec") or "",
            "home_page": s.get("homepage") or "",
            "logo_url": s.get("favicon") or "",
            "votes": s.get("votes") or 0,
        })
    connection.send_result(msg["id"], out)


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/favourite", vol.Required("entry_id"): str,
     vol.Required("name"): str, vol.Required("favourite"): bool}
)
@websocket_api.async_response
async def ws_favourite(hass, connection, msg):
    try:
        entry = _entry(hass, msg["entry_id"])
    except ValueError as err:
        return _err(connection, msg["id"], err)
    mid = f"file:RADIO/{msg['name']}.pls"
    favs = [m for m in entry.options.get(CONF_FAVOURITES, []) if m != mid]
    if msg["favourite"]:
        favs.append(mid)
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_FAVOURITES: favs})
    connection.send_result(msg["id"], {"favourite": msg["favourite"]})


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/play", vol.Required("entry_id"): str, vol.Required("url"): str}
)
@websocket_api.async_response
async def ws_play(hass, connection, msg):
    try:
        await _client(hass, msg["entry_id"]).play_url(msg["url"])
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/sync", vol.Required("source"): str,
     vol.Required("targets"): [str], vol.Optional("mirror", default=False): bool}
)
@websocket_api.async_response
async def ws_sync(hass, connection, msg):
    """Copy the source box's stations (with logos) to other boxes.
    mirror=True also deletes stations the source doesn't have."""
    try:
        src = _client(hass, msg["source"])
        src_rows = await src.get_stations()
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    src_names = {r["name"].casefold() for r in src_rows}
    report = {}
    for tid in msg["targets"]:
        if tid == msg["source"]:
            continue
        try:
            tgt = _client(hass, tid)
            rows = await tgt.get_stations()
        except (ValueError, MoodeError) as err:
            report[tid] = {"error": str(err)}
            continue
        existing = {r["name"].casefold() for r in rows}
        todo = [{"name": r["name"], "url": r.get("station", ""), "genre": r.get("genre", ""),
                 "country": r.get("country", ""), "bitrate": r.get("bitrate", ""),
                 "format": r.get("format", ""), "home_page": r.get("home_page", ""),
                 "logo_url": src.logo_url(r["name"], thumb=False)}
                for r in src_rows if r["name"].casefold() not in existing]
        added, failed = await _add_many(hass, tgt, todo, existing)
        removed = []
        if msg["mirror"]:
            for r in rows:
                if r["name"].casefold() not in src_names:
                    try:
                        await tgt.delete_station(r["name"])
                        removed.append(r["name"])
                    except MoodeError as err:
                        failed.append({"name": r["name"], "error": str(err)})
            await tgt.refresh_radio_library()
        report[tid] = {"added": len(added), "removed": len(removed), "failed": failed}
    connection.send_result(msg["id"], report)


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/backup", vol.Required("entry_id"): str,
     vol.Optional("names"): [str]}
)
@websocket_api.async_response
async def ws_backup(hass, connection, msg):
    """Station rows plus logos (as data: URLs). Pass `names` to fetch in chunks."""
    try:
        client = _client(hass, msg["entry_id"])
        rows = await client.get_stations()
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    if "names" in msg:
        wanted = set(msg["names"])
        rows = [r for r in rows if r["name"] in wanted]
    sem = asyncio.Semaphore(6)

    async def one(r):
        async with sem:
            logo = await _fetch_data_url(hass, client.logo_url(r["name"], thumb=False))
        return {
            "name": r["name"], "url": r.get("station", ""), "genre": r.get("genre", ""),
            "country": r.get("country", ""), "bitrate": str(r.get("bitrate", "")),
            "format": r.get("format", ""), "home_page": r.get("home_page", ""),
            "builtin": _is_builtin(r), "logo_data": logo or "",
        }

    connection.send_result(msg["id"], await asyncio.gather(*(one(r) for r in rows)))


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/restore", vol.Required("entry_id"): str,
     vol.Required("stations"): [STATION_SCHEMA],
     vol.Optional("delete_others", default=False): bool,
     vol.Optional("keep", default=list): [str]}
)
@websocket_api.async_response
async def ws_restore(hass, connection, msg):
    """Add backup stations that are missing. With delete_others, also remove stations
    whose names aren't in `keep` (the full backup's name list; sent with the last chunk)."""
    try:
        client = _client(hass, msg["entry_id"])
        rows = await client.get_stations()
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    existing = {r["name"].casefold() for r in rows}
    added, failed = await _add_many(hass, client, msg["stations"], existing)
    failed = [f for f in failed if f.get("error") != "already exists"]
    removed = []
    if msg["delete_others"]:
        keep = {clean_station_name(n).casefold() for n in msg["keep"]}
        for r in rows:
            if r["name"].casefold() not in keep:
                try:
                    await client.delete_station(r["name"])
                    removed.append(r["name"])
                except MoodeError as err:
                    failed.append({"name": r["name"], "error": str(err)})
        await client.refresh_radio_library()
    connection.send_result(msg["id"], {"added": added, "removed": removed, "failed": failed})


@websocket_api.websocket_command(
    {vol.Required("type"): "moode/stations/update", vol.Required("entry_id"): str,
     vol.Required("name"): str,
     vol.Optional("new_name"): str, vol.Optional("url"): str, vol.Optional("genre"): str,
     vol.Optional("country"): str, vol.Optional("bitrate"): vol.Coerce(str),
     vol.Optional("logo_data"): str}
)
@websocket_api.async_response
async def ws_update(hass, connection, msg):
    """Edit a station (name, URL, genre, country, logo). Done as a verified replace:
    delete, stage logo, re-add; restored if moOde refuses. Favourites follow a rename."""
    try:
        entry = _entry(hass, msg["entry_id"])
        client = entry.runtime_data.client
        rows = await client.get_stations()
    except (ValueError, MoodeError) as err:
        return _err(connection, msg["id"], err)
    row = next((r for r in rows if r["name"] == msg["name"]), None)
    if row is None:
        return _err(connection, msg["id"], Exception(f"No station called {msg['name']}"))
    url = (msg.get("url") or row.get("station", "")).strip()
    if not url.startswith(("http://", "https://")):
        return _err(connection, msg["id"], Exception("The stream URL must start with http:// or https://"))
    new_logo = msg.get("logo_data") if str(msg.get("logo_data", "")).startswith("data:image/") else None
    new_name = clean_station_name(msg.get("new_name") or row["name"])
    if new_name != row["name"] and new_name.casefold() in {r["name"].casefold() for r in rows}:
        return _err(connection, msg["id"], Exception(f"There's already a station called {new_name}"))

    # moOde's upd_station doesn't reliably apply changes from outside its own UI, so every
    # edit replaces the station: delete it, stage the logo, add it again with the new details.
    old_logo = await _fetch_data_url(hass, client.logo_url(row["name"], thumb=False))
    logo = new_logo or old_logo
    fields = {k: row.get(k, "") for k in ("genre", "country", "bitrate", "format", "home_page",
                                           "broadcaster", "language", "region")}
    fields.update({k: msg[k] for k in ("genre", "country", "bitrate") if k in msg})
    if url != row.get("station"):
        probed = await probe_stream(hass, url)       # new stream: read its real bitrate/format
        if probed.get("bitrate") and msg.get("bitrate", row.get("bitrate")) == row.get("bitrate"):
            fields["bitrate"] = probed["bitrate"]
        if probed.get("format"):
            fields["format"] = probed["format"]
    original = {**{k: row.get(k, "") for k in fields}, "name": row["name"], "url": row.get("station", "")}
    try:
        await client.delete_station(row["name"])  # frees the URL (moOde refuses duplicates)
        staged = await client.stage_station_logo(new_name, logo) if logo else False
        try:
            reply = await client.add_station({**fields, "name": new_name, "url": url})
        except MoodeError as err:
            reply = str(err)
        if reply not in (None, "", "OK"):
            if old_logo:
                await client.stage_station_logo(row["name"], old_logo)
            await client.add_station(original)
            await client.refresh_radio_library()
            raise MoodeError(f"moOde refused the change ({reply}); the station was left as it was")
        logo_ok = (staged and await client.wait_logo_applied(new_name)) if logo else True
        await client.refresh_radio_library()

        # verify: moOde must now have the station with the new URL
        after = {r["name"]: r for r in await client.get_stations()}
        if new_name not in after or after[new_name].get("station") != url:
            _LOGGER.warning("Edit of %r not applied by moOde: now %r", row["name"], after.get(new_name))
            raise MoodeError("moOde didn't keep the change. Please try again")

        if new_name != row["name"]:
            old_mid, new_mid = f"file:RADIO/{row['name']}.pls", f"file:RADIO/{new_name}.pls"
            favs = [new_mid if m == old_mid else m for m in entry.options.get(CONF_FAVOURITES, [])]
            if favs != entry.options.get(CONF_FAVOURITES, []):
                hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_FAVOURITES: favs})
    except MoodeError as err:
        return _err(connection, msg["id"], err)
    connection.send_result(msg["id"], {"name": new_name, "logo_ok": logo_ok})
