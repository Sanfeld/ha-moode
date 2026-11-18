"""Tests the moOde client against a simulated moOde box (fake MPD server + fake REST)."""
import asyncio
import importlib.util
import json
import pathlib
from urllib.parse import unquote

import sys

import pytest

_spec = importlib.util.spec_from_file_location(
    "moode_api", pathlib.Path(__file__).parents[2] / "custom_components/moode/api.py"
)
api = importlib.util.module_from_spec(_spec)
sys.modules['moode_api'] = api
_spec.loader.exec_module(api)


class FakeMoode:
    """Simulated moOde box state."""

    def __init__(self, metadata_file=True, receiver="rx,On,42,0,1,moodestreamer,239.0.0.1"):
        self.state = "play"
        self.volume = 30
        self.muted = False
        self.file = "http://ice1.somafm.com/groovesalad-128-mp3"
        self.metadata_file = metadata_file
        self.receiver = receiver
        self.mpd_volume = "30"
        self.mpd_log = []
        self.rest_log = []

    # REST
    def rest(self, cmd):
        self.rest_log.append(cmd)
        if cmd == "get_volume":
            return json.dumps({"volume": str(self.volume), "muted": "yes" if self.muted else "no"})
        if cmd.startswith("set_volume"):
            arg = cmd.split(" ", 1)[1]
            if arg == "-mute":
                self.muted = not self.muted
            elif arg.startswith("-up"):
                self.volume += int(arg.split()[1])
            elif arg.startswith("-dn"):
                self.volume -= int(arg.split()[1])
            else:
                self.volume = int(arg)
            return json.dumps({"volume": str(self.volume), "muted": "yes" if self.muted else "no"})
        if cmd == "get_currentsong":
            if not self.metadata_file:
                return ""
            return json.dumps({"file": self.file, "artist": "Soma FM", "title": "Groove Salad",
                               "album": "Radio", "coverurl": "imagesw/radio-logos/Soma FM.jpg",
                               "outrate": "16 bit, 44.1 kHz, Stereo", "state": self.state})
        if cmd == "get_receiver_status":
            return self.receiver
        if cmd.startswith("set_receiver_onoff"):
            on = cmd.endswith("-on")
            self.receiver = self.receiver.replace(",Off,", ",On,") if on else self.receiver.replace(",On,", ",Off,")
            return self.receiver
        if cmd.startswith("play_item "):
            self.file = cmd[len("play_item "):]
            self.state = "play"
            return json.dumps({"info": "OK"})
        return json.dumps({"error": "unknown"})

    # moOde radio.php
    stations = None

    def radio(self, cmd, data):
        if self.stations is None:
            self.stations = [{"id": "1", "name": "Stock FM", "station": "http://stock/fm", "type": "r"},
                             {"id": "501", "name": "My Station", "station": "https://mine/live", "type": "r"}]
        if cmd == "get_stations":
            return json.dumps(self.stations)
        if cmd == "new_station":
            if data["path[name]"] in getattr(self, "tmp_logos", set()):
                self.logos = getattr(self, "logos", set()) | {data["path[name]"]}
                self.tmp_logos.discard(data["path[name]"])   # moOde moves the temp file into place
            self.stations.append({"id": str(500 + len(self.stations)), "name": data["path[name]"],
                                  "station": data["path[url]"], "type": data["path[type]"]})
            return "OK"
        if cmd == "del_station":
            name = data["path"][len("RADIO/"):-len(".pls")]
            self.stations = [r for r in self.stations if r["name"] != name]
            return "OK"
        if cmd == "upd_station":
            for r in self.stations:
                if r["id"] == data["path[id]"]:
                    r["station"] = data["path[url]"]; r["genre"] = data["path[genre]"]
            return json.dumps("OK")
        if cmd == "set_ralogo_image":
            import base64 as _b
            try:  # like moOde's worker: strict base64, no data: prefix
                _b.b64decode(data["blob"], validate=True)
            except Exception:
                self.bad_logo = True
                return "job submitted"
            self.tmp_logos = getattr(self, "tmp_logos", set()) | {data["name"]}
            return "job submitted"
        return "Unknown command"

    # MPD
    def mpd(self, line):
        self.mpd_log.append(line)
        if line == "status":
            out = [f"volume: {self.mpd_volume}", f"state: {self.state}", "elapsed: 12.5",
                   "duration: 0", "song: 3", f"songid: {getattr(self, 'songid', '7')}"]
            if getattr(self, "ann_polls", None) is not None and getattr(self, "songid", None) == "99":
                self.ann_polls -= 1
                if self.ann_polls <= 0:  # announcement finished
                    self.state, self.songid = "stop", "7"
            return out
        if line.startswith("addid "):
            self.ann_url = line[6:]
            return ["Id: 99"]
        if line == "playid 99":
            self.state, self.songid, self.ann_polls = "play", "99", 3
            return []
        if line == "currentsong":
            return [f"file: {self.file}", "Name: SomaFM Groove Salad", "Title: Some Track"]
        if line == 'lsinfo "RADIO"':
            return ["directory: RADIO/Rock", "playlist: RADIO/Soma FM - Fluid.pls", "Last-Modified: x"]
        if line == 'listplaylist "RADIO/P4 Kalmar.pls"':
            return ["file: http://sverigesradio.se/topsy/direkt/207-hi.mp3"]
        if line == "listplaylists":
            return ["playlist: Favorites", "Last-Modified: x", "playlist: Dinner"]
        if line == "list albumartist":
            return ["AlbumArtist: Black Sabbath", "AlbumArtist: AC/DC"]
        if line == 'list album albumartist "AC/DC"':
            return ["Album: Back in Black", "Album: Highway to Hell"]
        if line == "play" or line.startswith("play "):
            self.state = "play"
        elif line == "stop":
            self.state = "stop"
        elif line == "pause 1":
            self.state = "pause"
        return []


