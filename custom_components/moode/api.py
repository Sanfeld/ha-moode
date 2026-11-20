"""Small async client for a moOde audio player.

Two channels are used:
  * moOde's REST API at http://<host>/command/?cmd=...  (volume, metadata,
    multiroom receiver, play_item)
  * MPD's text protocol on TCP 6600 (transport state, elapsed time, queue
    control). It is always available, even when moOde's metadata file is off.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

_LOGGER = logging.getLogger(__name__)

try:  # aiohttp ships with Home Assistant; tests run without it.
    from aiohttp import ClientError, ClientTimeout
except ImportError:  # pragma: no cover
    ClientError = OSError  # type: ignore[misc,assignment]
    ClientTimeout = None  # type: ignore[misc,assignment]

DEFAULT_MPD_PORT = 6600
TIMEOUT = 5


class MoodeError(Exception):
    """Any failure talking to a moOde box."""


@dataclass
class ReceiverStatus:
    """Multiroom receiver status as reported by get_receiver_status."""

    state: str  # "On", "Off", "Disabled" or "Unknown"
    volume: int | None = None
    muted: bool | None = None
    master_volume_opt_in: bool | None = None
    hostname: str | None = None
    multicast: str | None = None

    @property
    def enabled(self) -> bool:
        return self.state in ("On", "Off")


@dataclass
class MoodeState:
    """One poll of a moOde box."""

    state: str = "stop"  # play / pause / stop
    volume: int | None = None
    muted: bool = False
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    file: str | None = None
    station: str | None = None
    cover_url: str | None = None
    elapsed: float | None = None
    duration: float | None = None
    output_format: str | None = None
    receiver: ReceiverStatus | None = None
    renderer: str | None = None  # "AirPlay", "Spotify", ... when a renderer is active
    mpd_error: str | None = None
    fixed_volume: bool = False  # Volume type "Fixed (0dB)": MPD has no mixer
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_radio(self) -> bool:
        return bool(self.file and self.file.startswith(("http://", "https://")))


# --------------------------------------------------------------------------- parsing


def parse_rest_body(text: str) -> Any:
    """moOde answers JSON for most commands but not always with a JSON header."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_receiver_status(value: Any) -> ReceiverStatus:
    """Parse 'rx,On,15,0,1,host,239.0.0.1' (fields documented in the setup guide)."""
    if isinstance(value, dict):
        # Be tolerant if a future version wraps it, e.g. {"rx_status": "..."}
        value = next((v for v in value.values() if isinstance(v, str)), "")
    if not isinstance(value, str) or not value.strip():
        return ReceiverStatus(state="Unknown")

    parts = [p.strip() for p in value.strip().split(",")]
    if parts and parts[0].lower() == "rx":
        parts = parts[1:]

    raw_state = parts[0].lstrip("-").strip().capitalize() if parts else ""
    state = raw_state if raw_state in ("On", "Off", "Disabled") else "Unknown"

    def _int(i: int) -> int | None:
        try:
            return int(parts[i])
        except (IndexError, ValueError):
            return None

    def _bool(i: int) -> bool | None:
        v = _int(i)
        return None if v is None else bool(v)

    def _str(i: int) -> str | None:
        try:
            return parts[i] or None
        except IndexError:
            return None

    return ReceiverStatus(
        state=state,
        volume=_int(1),
        muted=_bool(2),
        master_volume_opt_in=_bool(3),
        hostname=_str(4),
        multicast=_str(5),
    )


def parse_mpd_pairs(lines: list[str]) -> dict[str, str]:
    """'key: value' lines -> dict (first occurrence wins)."""
    out: dict[str, str] = {}
    for line in lines:
        key, sep, val = line.partition(": ")
        if sep and key not in out:
            out[key] = val
    return out


def split_command_list(lines: list[str]) -> list[list[str]]:
    """Split a command_list_ok_begin response on 'list_OK' markers."""
    blocks: list[list[str]] = [[]]
    for line in lines:
        if line == "list_OK":
            blocks.append([])
        else:
            blocks[-1].append(line)
    if not blocks[-1]:
        blocks.pop()
    return blocks


def _unescape(val: Any) -> str | None:
    """Undo HTML escaping in moOde metadata (sometimes applied twice)."""
    if val is None:
        return None
    text = str(val)
    for _ in range(2):
        clean = html.unescape(text)
        if clean == text:
            break
        text = clean
    return text


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _yes(v: Any) -> bool:
    return str(v).strip().lower() in ("yes", "1", "true", "on")


