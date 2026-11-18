# Changelog

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
