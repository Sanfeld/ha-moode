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

    # Take the Kitchen out of the zone: it becomes a box of its own (receiver off in moOde)
    hass.config_entries.async_update_entry(sender, options={"receivers": []})
    await hass.async_block_till_done()
    r = keys(room)
    assert {"all_stations", "recent"} <= r and not {"room_mode", "room_station", "receiver"} & r