def mpd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class LibraryItem:
    kind: str  # "directory" | "file" | "playlist"
    path: str
    title: str
    artist: str | None = None


def parse_lsinfo(lines: list[str]) -> list[LibraryItem]:
    items: list[LibraryItem] = []
    for line in lines:
        key, sep, val = line.partition(": ")
        if not sep:
            continue
        if key in ("directory", "file", "playlist"):
            items.append(LibraryItem(kind=key, path=val, title=val.rsplit("/", 1)[-1]))
        elif items and items[-1].kind == "file":
            if key == "Title":
                items[-1].title = val
            elif key == "Artist" and items[-1].artist is None:
                items[-1].artist = val
    return items


def parse_list(lines: list[str], key: str) -> list[str]:
    prefix = f"{key}: "
    out = [ln[len(prefix):] for ln in lines if ln.startswith(prefix)]
    return sorted({v for v in out if v}, key=str.casefold)



# ---- music library (FLACs) ----------------------------------------------------------


def parse_songs(lines: list[str]) -> list[dict[str, str]]:
    """MPD song listing ('file:' starts each song) -> list of tag dicts (first value wins)."""
    songs: list[dict[str, str]] = []
    for line in lines:
        key, sep, val = line.partition(": ")
        if not sep:
            continue
        if key == "file":
            songs.append({"file": val})
        elif songs and key not in songs[-1]:
            songs[-1][key] = val
    return songs


def parse_grouped(lines: list[str], key: str, groups: tuple[str, ...]) -> list[dict[str, str]]:
    """Output of `list <key> group <g1> group <g2>`: group tags are printed when they change,
    each `<key>:` line is one result carrying the current group values."""
    current = {g: "" for g in groups}
    out: list[dict[str, str]] = []
    for line in lines:
        k, sep, val = line.partition(": ")
        if not sep and line.endswith(":"):
            k, val = line[:-1], ""
        elif not sep:
            continue
        if k in current:
            current[k] = val
        elif k == key:
            out.append({key: val, **current})
    return out


def _disc_track(song: dict[str, str]) -> tuple[int, int, str]:
    def num(v: str | None) -> int:
        try:
            return int(str(v or "0").split("/")[0])
        except ValueError:
            return 0
    return num(song.get("Disc")), num(song.get("Track")), song.get("file", "")


def song_out(song: dict[str, str]) -> dict[str, Any]:
    dur = _float(song.get("duration")) or _float(song.get("Time"))
    return {
        "file": song.get("file", ""),
        "title": song.get("Title") or song.get("file", "").rsplit("/", 1)[-1],
        "artist": song.get("Artist", ""),
        "album_artist": song.get("AlbumArtist") or song.get("Artist", ""),
        "album": song.get("Album", ""),
        "track": song.get("Track", ""),
        "disc": song.get("Disc", ""),
        "year": (song.get("Date") or "")[:4],
        "genre": song.get("Genre", ""),
        "duration": round(dur) if dur else None,
    }


_RELEASE_RE = re.compile(r"Release:\s*(\d+\.\d+\.\d+)")


def parse_release(html: str) -> str | None:
    """moOde's version from its web page: the About box says 'Release: 9.4.2 2025-…'
    (moOde's own tools read the same line)."""
    m = _RELEASE_RE.search(html or "")
    return m.group(1) if m else None


def artist_key(name: str) -> str:
    """Loose form of an artist name for matching: 'The Allman Brothers Band' ==
    'Allman Brothers Band', 'Simon & Garfunkel' == 'Simon and Garfunkel'."""
    n = name.casefold().replace("&", " and ")
    n = n[4:] if n.startswith("the ") else n
    return "".join(ch for ch in n if ch.isalnum())


def deezer_pick(name: str, data: Any) -> str | None:
    """From a Deezer artist search, the photo URL of the artist with exactly this name.
    Never a near miss (better no photo than the wrong band), never Deezer's placeholder."""
    want = artist_key(name)
    if not want or not isinstance(data, dict):
        return None
    for hit in data.get("data") or []:
        if not isinstance(hit, dict) or artist_key(str(hit.get("name", ""))) != want:
            continue
        if not hit.get("md5_image"):  # Deezer has no photo for this artist
            return None
        url = hit.get("picture_medium") or hit.get("picture_big")
        return url if isinstance(url, str) and url.startswith("https://") else None
    return None


