# Changelog

## 1.3.0 (2026-11-20)

- Each box only gets the controls that fit its role: no room mode, room station or
  receiver switch on a sender, no station pickers on a zone's rooms. No more entities that
  are always "unavailable". Changing a zone updates the boxes whose role changed.
- Device pages show the moOde version (checked every 6 hours, so moOde updates show up)

## 1.2.1 (2026-11-19)

- Removes the library menu selects left behind by a 1.3.0 test build (never released)
- Tests: fixed the artist image URL check

## 1.2.0 (2026-11-19)

- FLACs tab: artist photos (from Deezer, cached on Home Assistant; falls back to one of
  the artist's album covers). Can be switched off per box: Configure → Music library
- FLACs tab: show artists as a list or a grid (remembered)

## 1.1.0 (2026-11-19)

- New **FLACs** tab on the moOde dashboard: the music library stored on your boxes
  - browse by artist, album (with cover art) and genre, with an A–Z bar
  - search artists, albums and songs; recently added albums
  - play, shuffle or queue an album, artist or genre, or shuffle everything
  - shows which rooms it plays in, with a small now-playing bar
- The Music tab's *Browse library* button opens the FLACs tab

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
