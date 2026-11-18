"""Media browser for a moOde box's library (folders, radio, playlists, artists/albums).

media_id scheme:
  dir:<path>              folder (browse)        file:<path>   track / station (play)
  pl:<name>               playlist (play)
  artists | artist:<a>    artist list / one artist's albums (browse)
  album:<a>\x1f<album>    album (play)
Anything without a known prefix is passed to moOde's play_item as-is.
"""
from __future__ import annotations

from homeassistant.components.media_player import BrowseMedia, MediaClass, MediaType
from homeassistant.exceptions import HomeAssistantError

from .api import MoodeClient, MoodeError

SEP = "\x1f"
LIMIT = 2000  # keep huge folders responsive


def _node(media_id, title, cls, *, play=False, expand=False, thumb=None, children=None, child_cls=None):
    return BrowseMedia(
        media_class=cls,
        media_content_id=media_id,
        media_content_type=MediaType.MUSIC,
        title=title,
        can_play=play,
        can_expand=expand,
        thumbnail=thumb,
        children=children,
        children_media_class=child_cls,
    )


async def async_browse(client: MoodeClient, media_id: str | None) -> BrowseMedia:
    try:
        return await _browse(client, media_id or "root")
    except MoodeError as err:
        raise HomeAssistantError(f"moOde library: {err}") from err


async def _browse(c: MoodeClient, mid: str) -> BrowseMedia:
    if mid == "root":
        return _node("root", "moOde library", MediaClass.DIRECTORY, expand=True,
                     child_cls=MediaClass.DIRECTORY, children=[
                         _node("dir:RADIO", "Radio", MediaClass.DIRECTORY, expand=True),
                         _node("artists", "Artists", MediaClass.DIRECTORY, expand=True),
                         _node("playlists", "Playlists", MediaClass.DIRECTORY, expand=True),
                         _node("dir:", "Folders", MediaClass.DIRECTORY, expand=True),
                     ])

    if mid.startswith("dir:"):
        path = mid[4:]
        items = (await c.lsinfo(path))[:LIMIT]
        is_radio = path.upper().startswith("RADIO")
        children = []
        for i in items:
            if i.kind == "directory":
                children.append(_node(f"dir:{i.path}", i.title, MediaClass.DIRECTORY,
                                      expand=True, play=not is_radio and bool(path),
                                      thumb=None if is_radio else c.cover_for(i.path)))
            else:
                title = i.title.rsplit(".", 1)[0] if is_radio else i.title
                children.append(_node(f"file:{i.path}", title,
                                      MediaClass.CHANNEL if is_radio else MediaClass.TRACK,
                                      play=True,
                                      thumb=c.radio_logo(i.path) if is_radio else None))
        title = "Folders" if not path else "Radio" if path == "RADIO" else path.rsplit("/", 1)[-1]
        return _node(mid, title, MediaClass.DIRECTORY, expand=True,
                     play=bool(path) and not is_radio, children=children,
                     thumb=c.cover_for(path) if path and not is_radio else None)

    if mid == "playlists":
        return _node(mid, "Playlists", MediaClass.DIRECTORY, expand=True,
                     child_cls=MediaClass.PLAYLIST,
                     children=[_node(f"pl:{p}", p, MediaClass.PLAYLIST, play=True)
                               for p in await c.playlists()])

    if mid == "artists":
        return _node(mid, "Artists", MediaClass.DIRECTORY, expand=True,
                     child_cls=MediaClass.ARTIST,
                     children=[_node(f"artist:{a}", a, MediaClass.ARTIST, expand=True)
                               for a in (await c.artists())[:LIMIT]])

    if mid.startswith("artist:"):
        artist = mid[7:]
        return _node(mid, artist, MediaClass.ARTIST, expand=True, child_cls=MediaClass.ALBUM,
                     children=[_node(f"album:{artist}{SEP}{al}", al, MediaClass.ALBUM, play=True)
                               for al in await c.albums(artist)])

    raise HomeAssistantError(f"Can't browse {mid}")


async def async_search(client: MoodeClient, text: str) -> list[BrowseMedia]:
    """Voice/search: favourite-style matches on stations, artists and albums."""
    q = text.strip().casefold()
    if not q:
        return []
    out: list[BrowseMedia] = []
    try:
        for i in await client.lsinfo("RADIO"):
            name = i.title.rsplit(".", 1)[0]
            if i.kind != "directory" and q in name.casefold():
                out.append(_node(f"file:{i.path}", name, MediaClass.CHANNEL, play=True,
                                 thumb=client.radio_logo(i.path)))
        for a in await client.artists():
            if q in a.casefold():
                out.append(_node(f"artistall:{a}", a, MediaClass.ARTIST, play=True))
        for al in await client.all_albums():
            if q in al.casefold():
                out.append(_node(f"albumany:{al}", al, MediaClass.ALBUM, play=True))
    except MoodeError as err:
        raise HomeAssistantError(f"moOde search: {err}") from err
    # exact matches first
    out.sort(key=lambda b: (b.title.casefold() != q, not b.title.casefold().startswith(q)))
    return out[:25]


async def async_play(client: MoodeClient, media_id: str) -> None:
    """Play a media_id from the browser (or a raw moOde path / URL)."""
    if media_id.startswith("artistall:"):
        await client.play_artist(media_id[10:])
    elif media_id.startswith("albumany:"):
        await client.play_album_any(media_id[9:])
    elif media_id.startswith("album:"):
        artist, _, album = media_id[6:].partition(SEP)
        await client.play_album(artist, album)
    elif media_id.startswith(("file:", "dir:")):
        await client.play_item(media_id.split(":", 1)[1])
    elif media_id.startswith("pl:"):
        await client.play_item(media_id[3:])
    else:
        await client.play_item(media_id)