class _Morsel:
    def __init__(self, v):
        self.value = v


class FakeResp:
    def __init__(self, body, status=200, cookies=None):
        self.status, self._body = status, body
        self.cookies = cookies or {}
        self.headers = {}

    async def read(self):
        return self._body.encode()

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, box):
        self.box = box
        self.posts = []

    def get(self, url, **kw):
        if url.endswith("/index.php"):
            return FakeResp("<html>moOde</html>", cookies={"PHPSESSID": _Morsel("abc123")})
        if "/imagesw/radio-logos/__tmp__" in url:
            name = unquote(url.rsplit("__tmp__", 1)[1])[:-4]
            return FakeResp("", 200 if name in getattr(self.box, "tmp_logos", set()) else 404)
        cmd = unquote(url.split("cmd=", 1)[1])
        if "/radio.php" in url:
            return FakeResp(self.box.radio(cmd, None))
        return FakeResp(self.box.rest(cmd))

    def post(self, url, data=None, **kw):
        cmd = unquote(url.split("cmd=", 1)[1])
        self.posts.append((cmd, data))
        if cmd == "set_ralogo_image":
            self.logo_headers = kw.get("headers", {})
        return FakeResp(self.box.radio(cmd, data))


async def start_mpd(box):
    async def handle(reader, writer):
        writer.write(b"OK MPD 0.24.0\n")
        await writer.drain()
        in_list, results = False, []
        while (raw := await reader.readline()):
            line = raw.decode().rstrip("\n")
            if line.startswith("idle"):
                box.mpd_log.append(line)
                if getattr(box, "idle_sent", 0) < 2:
                    box.idle_sent = getattr(box, "idle_sent", 0) + 1
                    writer.write(b"changed: player\nchanged: mixer\nOK\n"); await writer.drain()
                continue  # afterwards: stay idle (no reply), like a quiet MPD
            if line == "command_list_ok_begin":
                in_list, results = True, []
                continue
            if line == "command_list_end":
                out = ""
                for r in results:
                    out += "".join(f"{x}\n" for x in r) + "list_OK\n"
                writer.write((out + "OK\n").encode())
                await writer.drain()
                in_list = False
                continue
            res = box.mpd(line)
            if in_list:
                results.append(res)
            else:
                writer.write(("".join(f"{x}\n" for x in res) + "OK\n").encode())
                await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def run(coro):
    return asyncio.run(coro)


def make(box):
    async def inner():
        server, port = await start_mpd(box)
        client = api.MoodeClient("127.0.0.1", FakeSession(box), mpd_port=port)
        return server, client
    return inner


# ---------------------------------------------------------------- parsing


def test_receiver_parsing_documented_example():
    rx = api.parse_receiver_status("rx,-Off,15,0,1,sig,239.0.0.1")
    assert (rx.state, rx.volume, rx.muted, rx.master_volume_opt_in, rx.hostname, rx.multicast) == (
        "Off", 15, False, True, "sig", "239.0.0.1")
    assert rx.enabled


