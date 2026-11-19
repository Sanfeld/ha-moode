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
        assert arts[0]["image"].startswith(f"/api/moode/artist/{entry.entry_id}?name=AC%2FDC") and "authSig=" in arts[0]["image"]

        await client.send_json_auto_id({"type": "moode/library/play", "entry_id": entry.entry_id,
                                        "what": "genre", "genre": "Hard Rock", "shuffle": True})
        assert (await client.receive_json())["success"]
        assert lib_play.call_args.args == ("genre",)
        assert lib_play.call_args.kwargs["genre"] == "Hard Rock" and lib_play.call_args.kwargs["shuffle"] is True

        await client.send_json_auto_id({"type": "moode/library/play", "entry_id": entry.entry_id, "what": "nonsense"})
        assert not (await client.receive_json())["success"]
