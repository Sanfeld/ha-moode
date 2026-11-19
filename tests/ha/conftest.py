"""Home Assistant test fixtures (pytest-homeassistant-custom-component)."""
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.moode.api import MoodeState, ReceiverStatus


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load custom_components/moode in every test."""
    yield


def make_state(**kw) -> MoodeState:
    st = MoodeState(
        state="play", volume=30, muted=False, title="Groove Salad", artist="SomaFM",
        file="http://ice1.somafm.com/groovesalad-128-mp3", station="SomaFM Groove Salad",
        receiver=ReceiverStatus(state="Disabled"),
    )
    for k, v in kw.items():
        setattr(st, k, v)
    return st


@pytest.fixture
def mock_moode():
    """Patch every network call the client makes; tests set .fetch_state etc."""
    with (
        patch("custom_components.moode.api.MoodeClient.validate", AsyncMock(return_value="moodestreamer")) as validate,
        patch("custom_components.moode.api.MoodeClient.fetch_state", AsyncMock(return_value=make_state())) as fetch,
        patch("custom_components.moode.api.MoodeClient.idle_forever", AsyncMock(return_value=None)),
        patch("custom_components.moode.api.MoodeClient.lsinfo", AsyncMock(return_value=[])),
        patch("custom_components.moode.api.MoodeClient.get_stations", AsyncMock(return_value=[])),
        patch("custom_components.moode.api.MoodeClient.lib_stats",
              AsyncMock(return_value={"songs": 0, "artists": 0, "albums": 0})),
        patch("custom_components.moode.api.MoodeClient.play", AsyncMock()) as play,
        patch("custom_components.moode.api.MoodeClient.set_volume", AsyncMock()) as set_volume,
    ):
        yield type("M", (), {"validate": validate, "fetch_state": fetch, "play": play, "set_volume": set_volume})