STATION_DEFAULTS: dict[str, str] = {
    "type": "r", "genre": "", "broadcaster": "", "language": "", "country": "",
    "region": "", "bitrate": "", "format": "", "geo_fenced": "No", "home_page": "",
    "monitor": "No",
}


def clean_station_name(name: str) -> str:
    """moOde stores a station as RADIO/<name>.pls: keep names filesystem-safe."""
    name = " ".join(str(name).replace("/", "-").replace("\\", "-").split())
    return name.strip(" .")[:100]


# --------------------------------------------------------------------------- client


class MoodeClient:
    """Talks to one moOde box."""

    def __init__(self, host: str, session: Any, mpd_port: int = DEFAULT_MPD_PORT) -> None:
        self.host = host
        self.mpd_port = mpd_port
        self._session = session
        self._mpd_lock = asyncio.Lock()
        self.announce_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}"

    # ---- REST ----------------------------------------------------------------

    async def command(self, cmd: str) -> Any:
        """Run a REST API command and return parsed JSON (or text)."""
        url = f"{self.base_url}/command/?cmd={quote(cmd)}"
        kwargs: dict[str, Any] = {}
        if ClientTimeout is not None:
            kwargs["timeout"] = ClientTimeout(total=TIMEOUT)
        try:
            async with self._session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    raise MoodeError(f"{cmd}: HTTP {resp.status}")
                return parse_rest_body(await resp.text())
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            raise MoodeError(f"{cmd}: {err}") from err

    # ---- radio stations (moOde's own Radio Manager endpoints) -----------------

    async def radio(self, cmd: str, data: dict[str, Any] | None = None, timeout: float = 15,
                    headers: dict[str, str] | None = None) -> Any:
        """GET (no data) or POST (form data) to /command/radio.php?cmd=<cmd>."""
        url = f"{self.base_url}/command/radio.php?cmd={quote(cmd)}"
        kwargs: dict[str, Any] = {"headers": {"X-Requested-With": "XMLHttpRequest", **(headers or {})}}
        if ClientTimeout is not None:
            kwargs["timeout"] = ClientTimeout(total=timeout)
        try:
            if data is None:
                ctx = self._session.get(url, **kwargs)
            else:
                ctx = self._session.post(url, data=data, **kwargs)
            async with ctx as resp:
                if resp.status != 200:
                    raise MoodeError(f"radio {cmd}: HTTP {resp.status}")
                return parse_rest_body(await resp.text())
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            raise MoodeError(f"radio {cmd}: {err}") from err

    async def get_stations(self, attempts: int = 4) -> list[dict[str, Any]]:
        """All stations. moOde can answer with a status text (e.g. while its worker is
        busy right after a change); then wait and ask again rather than return nothing."""
        last: Any = None
        for i in range(attempts):
            data = await self.radio("get_stations")
            if isinstance(data, dict):  # some versions key rows by id
                data = list(data.values())
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict) and r.get("name")]
            last = data
            await asyncio.sleep(1.5 * (i + 1))
        raise MoodeError(f"moOde didn't return the station list (it said: {str(last)[:120]})")

    async def add_station(self, fields: dict[str, Any]) -> Any:
        """Create a station. fields: name, url (+ optional genre, country, bitrate, ...)."""
        row = {**STATION_DEFAULTS, **{k: v for k, v in fields.items() if v is not None}}
        row["name"] = clean_station_name(row["name"])
        if not row["name"] or not str(row.get("url", "")).startswith(("http://", "https://")):
            raise MoodeError("A station needs a name and an http(s) stream URL")
        return await self.radio("new_station", {f"path[{k}]": str(v) for k, v in row.items()})

    async def update_station(self, row: dict[str, Any], changes: dict[str, Any]) -> Any:
        """Change an existing station's URL/details (not its name: see rename in stations.py).
        row is the station's current record from get_stations()."""
        merged = {**STATION_DEFAULTS}
        for k in STATION_DEFAULTS:
            if row.get(k) not in (None, ""):
                merged[k] = row[k]
        merged.update({k: v for k, v in changes.items() if v is not None})
        merged["id"] = row.get("id")
        merged["name"] = row["name"]
        merged["url"] = changes.get("url") or row.get("station", "")
        reply = await self.radio("upd_station", {f"path[{k}]": str(v) for k, v in merged.items()})
        if reply not in (None, "", "OK"):
            raise MoodeError(str(reply))
        return reply

    async def delete_station(self, name: str) -> Any:
        return await self.radio("del_station", {"path": f"RADIO/{name}.pls"})

    async def stage_station_logo(self, name: str, image: str) -> bool:
        """Hand moOde a logo for station `name`. moOde's worker writes it (when it gets to
        the job) to radio-logos/__tmp__<name>.jpg, and moOde moves that file into place on
        the NEXT new_station/upd_station for that name. So: submit, WAIT until the temp
        file exists, then the caller saves the station. Returns True once it's staged.
        `image` may be a data: URL or plain base64; moOde wants plain base64."""
        b64 = image.split(",", 1)[1] if image.startswith("data:") else image
        cookie = await self._moode_session_cookie()
        for _ in range(15):
            reply = str(await self.radio("set_ralogo_image", {"name": name, "blob": b64},
                                         headers=cookie) or "")
            if "busy" not in reply.lower():
                break
            await asyncio.sleep(1)
        else:
            _LOGGER.warning("%s: moOde's worker stayed busy; logo for %s not set", self.host, name)
            return False
        for _ in range(30):  # the worker picks jobs up on its own loop; allow up to ~30 s
            if await self._url_exists(self.tmp_logo_url(name)):
                return True
            await asyncio.sleep(1)
        _LOGGER.warning("%s: moOde didn't write the logo for %s (reply %r, session cookie %s)",
                        self.host, name, reply, "sent" if cookie else "NOT available")
        return False

    async def wait_logo_applied(self, name: str, timeout: int = 15) -> bool:
        """After saving the station, moOde moves the temp logo into place (after ~3 s)."""
        for _ in range(timeout):
            if not await self._url_exists(self.tmp_logo_url(name)):
                return True
            await asyncio.sleep(1)
        return False

    async def _url_exists(self, url: str) -> bool:
        kwargs: dict[str, Any] = {}
        if ClientTimeout is not None:
            kwargs["timeout"] = ClientTimeout(total=5)
        try:
            async with self._session.get(url, **kwargs) as resp:
                return resp.status == 200
        except (ClientError, asyncio.TimeoutError, OSError):
            return False

    async def _moode_session_cookie(self) -> dict[str, str]:
        """moOde passes jobs to its worker through the PHP session, like its own web page
        does with the browser's cookie. Open moOde's page once to get that cookie."""
        kwargs: dict[str, Any] = {}
        if ClientTimeout is not None:
            kwargs["timeout"] = ClientTimeout(total=10)
        try:
            async with self._session.get(f"{self.base_url}/index.php", **kwargs) as resp:
                await resp.read()
                sid = None
                if getattr(resp, "cookies", None) and "PHPSESSID" in resp.cookies:
                    sid = resp.cookies["PHPSESSID"].value
                else:
                    for h in resp.headers.getall("Set-Cookie", []) if hasattr(resp.headers, "getall") else []:
                        if h.startswith("PHPSESSID="):
                            sid = h.split(";", 1)[0].split("=", 1)[1]
                if sid:
                    return {"Cookie": f"PHPSESSID={sid}"}
                _LOGGER.debug("%s: moOde sent no session cookie", self.host)
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.debug("%s: couldn't open moOde's page for a session: %s", self.host, err)
        return {}

    async def get_release(self) -> str | None:
        """The moOde version installed on the box, e.g. '9.4.2' (None if unknown)."""
        kwargs: dict[str, Any] = {}
        if ClientTimeout is not None:
            kwargs["timeout"] = ClientTimeout(total=10)
        try:
            async with self._session.get(f"{self.base_url}/index.php", **kwargs) as resp:
                if resp.status != 200:
                    return None
                return parse_release((await resp.read()).decode("utf-8", "replace"))
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.debug("%s: couldn't read the moOde version: %s", self.host, err)
            return None

    def tmp_logo_url(self, name: str) -> str:
        return f"{self.base_url}/imagesw/radio-logos/__tmp__{quote(name, safe='')}.jpg"

    async def refresh_radio_library(self) -> None:
        """Make MPD notice added/removed .pls files (only rescans the RADIO folder)."""
        try:
            await self.mpd('update "RADIO"')
        except MoodeError as err:
            _LOGGER.debug("%s: MPD update RADIO failed: %s", self.host, err)

    def logo_url(self, name: str, thumb: bool = True) -> str:
        sub = "thumbs/" if thumb else ""
        return f"{self.base_url}/imagesw/radio-logos/{sub}{quote(name, safe='')}.jpg"

    async def get_volume(self) -> tuple[int | None, bool]:
        vol, muted, _can = await self.get_volume_state()
        return vol, muted

    async def get_volume_state(self) -> tuple[int | None, bool, bool | None]:
        """(volume, muted, can_volume). moOde 10 says whether the volume can be changed
        (False for Volume type 'Fixed (0dB)' or a renderer like Spotify; the number is then
        just the saved knob). Older moOde doesn't say: can_volume is None."""
        data = await self.command("get_volume")
        if not isinstance(data, dict) or "volume" not in data:
            raise MoodeError(f"Unexpected get_volume answer: {data!r}")
        can = data.get("can_volume")
        can_volume = None if can is None else (can if isinstance(can, bool) else _yes(can))
        return _int_or_none(data.get("volume")), _yes(data.get("muted")), can_volume

    async def set_volume(self, level: int) -> None:
        await self.command(f"set_volume {max(0, min(100, int(level)))}")

    async def volume_step(self, up: bool, step: int = 2) -> None:
        await self.command(f"set_volume {'-up' if up else '-dn'} {step}")

    async def toggle_mute(self) -> None:
        await self.command("set_volume -mute")

    async def get_currentsong(self) -> dict[str, Any] | None:
        """Needs 'Metadata file' enabled in moOde; returns None otherwise."""
        try:
            data = await self.command("get_currentsong")
        except MoodeError:
            return None
        return data if isinstance(data, dict) and data else None

    async def get_receiver_status(self) -> ReceiverStatus:
        try:
            return parse_receiver_status(await self.command("get_receiver_status"))
        except MoodeError:
            return ReceiverStatus(state="Unknown")

    async def set_receiver(self, on: bool) -> ReceiverStatus:
        return parse_receiver_status(
            await self.command(f"set_receiver_onoff {'-on' if on else '-off'}")
        )

    async def play_item(self, item: str) -> None:
        await self.command(f"play_item {item}")

    # ---- MPD -----------------------------------------------------------------

    async def mpd(self, *commands: str) -> list[list[str]]:
        """Send one or more MPD commands; return one list of lines per command."""
        async with self._mpd_lock:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.mpd_port), TIMEOUT
                )
            except (OSError, asyncio.TimeoutError) as err:
                raise MoodeError(f"MPD connect: {err}") from err
            try:
                hello = await asyncio.wait_for(reader.readline(), TIMEOUT)
                if not hello.startswith(b"OK MPD"):
                    raise MoodeError(f"Not an MPD server: {hello!r}")
                if len(commands) == 1:
                    payload = commands[0] + "\n"
                else:
                    payload = "command_list_ok_begin\n" + "\n".join(commands) + "\ncommand_list_end\n"
                writer.write(payload.encode())
                await writer.drain()

                lines: list[str] = []
                while True:
                    raw = await asyncio.wait_for(reader.readline(), TIMEOUT)
                    if not raw:
                        raise MoodeError("MPD closed the connection")
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line == "OK":
                        break
                    if line.startswith("ACK"):
                        raise MoodeError(f"MPD error: {line}")
                    lines.append(line)
            except asyncio.TimeoutError as err:
                raise MoodeError("MPD timeout") from err
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

        return [lines] if len(commands) == 1 else split_command_list(lines)

    # ---- push updates (MPD idle) --------------------------------------------------

    async def idle_forever(self, on_change, subsystems: str = "player mixer options playlist") -> None:
        """Keep a dedicated MPD connection in `idle` and call on_change(set_of_subsystems)
        whenever MPD reports a change. Reconnects with back-off; runs until cancelled."""
        backoff = 5
        while True:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.mpd_port), TIMEOUT
                )
                hello = await asyncio.wait_for(reader.readline(), TIMEOUT)
                if not hello.startswith(b"OK MPD"):
                    raise MoodeError("not MPD")
                backoff = 5
                while True:
                    writer.write(f"idle {subsystems}\n".encode())
                    await writer.drain()
                    changed: set[str] = set()
                    while True:
                        raw = await reader.readline()  # no timeout: idle waits for changes
                        if not raw:
                            raise MoodeError("MPD closed the idle connection")
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if line == "OK":
                            break
                        if line.startswith("ACK"):
                            raise MoodeError(line)
                        if line.startswith("changed: "):
                            changed.add(line[9:])
                    if changed:
                        on_change(changed)
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError, MoodeError) as err:
                _LOGGER.debug("%s: MPD idle connection lost (%s); retrying in %ss", self.host, err, backoff)
            finally:
                if writer is not None:
                    writer.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ---- transport -----------------------------------------------------------

    async def play(self) -> None:
        await self.mpd("play")

    async def pause(self, is_radio: bool) -> None:
        # moOde's own UI stops radio streams and pauses files.
        await self.mpd("stop" if is_radio else "pause 1")

    async def stop(self) -> None:
        await self.mpd("stop")

    async def next(self) -> None:
        await self.mpd("next")

    async def previous(self) -> None:
        await self.mpd("previous")

    async def seek(self, position: float) -> None:
        await self.mpd(f"seekcur {position:.1f}")

    # ---- library ---------------------------------------------------------------

    async def lsinfo(self, path: str = "") -> list[LibraryItem]:
        cmd = f"lsinfo {mpd_quote(path)}" if path else "lsinfo"
        items = parse_lsinfo((await self.mpd(cmd))[0])
        # At the root, lsinfo also lists saved playlists (browsed separately).
        # Inside folders, playlist entries are .pls/.m3u files, e.g. radio stations.
        return [i for i in items if i.kind != "playlist"] if not path else items

    async def playlists(self) -> list[str]:
        return parse_list((await self.mpd("listplaylists"))[0], "playlist")

    async def artists(self) -> list[str]:
        return parse_list((await self.mpd("list albumartist"))[0], "AlbumArtist")

    async def albums(self, artist: str) -> list[str]:
        return parse_list((await self.mpd(f"list album albumartist {mpd_quote(artist)}"))[0], "Album")

    async def play_album(self, artist: str, album: str) -> None:
        await self.mpd(
            "clear",
            f"findadd albumartist {mpd_quote(artist)} album {mpd_quote(album)}",
            "play",
        )

    async def play_artist(self, artist: str) -> None:
        await self.mpd("clear", f"findadd albumartist {mpd_quote(artist)}", "play")

    async def all_albums(self) -> list[str]:
        return parse_list((await self.mpd("list album"))[0], "Album")

    async def play_album_any(self, album: str) -> None:
        await self.mpd("clear", f"findadd album {mpd_quote(album)}", "play")

    # ---- music library (FLACs) ----------------------------------------------------

    async def lib_stats(self) -> dict[str, int]:
        st = parse_mpd_pairs((await self.mpd("stats"))[0])
        return {k: int(st.get(k, 0) or 0) for k in ("songs", "artists", "albums")}

    async def lib_artists(self, genre: str | None = None) -> list[str]:
        cmd = "list albumartist" + (f" genre {mpd_quote(genre)}" if genre else "")
        return parse_list((await self.mpd(cmd))[0], "AlbumArtist")

    async def lib_genres(self) -> list[str]:
        return parse_list((await self.mpd("list genre"))[0], "Genre")

    async def lib_albums(self, artist: str | None = None, genre: str | None = None) -> list[dict[str, str]]:
        """Albums as {album, artist, year}; one artist's albums come oldest first."""
        filt = ""
        if artist is not None:
            filt += f" albumartist {mpd_quote(artist)}"
        if genre is not None:
            filt += f" genre {mpd_quote(genre)}"
        lines = (await self.mpd(f"list album{filt} group albumartist group date"))[0]
        seen: dict[tuple[str, str], dict[str, str]] = {}
        for row in parse_grouped(lines, "Album", ("AlbumArtist", "Date")):
            if not row["Album"]:
                continue
            k = (row["AlbumArtist"], row["Album"])
            year = (row["Date"] or "")[:4]
            if k not in seen:
                seen[k] = {"album": row["Album"], "artist": row["AlbumArtist"], "year": year}
            elif year and (not seen[k]["year"] or year < seen[k]["year"]):
                seen[k]["year"] = year
        albums = list(seen.values())
        if artist is not None:
            albums.sort(key=lambda a: (a["year"] or "9999", a["album"].casefold()))
        else:
            albums.sort(key=lambda a: (a["album"].casefold(), a["artist"].casefold()))
        return albums

    async def lib_tracks(self, artist: str, album: str) -> list[dict[str, Any]]:
        songs = parse_songs((await self.mpd(
            f"find albumartist {mpd_quote(artist)} album {mpd_quote(album)}"))[0])
        songs.sort(key=_disc_track)
        return [song_out(s) for s in songs]

    async def lib_first_file(self, artist: str, album: str) -> str | None:
        songs = parse_songs((await self.mpd(
            f"find albumartist {mpd_quote(artist)} album {mpd_quote(album)} window 0:1"))[0])
        return songs[0]["file"] if songs else None

    async def lib_search(self, query: str, limit: int = 300) -> list[dict[str, Any]]:
        songs = parse_songs((await self.mpd(f"search any {mpd_quote(query)} window 0:{limit}"))[0])
        return [song_out(s) for s in songs if not s["file"].startswith(("http://", "https://"))]

    async def lib_recent(self, max_albums: int = 48) -> list[dict[str, str]]:
        """Most recently added albums (by file modification time), newest first."""
        expr = mpd_quote('(modified-since "1970-01-01T00:00:00Z")')
        songs = parse_songs((await self.mpd(f"find {expr} sort -Last-Modified window 0:3000"))[0])
        out: dict[tuple[str, str], dict[str, str]] = {}
        for s in songs:
            album = s.get("Album")
            if not album or s["file"].startswith(("http://", "https://")):
                continue
            artist = s.get("AlbumArtist") or s.get("Artist", "")
            if (artist, album) not in out:
                out[(artist, album)] = {"album": album, "artist": artist,
                                        "year": (s.get("Date") or "")[:4]}
                if len(out) >= max_albums:
                    break
        return list(out.values())

    async def lib_play(self, what: str, *, artist: str | None = None, album: str | None = None,
                       genre: str | None = None, position: int = 0, shuffle: bool = False,
                       queue: bool = False) -> None:
        """Replace the queue with (or, queue=True, append) an album / artist / genre /
        the whole library, then play."""
        if what == "album" and artist is not None and album is not None:
            add = f"findadd albumartist {mpd_quote(artist)} album {mpd_quote(album)}"
        elif what == "artist" and artist is not None:
            add = f"findadd albumartist {mpd_quote(artist)}"
        elif what == "genre" and genre is not None:
            add = f"findadd genre {mpd_quote(genre)}"
        elif what == "all":
            add = 'add ""'
        else:
            raise MoodeError(f"Nothing to play for {what!r}")
        if queue:
            st = parse_mpd_pairs((await self.mpd("status"))[0])
            await self.mpd(add)
            if st.get("state") != "play":
                await self.mpd("play")
            return
        cmds = ["clearerror", "clear", add]
        if shuffle:
            cmds.append("shuffle")
        cmds.append(f"play {max(0, int(position))}" if not shuffle else "play")
        await self.mpd(*cmds)

    def cover_url(self, file: str) -> str:
        """moOde's cover art for a track: embedded art, or the folder image next to it."""
        return f"{self.base_url}/coverart.php/{quote(file, safe='')}"

    async def announce(self, url: str, *, max_seconds: float, lead_in: float, resume: bool) -> None:
        """Play `url` once, then put things back the way they were."""
        before = parse_mpd_pairs((await self.mpd("status"))[0])
        was, pos, elapsed = before.get("state"), before.get("song"), _float(before.get("elapsed"))
        song = parse_mpd_pairs((await self.mpd("currentsong"))[0])
        was_radio = str(song.get("file", "")).startswith(("http://", "https://"))

        added = parse_mpd_pairs((await self.mpd(f"addid {mpd_quote(url)}"))[0])
        song_id = added.get("Id")
        if song_id is None:
            raise MoodeError(f"MPD refused announcement URL: {url}")
        try:
            if was == "play":
                await self.mpd("pause 1" if not was_radio else "stop")
            if lead_in:
                await asyncio.sleep(lead_in)  # let multiroom receivers lock on
            await self.mpd(f"playid {song_id}")
            loop = asyncio.get_running_loop()

            async def playing_it() -> bool:
                st = parse_mpd_pairs((await self.mpd("status"))[0])
                return st.get("songid") == song_id and st.get("state") == "play"

            # wait (max 10 s) for it to start, then until it's finished or max_seconds
            start_by = loop.time() + 10
            while loop.time() < start_by and not await playing_it():
                await asyncio.sleep(0.3)
            end_by = loop.time() + max_seconds
            while loop.time() < end_by and await playing_it():
                await asyncio.sleep(0.5)
        finally:
            try:
                await self.mpd(f"deleteid {song_id}")
            except MoodeError:
                pass
        if not resume or pos is None:
            await self.mpd("stop")
            return
        if was == "play":
            await self.mpd(f"play {pos}")
            if elapsed and not was_radio:
                await self.mpd(f"seekcur {elapsed:.1f}")
        elif was == "pause":
            cmds = [f"play {pos}"]
            if elapsed:
                cmds.append(f"seekcur {elapsed:.1f}")
            cmds.append("pause 1")
            await self.mpd(*cmds)
        else:
            await self.mpd("stop")

    def cover_for(self, path: str) -> str:
        return f"{self.base_url}/coverart.php/{quote(path)}"

    async def play_url(self, url: str) -> None:
        """Play a stream URL directly through MPD (works even if this box doesn't
        have the station in its own moOde radio list)."""
        await self.mpd("clearerror", "clear", f"add {mpd_quote(url)}", "play")

    async def is_playing(self) -> bool:
        return parse_mpd_pairs((await self.mpd("status"))[0]).get("state") == "play"

    async def mpd_error(self) -> str | None:
        return parse_mpd_pairs((await self.mpd("status"))[0]).get("error")

    async def stream_url(self, pls_path: str) -> str | None:
        """The stream URL inside a station's .pls (via MPD's playlist plugin)."""
        lines = (await self.mpd(f"listplaylist {mpd_quote(pls_path)}"))[0]
        for ln in lines:
            if ln.startswith("file: "):
                return ln[6:]
        return None

    def radio_logo(self, pls_path: str) -> str:
        name = pls_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return f"{self.base_url}/imagesw/radio-logos/thumbs/{quote(name)}.jpg"

    # ---- polling -------------------------------------------------------------

    async def validate(self) -> str | None:
        """Confirm this is moOde. Returns the box's hostname if it reports one."""
        await self.get_volume()  # moOde-specific endpoint
        rx = await self.get_receiver_status()
        return rx.hostname

    async def fetch_state(self) -> MoodeState:
        st = MoodeState()

        # MPD: the source of truth for transport + elapsed time.
        mpd_ok = True
        try:
            status_lines, song_lines = await self.mpd("status", "currentsong")
            status = parse_mpd_pairs(status_lines)
            song = parse_mpd_pairs(song_lines)
            st.state = status.get("state", "stop")
            st.mpd_error = status.get("error")
            # Fixed (0dB): MPD has no mixer -> reports -1, or (newer MPD) omits volume
            st.fixed_volume = status.get("volume") in (None, "-1")
            st.elapsed = _float(status.get("elapsed"))
            st.duration = _float(status.get("duration")) or _float(song.get("Time"))
            st.file = song.get("file")
            st.title = song.get("Title")
            st.artist = song.get("Artist")
            st.album = song.get("Album")
            st.station = song.get("Name")
        except MoodeError as err:
            mpd_ok = False
            _LOGGER.debug("%s: MPD poll failed: %s", self.host, err)

        # REST: volume + richer metadata. A REST hiccup (e.g. moOde answering HTTP 500)
        # only costs us the volume/metadata as long as MPD still answers.
        try:
            st.volume, st.muted, can_volume = await self.get_volume_state()
            if can_volume is not None:  # moOde 10 tells us; trust it over the MPD guess
                st.fixed_volume = not can_volume
        except MoodeError as err:
            if not mpd_ok:
                raise
            _LOGGER.debug("%s: REST volume failed, continuing on MPD only: %s", self.host, err)

        meta = await self.get_currentsong()
        if meta:
            st.raw = meta
            # moOde's REST metadata is HTML-escaped ("Push &amp; Shove")
            st.title = _unescape(meta.get("title")) or st.title
            st.artist = _unescape(meta.get("artist")) or st.artist
            st.album = _unescape(meta.get("album")) or st.album
            st.file = meta.get("file") or st.file
            if not mpd_ok and meta.get("state"):
                st.state = str(meta["state"])
            st.output_format = meta.get("outrate")
            f = str(meta.get("file") or "")
            if f.endswith(" Active"):  # e.g. "AirPlay Active", "Spotify Active"
                st.renderer = f[: -len(" Active")]
                st.state = "play"
            # Radio: moOde reports artist="Radio station", title="Artist - Track",
            # and the station name in album.
            if st.artist and st.artist.strip().lower() == "radio station":
                st.station = st.station or _unescape(meta.get("album"))
                left, sep, right = (st.title or "").partition(" - ")
                if sep and left.strip() and right.strip():
                    st.artist, st.title = left.strip(), right.strip()
                else:
                    st.artist = None
            cover = meta.get("coverurl")
            if cover:
                st.cover_url = cover if cover.startswith("http") else f"{self.base_url}/{cover.lstrip('/')}"

        st.receiver = await self.get_receiver_status()
        return st
