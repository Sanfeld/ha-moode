"""End-to-end tests inside a real Home Assistant instance."""
from homeassistant import config_entries
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.moode.api import MoodeError
from custom_components.moode.const import DOMAIN

from .conftest import make_state


async def _setup(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Streamer", unique_id="moodestreamer",
        data={CONF_HOST: "192.0.2.10"}, options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_user_flow_adds_box(hass: HomeAssistant, mock_moode) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.10"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Moodestreamer"
    assert result["result"].unique_id == "moodestreamer"


async def test_user_flow_cannot_connect(hass: HomeAssistant, mock_moode) -> None:
    mock_moode.validate.side_effect = MoodeError("no route")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.99"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_setup_creates_player(hass: HomeAssistant, mock_moode) -> None:
    await _setup(hass)
    state = hass.states.get("media_player.streamer")
    assert state is not None
    assert state.state == "playing"
    assert state.attributes["media_title"] == "Groove Salad"
    assert state.attributes["volume_level"] == 0.3


async def test_stopped_box_shows_no_old_song(hass: HomeAssistant, mock_moode) -> None:
    """A stopped box shows nothing of what MPD last played: no song, station or cover."""
    mock_moode.fetch_state.return_value = make_state(state="stop")
    await _setup(hass)
    state = hass.states.get("media_player.streamer")
    for attr in ("media_title", "media_artist", "media_album_name", "media_channel",
                 "media_duration", "media_position", "entity_picture"):
        assert attr not in state.attributes, attr


async def test_fixed_volume_hides_volume_controls(hass: HomeAssistant, mock_moode) -> None:
    await _setup(hass, options={"volume_mode": "fixed"})
    feats = hass.states.get("media_player.streamer").attributes["supported_features"]
    assert not feats & MediaPlayerEntityFeature.VOLUME_SET
    assert not feats & MediaPlayerEntityFeature.VOLUME_MUTE


async def test_volume_service_reaches_moode(hass: HomeAssistant, mock_moode) -> None:
    await _setup(hass)
    await hass.services.async_call(
        "media_player", "volume_set",
        {"entity_id": "media_player.streamer", "volume_level": 0.55}, blocking=True,
    )
    mock_moode.set_volume.assert_awaited_with(55)


async def test_offline_box_marks_player_unavailable(hass: HomeAssistant, mock_moode) -> None:
    entry = await _setup(hass)
    mock_moode.fetch_state.side_effect = MoodeError("down")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("media_player.streamer").state == "unavailable"


async def test_unload(hass: HomeAssistant, mock_moode) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.NOT_LOADED


async def test_library_websocket(hass: HomeAssistant, mock_moode, hass_ws_client) -> None:
    """FLACs tab: boxes with music, signed cover URLs, and play commands reach MPD."""
    from unittest.mock import AsyncMock, patch

    entry = await _setup(hass)
    albums = [{"album": "Back in Black", "artist": "AC/DC", "year": "1980"}]
    with (
        patch("custom_components.moode.api.MoodeClient.lib_stats",
              AsyncMock(return_value={"songs": 4, "artists": 1, "albums": 1})),
        patch("custom_components.moode.api.MoodeClient.lib_albums", AsyncMock(return_value=albums)),
        patch("custom_components.moode.api.MoodeClient.lib_play", AsyncMock()) as lib_play,
        patch("custom_components.moode.api.MoodeClient.lib_artists", AsyncMock(return_value=["AC/DC"])),
    ):
        client = await hass_ws_client(hass)
        await client.send_json_auto_id({"type": "moode/library/boxes"})
        boxes = (await client.receive_json())["result"]
        assert boxes[0]["entry_id"] == entry.entry_id and boxes[0]["songs"] == 4
        assert boxes[0]["player"].startswith("media_player.")

        await client.send_json_auto_id({"type": "moode/library/albums", "entry_id": entry.entry_id, "artist": "AC/DC"})
        out = (await client.receive_json())["result"]
        assert out[0]["album"] == "Back in Black"
        assert out[0]["cover"].startswith(f"/api/moode/cover/{entry.entry_id}?") and "authSig=" in out[0]["cover"]

        await client.send_json_auto_id({"type": "moode/library/artists", "entry_id": entry.entry_id})
        arts = (await client.receive_json())["result"]
        assert arts[0]["name"] == "AC/DC"
        from urllib.parse import parse_qs, urlsplit
        img = urlsplit(arts[0]["image"])
        assert img.path == f"/api/moode/artist/{entry.entry_id}"
        assert parse_qs(img.query)["name"] == ["AC/DC"] and "authSig" in parse_qs(img.query)

        await client.send_json_auto_id({"type": "moode/library/play", "entry_id": entry.entry_id,
                                        "what": "genre", "genre": "Hard Rock", "shuffle": True})
        assert (await client.receive_json())["success"]
        assert lib_play.call_args.args == ("genre",)
        assert lib_play.call_args.kwargs["genre"] == "Hard Rock" and lib_play.call_args.kwargs["shuffle"] is True

        await client.send_json_auto_id({"type": "moode/library/play", "entry_id": entry.entry_id, "what": "nonsense"})
        assert not (await client.receive_json())["success"]


async def test_leftover_library_menu_entities_are_removed(hass: HomeAssistant, mock_moode) -> None:
    """Entities from the dropped library-menu test build disappear on setup."""
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain=DOMAIN, title="Streamer", unique_id="moodestreamer",
                            data={CONF_HOST: "192.0.2.10"})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    old = reg.async_get_or_create("select", DOMAIN, "moodestreamer_lib_letter", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get(old.entity_id) is None


async def test_controls_follow_the_box_role(hass: HomeAssistant, mock_moode) -> None:
    """A sender gets no room controls, a room gets no station pickers, and changing the
    zone rebuilds them for the boxes whose role changed. The moOde version is shown."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    room = MockConfigEntry(domain=DOMAIN, title="Kitchen", unique_id="kitchenpi",
                           data={CONF_HOST: "192.0.2.11"})
    room.add_to_hass(hass)
    sender = MockConfigEntry(domain=DOMAIN, title="Streamer", unique_id="moodestreamer",
                             data={CONF_HOST: "192.0.2.10"}, options={"receivers": [room.entry_id]})
    sender.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sender.entry_id)
    await hass.async_block_till_done()

    reg = er.async_get(hass)

    def keys(entry) -> set[str]:
        box = entry.unique_id
        return {e.unique_id.removeprefix(f"{box}_") for e in er.async_entries_for_config_entry(reg, entry.entry_id)}

    s, r = keys(sender), keys(room)
    assert {"all_stations", "recent"} <= s and not {"room_mode", "room_station", "receiver"} & s
    assert {"room_mode", "room_station", "receiver"} <= r and not {"all_stations", "recent"} & r

    dev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "moodestreamer")})
    assert dev.sw_version == "9.4.2"
    assert dev.hw_version == "Pi-4B 1.4 4GB"

    # Take the Kitchen out of the zone: it becomes a box of its own (receiver off in moOde)
    hass.config_entries.async_update_entry(sender, options={"receivers": []})
    await hass.async_block_till_done()
    r = keys(room)
    assert {"all_stations", "recent"} <= r and not {"room_mode", "room_station", "receiver"} & r


async def test_station_sync_makes_targets_a_copy(hass: HomeAssistant, mock_moode) -> None:
    """moode.sync_stations: missing added, changed replaced, extras removed (mirror)."""
    from datetime import timedelta
    from unittest.mock import AsyncMock, patch

    from homeassistant.helpers import entity_registry as er
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.moode import api
    from custom_components.moode.stations import note_stations_changed

    boxes = {
        "192.0.2.10": [{"name": "A", "station": "http://a"}, {"name": "B", "station": "http://b2", "genre": "Rock"},
                       {"name": "C", "station": "http://c"}],
        "192.0.2.11": [{"name": "A", "station": "http://a"}, {"name": "B", "station": "http://b1"},
                       {"name": "D", "station": "http://d"}],
    }
    added, deleted = [], []

    async def get_stations(self, *args, **kwargs):
        return [dict(r) for r in boxes[self.host]]

    async def add_station(self, st):
        if any(r["station"] == st["url"] for r in boxes[self.host]):  # as moOde does
            raise api.MoodeError("A station with same URL already exists in the database")
        added.append((self.host, st["name"]))
        boxes[self.host].append({"name": st["name"], "station": st["url"], "genre": st.get("genre", "")})
        return "OK"

    async def delete_station(self, name):
        deleted.append((self.host, name))
        boxes[self.host] = [r for r in boxes[self.host] if r["name"] != name]

    # moOde re-encodes uploaded logos: the target's copies never match the source's bytes
    source_logo = {"A": "data:image/jpeg;base64,AA"}

    async def fetch_logo(hass, url, *args, **kwargs):
        if "192.0.2.11" in url:
            return "data:image/jpeg;base64,REENCODED"
        name = url.rsplit("/", 1)[-1].split(".")[0]
        return source_logo.get(name, "data:image/jpeg;base64,SAME")

    src = MockConfigEntry(domain=DOMAIN, title="Streamer", unique_id="moodestreamer",
                          data={CONF_HOST: "192.0.2.10"})
    tgt = MockConfigEntry(domain=DOMAIN, title="Kitchen", unique_id="kitchenpi",
                          data={CONF_HOST: "192.0.2.11"})
    src.add_to_hass(hass)
    tgt.add_to_hass(hass)
    with (
        patch.object(api.MoodeClient, "get_stations", get_stations),
        patch.object(api.MoodeClient, "add_station", add_station),
        patch.object(api.MoodeClient, "delete_station", delete_station),
        patch.object(api.MoodeClient, "refresh_radio_library", AsyncMock()),
        patch("custom_components.moode.stations._fetch_data_url", AsyncMock(side_effect=fetch_logo)),
        patch.object(api.MoodeClient, "stage_station_logo", AsyncMock(return_value=False)),
    ):
        assert await hass.config_entries.async_setup(src.entry_id)
        await hass.async_block_till_done()
        player = next(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), src.entry_id)
                      if e.domain == "media_player" and e.unique_id == "moodestreamer_player")

        res = await hass.services.async_call(DOMAIN, "sync_stations", {"source": player},
                                             blocking=True, return_response=True)
        assert res["boxes"]["Kitchen"] == {"added": 1, "updated": 1, "removed": 1, "failed": []}
        assert sorted(r["name"] for r in boxes["192.0.2.11"]) == ["A", "B", "C"]
        assert next(r for r in boxes["192.0.2.11"] if r["name"] == "B")["station"] == "http://b2"
        assert ("192.0.2.10", "B") not in deleted          # the source is never touched

        # nothing changed: a second sync touches nothing, even though logo bytes differ
        res = await hass.services.async_call(DOMAIN, "sync_stations", {"source": player},
                                             blocking=True, return_response=True)
        assert res["boxes"]["Kitchen"] == {"added": 0, "updated": 0, "removed": 0, "failed": []}
        # a logo changed on the source: just that station is sent again
        source_logo["A"] = "data:image/jpeg;base64,NEWLOGO"
        res = await hass.services.async_call(DOMAIN, "sync_stations", {"source": player},
                                             blocking=True, return_response=True)
        assert res["boxes"]["Kitchen"]["updated"] == 1

        # a station renamed on the source (same URL): the old name goes before the new one comes
        boxes["192.0.2.10"] = [{**r, "name": "C renamed"} if r["name"] == "C" else r
                               for r in boxes["192.0.2.10"]]
        res = await hass.services.async_call(DOMAIN, "sync_stations", {"source": player},
                                             blocking=True, return_response=True)
        assert res["boxes"]["Kitchen"] == {"added": 1, "updated": 0, "removed": 1, "failed": []}
        assert sorted(r["name"] for r in boxes["192.0.2.11"]) == ["A", "B", "C renamed"]

        # automatic: an edit on the source syncs the others a minute later
        boxes["192.0.2.10"].append({"name": "E", "station": "http://e"})
        hass.config_entries.async_update_entry(src, options={**src.options, "auto_sync_stations": True})
        await hass.async_block_till_done()
        note_stations_changed(hass, src.entry_id)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()
        assert ("192.0.2.11", "E") not in added             # not yet
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
        await hass.async_block_till_done()
        assert ("192.0.2.11", "E") in added


def _fake_trx(boxes: dict):
    """moOde's Multiroom page per host: `boxes[host]` holds the current values."""
    from custom_components.moode.api import TrxForm

    choices = {"multiroom_rx": ["Disabled", "On", "Off"], "multiroom_rx_bfr": ["16", "32", "64"],
               "multiroom_rx_alsa_output_mode": ["plughw", "hw"]}
    posted = []

    def form(host):
        return TrxForm(values=dict(boxes[host]), choices=choices, csrf="t")

    async def trx_read(self):
        return form(self.host), {}

    async def trx_set(self, button, values=None):
        posted.append((self.host, button, dict(values or {})))
        boxes[self.host].update(values or {})
        return form(self.host)

    return trx_read, trx_set, posted


async def test_provision_receiver_joins_the_zone(hass: HomeAssistant, mock_moode) -> None:
    """moode.provision_multiroom: dry run lists the changes; the real run makes them,
    copies a working room's settings and adds the box to the zone."""
    from unittest.mock import patch

    from homeassistant.helpers import entity_registry as er

    from custom_components.moode import api

    boxes = {
        "192.0.2.10": {"multiroom_tx": "On", "alsa_loopback": "On", "multiroom_tx_host": "239.0.0.1",
                       "multiroom_rx": "Disabled"},
        "192.0.2.11": {"multiroom_rx": "On", "multiroom_rx_host": "239.0.0.1", "multiroom_rx_bfr": "32",
                       "multiroom_rx_alsa_output_mode": "iec958"},
        "192.0.2.12": {"multiroom_rx": "Disabled", "multiroom_rx_host": "239.0.0.9", "multiroom_rx_bfr": "64",
                       "multiroom_rx_alsa_output_mode": "plughw"},
    }
    trx_read, trx_set, posted = _fake_trx(boxes)
    kitchen = MockConfigEntry(domain=DOMAIN, title="Kitchen", unique_id="kitchenpi", data={CONF_HOST: "192.0.2.11"})
    new = MockConfigEntry(domain=DOMAIN, title="Office", unique_id="officepi", data={CONF_HOST: "192.0.2.12"})
    kitchen.add_to_hass(hass)
    new.add_to_hass(hass)
    sender = MockConfigEntry(domain=DOMAIN, title="Streamer", unique_id="moodestreamer",
                             data={CONF_HOST: "192.0.2.10"}, options={"receivers": [kitchen.entry_id]})
    sender.add_to_hass(hass)
    with (
        patch.object(api.MoodeClient, "trx_read", trx_read),
        patch.object(api.MoodeClient, "trx_set", trx_set),
        patch("custom_components.moode.provision.JOB_PAUSE", 0),
    ):
        assert await hass.config_entries.async_setup(sender.entry_id)
        await hass.async_block_till_done()
        reg = er.async_get(hass)

        def player(entry):
            return next(e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)
                        if e.domain == "media_player" and e.unique_id.endswith("_player"))

        data = {"box": player(new), "role": "receiver", "sender": player(sender)}
        res = await hass.services.async_call(DOMAIN, "provision_multiroom", {**data, "dry_run": True},
                                             blocking=True, return_response=True)
        assert res["changes"] == ["Multicast address: 239.0.0.9 → 239.0.0.1", "Receiver buffer: 64 → 32",
                                  "Receiver: Disabled → On"]
        assert any("ALSA output mode" in n for n in res["notes"])   # iec958 not offered here
        assert not posted                                            # dry run changes nothing

        res = await hass.services.async_call(DOMAIN, "provision_multiroom", data,
                                             blocking=True, return_response=True)
        await hass.async_block_till_done()
        assert boxes["192.0.2.12"]["multiroom_rx"] == "On"
        assert boxes["192.0.2.12"]["multiroom_rx_host"] == "239.0.0.1"
        assert ("192.0.2.10", "multiroom_tx_discover", {}) in posted  # sender looks for it
        assert hass.config_entries.async_get_entry(sender.entry_id).options["receivers"] == [
            kitchen.entry_id, new.entry_id]

        # again: nothing left to do
        res = await hass.services.async_call(DOMAIN, "provision_multiroom", data,
                                             blocking=True, return_response=True)
        assert res["changes"] == ["Nothing to change: already set up"]


async def test_new_box_flow_offers_multiroom_role(hass: HomeAssistant, mock_moode) -> None:
    """Adding a box while a zone exists asks for its role; Sender switches loopback + sender on."""
    from unittest.mock import patch

    from custom_components.moode import api

    boxes = {"192.0.2.20": {"multiroom_tx": "Off", "alsa_loopback": "Off", "multiroom_rx": "Disabled"}}
    trx_read, trx_set, posted = _fake_trx(boxes)
    await _setup(hass)
    mock_moode.validate.return_value = "newpi"
    with (
        patch.object(api.MoodeClient, "trx_read", trx_read),
        patch.object(api.MoodeClient, "trx_set", trx_set),
        patch("custom_components.moode.provision.JOB_PAUSE", 0),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.20"})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "multiroom"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"role": "sender"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [b for _, b, _ in posted] == ["update_alsa_loopback", "update_multiroom_tx"]
    assert boxes["192.0.2.20"]["multiroom_tx"] == "On"
