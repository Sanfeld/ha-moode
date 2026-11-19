# Changelog

## 1.0.4 (2026-11-19)

- A stopped box no longer shows an old song: moOde keeps the last song its own player
  played, even after the box has since been playing from a multiroom zone, so stopped
  players now show only the station

Doorbell pause blueprint:

- New "Turn the volume down" mode, as an alternative to pausing: lowers chosen players or amplifiers
  to a set level and puts the volume back afterwards
- The announcement now plays before the volume goes down, so it isn't drowned out
- Adjustable time to pause or keep the volume down (5 s to 30 min) and announcement length
- Works with phone/tablet "Last notification" sensors, for doorbells whose rings don't reach
  Home Assistant (e.g. Yale video doorbells); a regular expression picks out ring notifications
- Fix: the very first ring of a doorbell that had never rung before was ignored

## 1.0.3 (2026-11-18)

- Dashboard: newly starred favourites appear right away, and logo or name changes show
  without reloading the page

## 1.0.2 (2026-11-18)

- Song titles, artists and station names with "&" no longer show as "&amp;amp;"
- README: dashboard screenshot

## 1.0.1 (2026-11-18)

- Stations tab keeps your view (box, filter, search) when you star or unstar a station,
  instead of jumping back to the full list

## 1.0.0 (2026-11-18)

First public release.

- Discovery (zeroconf) and manual setup; one device per moOde box
- Media player per box with instant updates (MPD idle) and a 15 s fallback poll
- Multiroom zones: zone player, per-room Zone / Solo / Off, receiver mirroring, HA grouping
- Favourites (buttons with station logos), All stations, Recently played, per-room station
- Stations manager: check streams, bulk delete, favourite, edit (name, URL, logo), radio-browser.info search,
  backup / restore, sync between boxes
- Amplifier link per box (power, volume, input on the dashboard)
- Sleep timer, announcements with resume, Assist media search
- Repairs warnings (offline, no internet, receiver disabled) and diagnostics
- Self-building dashboard strategy `custom:moode-music`; card `custom:moode-stations-card`
- Blueprints: amps follow zone, doorbell pause, morning radio
