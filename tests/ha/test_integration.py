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
