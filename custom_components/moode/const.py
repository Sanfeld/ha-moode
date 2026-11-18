"""Constants for the moOde Audio integration."""
from datetime import timedelta

DOMAIN = "moode"
MANUFACTURER = "moOde audio"
SCAN_INTERVAL = timedelta(seconds=15)  # MPD idle pushes changes instantly; this is the fallback
PLATFORMS = ["media_player", "switch", "select", "button"]
STATIC_URL = "/moode_static"
STRATEGY_URL = f"{STATIC_URL}/moode-strategy.js"
PANEL_URL = f"{STATIC_URL}/moode-stations-panel.js"
CONF_RECEIVERS = "receivers"
CONF_ZONE_NAME = "zone_name"
CONF_FAVOURITES = "favourites"
CONF_FAV_WAKE_ZONE = "favourites_wake_zone"
CONF_AMPLIFIER = "amplifier"  # entity_id of the amp this box feeds
CONF_VOLUME_MODE = "volume_mode"  # "auto" | "fixed" | "variable"
CONF_SLEEP_ACTION = "sleep_action"  # "stop" | "zone_off"
CONF_SLEEP_FADE = "sleep_fade"
CONF_SLEEP_FADE_SECONDS = "sleep_fade_seconds"
CONF_ANN_RESUME = "announce_resume"
CONF_ANN_ALL_ROOMS = "announce_all_rooms"
CONF_ANN_LEAD_IN = "announce_lead_in"
CONF_ANN_MAX = "announce_max_seconds"

DEFAULTS = {
    CONF_VOLUME_MODE: "auto",
    CONF_FAV_WAKE_ZONE: True,
    CONF_SLEEP_ACTION: "zone_off",
    CONF_SLEEP_FADE: True,
    CONF_SLEEP_FADE_SECONDS: 30,
    CONF_ANN_RESUME: True,
    CONF_ANN_ALL_ROOMS: True,
    CONF_ANN_LEAD_IN: 1.0,
    CONF_ANN_MAX: 60,
}


def opt(entry, key):
    """Option value with default."""
    return entry.options.get(key, DEFAULTS.get(key))