def test_receiver_parsing_disabled_and_garbage():
    assert api.parse_receiver_status("rx,Disabled").state == "Disabled"
    assert not api.parse_receiver_status("rx,Disabled").enabled
    assert api.parse_receiver_status("").state == "Unknown"
    assert api.parse_receiver_status(None).state == "Unknown"
    assert api.parse_receiver_status({"status": "rx,On,5,1,0,k,239.0.0.1"}).state == "On"


def test_command_list_split():
    assert api.split_command_list(["a: 1", "list_OK", "b: 2", "list_OK"]) == [["a: 1"], ["b: 2"]]


# ---------------------------------------------------------------- simulated box


def test_fetch_state_full():
    box = FakeMoode()

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.fetch_state()

    st = run(go())
    assert st.state == "play" and st.volume == 30 and st.muted is False
    assert st.title == "Groove Salad" and st.station == "SomaFM Groove Salad"
    assert st.is_radio
    assert st.cover_url == "http://127.0.0.1/imagesw/radio-logos/Soma FM.jpg"
    assert st.receiver.state == "On" and st.receiver.hostname == "moodestreamer"
    assert st.elapsed == 12.5


def test_fetch_state_without_metadata_file_falls_back_to_mpd():
    box = FakeMoode(metadata_file=False, receiver="rx,Disabled")

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.fetch_state()

    st = run(go())
    assert st.title == "Some Track" and st.cover_url is None
    assert st.receiver.state == "Disabled"


def test_validate_returns_hostname():
    box = FakeMoode()

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.validate()

    assert run(go()) == "moodestreamer"


def test_controls():
    box = FakeMoode()

    async def go():
        server, c = await make(box)()
        async with server:
            await c.pause(is_radio=True)
            assert box.state == "stop"          # radio -> stop
            await c.play()
            box.file = "NAS/Music/a.flac"
            await c.pause(is_radio=False)
            assert box.state == "pause"         # file -> pause
            await c.set_volume(150)
            assert box.volume == 100            # clamped
            await c.volume_step(False, 4)
            assert box.volume == 96
            await c.toggle_mute()
            assert box.muted
            rx = await c.set_receiver(False)
            assert rx.state == "Off"
            await c.play_item("RADIO/Soma FM - Fluid.pls")
            assert box.file == "RADIO/Soma FM - Fluid.pls"

    run(go())


def test_mpd_unreachable_raises():
    async def go():
        c = api.MoodeClient("127.0.0.1", FakeSession(FakeMoode()), mpd_port=1)
        with pytest.raises(api.MoodeError):
            await c.mpd("status")
    run(go())


def test_non_moode_rejected():
    class NotMoode(FakeMoode):
        def rest(self, cmd):
            return "<html>hello</html>"

    async def go():
        c = api.MoodeClient("127.0.0.1", FakeSession(NotMoode()))
        with pytest.raises(api.MoodeError):
            await c.validate()
    run(go())


def test_radio_artist_title_split():
    box = FakeMoode()
    orig = box.rest

    def rest(cmd):
        if cmd == "get_currentsong":
            return json.dumps({"file": box.file, "artist": "Radio station", "title": "Betontod - Küss Mich",
                               "album": "MetalFM", "state": "play"})
        return orig(cmd)
    box.rest = rest

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.fetch_state()

    st = run(go())
    assert (st.artist, st.title) == ("Betontod", "Küss Mich")
    assert st.station == "SomaFM Groove Salad"  # MPD Name wins when present


def test_html_escaped_metadata_is_cleaned():
    box = FakeMoode()
    orig = box.rest

    def rest(cmd):
        if cmd == "get_currentsong":
            return json.dumps({"file": box.file, "artist": "Radio station",
                               "title": "Mike Z Band - Push &amp; Shove",
                               "album": "Rock &amp;amp; Roll FM", "state": "play"})
        return orig(cmd)
    box.rest = rest

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.fetch_state()

    st = run(go())
    assert (st.artist, st.title) == ("Mike Z Band", "Push & Shove")
    assert api._unescape("Rock &amp;amp; Roll") == "Rock & Roll"
    assert api._unescape("AT&T 5 < 6") == "AT&T 5 < 6"


def test_fixed_volume_detected():
    box = FakeMoode()

    async def go():
        server, c = await make(box)()
        async with server:
            a = await c.fetch_state()
            box.mpd_volume = "-1"
            b = await c.fetch_state()
            return a, b

    a, b = run(go())
    assert a.fixed_volume is False and b.fixed_volume is True


def test_fixed_volume_when_mpd_omits_volume():
    box = FakeMoode()
    orig = box.mpd

    def mpd(line):
        out = orig(line)
        return [x for x in out if not x.startswith("volume:")] if line == "status" else out
    box.mpd = mpd

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.fetch_state()

    assert run(go()).fixed_volume is True


def test_airplay_renderer_detected():
    box = FakeMoode()
    orig = box.rest

    def rest(cmd):
        if cmd == "get_currentsong":
            return json.dumps({"file": "AirPlay Active", "outrate": "16 bit, 44.1 kHz"})
        return orig(cmd)
    box.rest = rest
    box.state = "stop"

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.fetch_state()

    st = run(go())
    assert st.renderer == "AirPlay" and st.state == "play"


def test_library():
    box = FakeMoode()

    async def go():
        server, c = await make(box)()
        async with server:
            radio = await c.lsinfo("RADIO")
            pls = await c.playlists()
            arts = await c.artists()
            albs = await c.albums("AC/DC")
            await c.play_album("AC/DC", 'Back in "Black"')
            return radio, pls, arts, albs

    radio, pls, arts, albs = run(go())
    assert [i.kind for i in radio] == ["directory", "playlist"]
    assert radio[1].path == "RADIO/Soma FM - Fluid.pls"
    assert pls == ["Dinner", "Favorites"]
    assert arts == ["AC/DC", "Black Sabbath"]
    assert albs == ["Back in Black", "Highway to Hell"]
    assert box.mpd_log[-3:] == ["clear", 'findadd albumartist "AC/DC" album "Back in \\"Black\\""', "play"]


def test_announce_resumes_radio():
    box = FakeMoode()  # playing radio, song 3

    async def go():
        server, c = await make(box)()
        async with server:
            await c.announce("http://ha:8123/api/tts_proxy/x.mp3", max_seconds=20, lead_in=0, resume=True)

    run(go())
    log = box.mpd_log
    assert 'addid "http://ha:8123/api/tts_proxy/x.mp3"' in log
    assert log.index("stop") < log.index("playid 99")        # radio stopped first
    assert "deleteid 99" in log
    assert log[-1] == "play 3"                                # back on the station
    assert box.state == "play"


def test_announce_resumes_file_position():
    box = FakeMoode()
    box.file = "NAS/Music/a.flac"

    async def go():
        server, c = await make(box)()
        async with server:
            await c.announce("http://x/a.mp3", max_seconds=20, lead_in=0, resume=True)

    run(go())
    assert box.mpd_log[-2:] == ["play 3", "seekcur 12.5"]


def test_announce_no_resume_stops():
    box = FakeMoode()

    async def go():
        server, c = await make(box)()
        async with server:
            await c.announce("http://x/a.mp3", max_seconds=20, lead_in=0, resume=False)

    run(go())
    assert box.mpd_log[-1] == "stop"


def test_rest_failure_keeps_box_available_via_mpd():
    box = FakeMoode()
    box.rest = lambda cmd: "<html>500</html>"   # moOde web API broken

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.fetch_state()

    st = run(go())
    assert st.state == "play" and st.volume is None


def test_everything_down_raises():
    async def go():
        c = api.MoodeClient("127.0.0.1", FakeSession(FakeMoode()), mpd_port=1)
        c._session = type("S", (), {"get": lambda self, url, **k: (_ for _ in ()).throw(OSError("down"))})()
        with pytest.raises(api.MoodeError):
            await c.fetch_state()
    run(go())


def test_stream_url_from_pls():
    box = FakeMoode()

    async def go():
        server, c = await make(box)()
        async with server:
            return await c.stream_url("RADIO/P4 Kalmar.pls")

    assert run(go()) == "http://sverigesradio.se/topsy/direkt/207-hi.mp3"


def test_play_url_direct_via_mpd():
    box = FakeMoode()
    box.state = "stop"

    async def go():
        server, c = await make(box)()
        async with server:
            await c.play_url("http://sverigesradio.se/topsy/direkt/207-hi.mp3")
            return await c.is_playing()

    assert run(go()) is True
    assert box.mpd_log[-4:-1] == ["clear", 'add "http://sverigesradio.se/topsy/direkt/207-hi.mp3"', "play"]


def test_station_manager_api():
    box = FakeMoode()
    sess = FakeSession(box)

    async def go():
        server, _ = await make(box)()
        async with server:
            c = api.MoodeClient("127.0.0.1", sess, mpd_port=server.sockets[0].getsockname()[1])
            rows = await c.get_stations()
            await c.add_station({"name": "AC/DC  Radio ", "url": "https://acdc/live", "genre": "rock"})
            with pytest.raises(api.MoodeError):
                await c.add_station({"name": "No URL", "url": "ftp://x"})
            await c.delete_station("Stock FM")
            await c.refresh_radio_library()
            return rows, await c.get_stations()

    before, after = run(go())
    assert [r["name"] for r in before] == ["Stock FM", "My Station"]
    assert [r["name"] for r in after] == ["My Station", "AC-DC Radio"]   # cleaned, slash-free name
    cmd, data = sess.posts[0]
    assert cmd == "new_station" and data["path[url]"] == "https://acdc/live" and data["path[geo_fenced]"] == "No"
    assert ("del_station", {"path": "RADIO/Stock FM.pls"}) in sess.posts
    assert 'update "RADIO"' in box.mpd_log


def test_clean_station_name():
    assert api.clean_station_name("  Rock / Metal  FM. ") == "Rock - Metal FM"


def test_get_stations_retries_while_moode_busy():
    box = FakeMoode()
    orig = box.radio
    calls = {"n": 0}

    def radio(cmd, data):
        if cmd == "get_stations":
            calls["n"] += 1
            if calls["n"] < 3:
                return "worker busy"
        return orig(cmd, data)
    box.radio = radio

    async def go():
        c = api.MoodeClient("127.0.0.1", FakeSession(box))
        return await c.get_stations()

    assert [r["name"] for r in run(go())] == ["Stock FM", "My Station"] and calls["n"] == 3


def test_idle_pushes_changes():
    box = FakeMoode()
    seen = []

    async def go():
        server, port = await start_mpd(box)
        async with server:
            c = api.MoodeClient("127.0.0.1", FakeSession(box), mpd_port=port)
            task = asyncio.create_task(c.idle_forever(seen.append))
            for _ in range(40):
                await asyncio.sleep(0.05)
                if len(seen) >= 2:
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(go())
    assert seen[:2] == [{"player", "mixer"}, {"player", "mixer"}]


def test_update_station_keeps_name_and_id():
    box = FakeMoode()
    sess = FakeSession(box)

    async def go():
        c = api.MoodeClient("127.0.0.1", sess)
        row = (await c.get_stations())[1]
        await c.update_station(row, {"url": "https://new/stream", "genre": "Jazz"})
        return await c.get_stations()

    rows = run(go())
    cmd, data = sess.posts[-1]
    assert cmd == "upd_station" and data["path[id]"] == "501" and data["path[name]"] == "My Station"
    assert rows[1]["station"] == "https://new/stream" and rows[1]["genre"] == "Jazz"


def test_logo_is_plain_base64_and_staged_before_create(monkeypatch):
    monkeypatch.setattr(api.asyncio, "sleep", _nosleep)
    box = FakeMoode()
    sess = FakeSession(box)

    async def go():
        c = api.MoodeClient("127.0.0.1", sess)
        staged = await c.stage_station_logo("P4 Kalmar", "data:image/jpeg;base64,/9j/4AAQSkZJRg==")
        await c.add_station({"name": "P4 Kalmar", "url": "https://sr/p4"})
        applied = await c.wait_logo_applied("P4 Kalmar")
        assert staged and applied

    run(go())
    assert not getattr(box, "bad_logo", False)
    assert "P4 Kalmar" in box.logos
    assert [p[0] for p in sess.posts] == ["set_ralogo_image", "new_station"]
    assert sess.logo_headers.get("Cookie") == "PHPSESSID=abc123"   # sent like moOde's own page


async def _nosleep(*_a, **_k):
    return None


def test_logo_urls_cannot_escape_the_logo_folder():
    c = api.MoodeClient("moode", None)
    assert c.logo_url("../../command/?cmd=stop") == "http://moode/imagesw/radio-logos/thumbs/..%2F..%2Fcommand%2F%3Fcmd%3Dstop.jpg"
    assert c.logo_url("P4 Kalmar", thumb=False) == "http://moode/imagesw/radio-logos/P4%20Kalmar.jpg"
