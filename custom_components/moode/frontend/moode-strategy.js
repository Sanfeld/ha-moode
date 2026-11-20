/*
 * moOde Audio – self-building dashboard.
 *
 * Use it by creating a dashboard whose raw configuration is:
 *
 *   strategy:
 *     type: custom:moode-music
 *
 * Every time the dashboard opens it is rebuilt from what the moOde integration
 * currently has: zones, rooms, favourite stations, amplifiers and settings.
 * Only Home Assistant's built-in cards are used.
 */

const DOMAIN = "moode";

// Which parts of the moOde entities' state the dashboard depends on: whether each
// entity is usable, and each favourite's logo and name. Cached per registry object
// so the check on every state change only touches moOde entities.
const _moodeIds = new WeakMap();
function moodeEntities(hass) {
  let list = _moodeIds.get(hass.entities);
  if (!list) {
    list = Object.values(hass.entities || {})
      .filter((e) => e.platform === DOMAIN)
      .map((e) => [e.entity_id, e.translation_key === "play_favourite"]);
    _moodeIds.set(hass.entities, list);
  }
  return list;
}
function dashboardSignature(hass) {
  return moodeEntities(hass).map(([id, fav]) => {
    const s = hass.states[id];
    const ok = !s ? "-" : s.state === "unavailable" ? "u" : "a";
    return fav && s ? `${id}:${ok}:${s.attributes.entity_picture || ""}:${s.attributes.friendly_name || ""}` : `${id}:${ok}`;
  }).join("|");
}

class MoodeMusicStrategy extends HTMLElement {
  // HA normally rebuilds a generated dashboard only when the registries change.
  // A new favourite is registered before it has a state, so we also rebuild when a
  // moOde entity becomes (un)available or a favourite's logo/name changes.
  static shouldRegenerate(config, oldHass, newHass) {
    if (["entities", "devices", "areas", "floors"].some((k) => oldHass[k] !== newHass[k])) return true;
    if (oldHass.states === newHass.states) return false;
    return dashboardSignature(oldHass) !== dashboardSignature(newHass);
  }

  static async generate(config, hass) {
    const ents = Object.values(hass.entities || {}).filter(
      (e) => e.platform === DOMAIN && !e.hidden
    );
    const st = (id) => hass.states[id];
    const attr = (id, a) => st(id)?.attributes?.[a];
    const usable = (id) => st(id) && st(id).state !== "unavailable";
    const onDevice = (dev, key) =>
      ents.find((e) => e.device_id === dev && e.translation_key === key);
    const allOnDevice = (dev, key) =>
      ents.filter((e) => e.device_id === dev && e.translation_key === key);
    const devName = (dev) =>
      hass.devices?.[dev]?.name_by_user || hass.devices?.[dev]?.name || "";
    const byName = (a, b) => (a.name || "").localeCompare(b.name || "");

    // Work out the layout from the registry where possible, so a box that is offline
    // (its attributes gone) keeps its place instead of scrambling the page.
    const players = ents.filter((e) => e.entity_id.startsWith("media_player."));
    const isZone = (e) =>
      e.translation_key === "zone" || !!attr(e.entity_id, "rooms")
      || hass.devices?.[e.device_id]?.model === "moOde multiroom zone";
    const zones = players.filter(isZone);
    const boxes = players
      .filter((e) => !isZone(e))
      .map((e) => ({ ...e, name: devName(e.device_id) || e.entity_id }));
    const senderOf = (zone) => {
      const id = attr(zone.entity_id, "sender_entity");
      const via = hass.devices?.[zone.device_id]?.via_device_id;
      return boxes.find((b) => b.entity_id === id) || boxes.find((b) => via && b.device_id === via);
    };
    const isRoom = (b) =>
      attr(b.entity_id, "multiroom_role") === "receiver" || !!onDevice(b.device_id, "room_mode");
    const receivers = boxes.filter(isRoom).sort(byName);
    const zoneSenders = new Set(zones.map((z) => senderOf(z)?.entity_id).filter(Boolean));
    const offline = (id) => st(id)?.state === "unavailable";
    const offlineCard = (name) => ({
      type: "markdown",
      content: `**${name} is offline.**\n\nIts music controls come back on their own as soon as it is on the network again.`,
      grid_options: { columns: 12 },
    });
    const standalone = boxes
      .filter(
        (b) =>
          !isRoom(b) && !zoneSenders.has(b.entity_id)
      )
      .sort(byName);

    // ---------------------------------------------------------------- pieces
    const ampCards = (box) => {
      const amp = attr(box.entity_id, "amplifier");
      if (!amp || !st(amp)) return [];
      const cards = [
        {
          type: "tile",
          entity: amp,
          name: "Amplifier",
          icon: "mdi:amplifier",
          tap_action: { action: "toggle" },
          features_position: "bottom",
          features: [{ type: "media-player-volume-slider" }],
          grid_options: { columns: 6, rows: 2 },
        },
      ];
      const input = onDevice(box.device_id, "amp_input");
      if (input)
        cards.push({
          type: "tile",
          entity: input.entity_id,
          name: "Input",
          icon: "mdi:import",
          hide_state: true,
          features_position: "bottom",
          features: [{ type: "select-options" }],
          grid_options: { columns: 6, rows: 2 },
        });
      return cards;
    };

    const stationCards = (box) => {
      const cards = [];
      const favs = allOnDevice(box.device_id, "play_favourite")
        .filter((b) => usable(b.entity_id))   // skip leftovers of removed favourites
        .map((b) => ({
          ...b,
          label: (st(b.entity_id)?.attributes?.friendly_name || b.entity_id).replace(
            /^.*?Play /,
            ""
          ),
        }))
        .sort((a, b) => a.label.localeCompare(b.label));
      if (favs.length) {
        cards.push({ type: "heading", heading: "Stations", heading_style: "subtitle" });
        for (const f of favs)
          cards.push({
            // picture-entity shows the button's entity_picture (the station's moOde logo)
            type: "picture-entity",
            entity: f.entity_id,
            // picture-entity needs an explicit image; use the button's logo (entity_picture)
            image: attr(f.entity_id, "entity_picture"),
            fit_mode: "contain",                // show the whole logo, never cropped
            name: f.label,
            show_state: false,
            show_name: true,
            aspect_ratio: "1:1",
            tap_action: {
              action: "perform-action",
              perform_action: "button.press",
              target: { entity_id: f.entity_id },
            },
            hold_action: { action: "more-info" },
            grid_options: { columns: 4 },
          });
      }
      const recent = onDevice(box.device_id, "recent");
      if (recent && usable(recent.entity_id))
        cards.push({
          type: "tile",
          entity: recent.entity_id,
          name: "Recently played",
          icon: "mdi:history",
          hide_state: true,
          features_position: "bottom",
          features: [{ type: "select-options" }],
          grid_options: { columns: 12, rows: 2 },
        });
      const all = onDevice(box.device_id, "all_stations");
      if (all && usable(all.entity_id))
        cards.push({
          type: "tile",
          entity: all.entity_id,
          name: "All stations",
          icon: "mdi:radio-tower",
          hide_state: true,
          features_position: "bottom",
          features: [{ type: "select-options" }],
          grid_options: { columns: 12, rows: 2 },
        });
      return cards;
    };

    const zoneButtons = (zoneId) => [
      {
        type: "button",
        name: "All rooms on",
        icon: "mdi:speaker-multiple",
        show_state: false,
        tap_action: {
          action: "perform-action",
          perform_action: "media_player.turn_on",
          target: { entity_id: zoneId },
        },
        grid_options: { columns: 6, rows: 1 },
      },
      {
        type: "button",
        name: "All rooms off",
        icon: "mdi:speaker-off",
        show_state: false,
        tap_action: {
          action: "perform-action",
          perform_action: "media_player.turn_off",
          target: { entity_id: zoneId },
          confirmation: { text: "Switch every room off and stop the music?" },
        },
        grid_options: { columns: 6, rows: 1 },
      },
    ];

    // ---------------------------------------------------------------- music view
    const musicSections = [];

    for (const zone of zones) {
      const sender = senderOf(zone);
      const name = sender?.name || devName(zone.device_id) || "Streamer";
      const cards = [
        {
          type: "heading",
          heading: name,
          icon: "mdi:access-point",
          badges: [{ type: "entity", entity: zone.entity_id, show_state: true, show_icon: false }],
        },
      ];
      if (offline(zone.entity_id) || (sender && offline(sender.entity_id))) {
        cards.push(offlineCard(name));
        if (sender) cards.push(...ampCards(sender));
      } else {
        cards.push(
          { type: "media-control", entity: zone.entity_id, grid_options: { columns: 12 } },
          ...zoneButtons(zone.entity_id),
        );
        if (sender) cards.push(...ampCards(sender), ...stationCards(sender));
      }
      musicSections.push({ type: "grid", cards });
    }

    for (const box of standalone) {
      musicSections.push({
        type: "grid",
        cards: offline(box.entity_id)
          ? [{ type: "heading", heading: box.name, icon: "mdi:speaker" }, offlineCard(box.name), ...ampCards(box)]
          : [
              { type: "heading", heading: box.name, icon: "mdi:speaker" },
              { type: "media-control", entity: box.entity_id, grid_options: { columns: 12 } },
              ...ampCards(box),
              ...stationCards(box),
            ],
      });
    }

    if (receivers.length) {
      const cards = [{ type: "heading", heading: "Rooms", icon: "mdi:home-sound-in" }];
      for (const r of receivers) {
        cards.push({
          type: "tile",
          entity: r.entity_id,
          name: r.name,
          icon: "mdi:speaker",
          show_entity_picture: true,
          state_content: ["media_title", "media_artist"],
          grid_options: { columns: 12 },
        });
        for (const [key, name, icon] of [
          ["room_mode", "Mode", "mdi:speaker-multiple"],
          ["room_station", "Station", "mdi:radio"],
        ]) {
          const e = onDevice(r.device_id, key);
          if (e)
            cards.push({
              type: "tile",
              entity: e.entity_id,
              name,
              icon,
              hide_state: true,
              features_position: "bottom",
              features: [{ type: "select-options" }],
              grid_options: { columns: 6, rows: 2 },
            });
        }
        cards.push(...ampCards(r));
      }
      musicSections.push({ type: "grid", cards });
    }

    if (!musicSections.length)
      musicSections.push({
        type: "grid",
        cards: [
          {
            type: "markdown",
            content:
              "No moOde players yet. Add one under **Settings → Devices & services → moOde Audio**.",
          },
        ],
      });

    // ---------------------------------------------------------------- settings view
    const settingsSections = [];
    const playback = [{ type: "heading", heading: "Playback", icon: "mdi:tune" }];
    const tts = Object.keys(hass.states).find((id) => id.startsWith("tts."));
    for (const zone of zones.length ? zones : standalone) {
      const sender = senderOf(zone) || zone;
      const sleep = onDevice(sender.device_id, "sleep_timer");
      const label = sender.name || devName(zone.device_id);
      if (sleep)
        playback.push({
          type: "tile",
          entity: sleep.entity_id,
          name: `Sleep timer – ${label}`,
          icon: "mdi:timer-sand",
          state_content: ["state", "ends_at"],
          features_position: "bottom",
          features: [{ type: "select-options" }],
          grid_options: { columns: 12, rows: 2 },
        });
      playback.push({
        type: "button",
        name: "Browse library",
        icon: "mdi:music-box-multiple",
        // the dashboard's own FLACs tab (same dashboard, whatever its URL is)
        tap_action: { action: "navigate", navigation_path: `/${location.pathname.split("/")[1] || "lovelace"}/flacs` },
        grid_options: { columns: 6, rows: 1 },
      });
      if (tts)
        playback.push({
          type: "button",
          name: "Test announcement",
          icon: "mdi:bullhorn",
          tap_action: {
            action: "perform-action",
            perform_action: "media_player.play_media",
            target: { entity_id: zone.entity_id },
            data: {
              media_content_type: "music",
              media_content_id: `media-source://tts/${tts}?message=This%20is%20a%20test%20announcement`,
              announce: true,
            },
            confirmation: { text: "Play a test announcement?" },
          },
          grid_options: { columns: 6, rows: 1 },
        });
    }
    settingsSections.push({ type: "grid", cards: playback });

    const autos = Object.keys(hass.states)
      .filter(
        (id) =>
          id.startsWith("automation.") &&
          (id.includes("moode") || /moode/i.test(attr(id, "friendly_name") || ""))
      )
      .sort();
    if (autos.length)
      settingsSections.push({
        type: "grid",
        cards: [
          { type: "heading", heading: "Automations", icon: "mdi:robot" },
          ...autos.map((id) => ({
            type: "tile",
            entity: id,
            icon: "mdi:robot",
            tap_action: { action: "toggle" },
            grid_options: { columns: 6 },
          })),
        ],
      });

    const switches = ents
      .filter((e) => e.translation_key === "receiver" && usable(e.entity_id))
      .map((e) => ({ entity: e.entity_id, name: `${devName(e.device_id)} receiver` }));
    const boxRows = [
      ...switches,
      ...boxes.map((b) => ({
        type: "attribute",
        entity: b.entity_id,
        attribute: "output_format",
        name: `${b.name} output`,
        icon: "mdi:waveform",
      })),
    ];
    if (boxRows.length)
      settingsSections.push({
        type: "grid",
        cards: [
          { type: "heading", heading: "Boxes", icon: "mdi:raspberry-pi" },
          { type: "entities", show_header_toggle: false, entities: boxRows },
        ],
      });

    return {
      title: config.title || "Music",
      views: [
        {
          title: "Music",
          path: "music",
          icon: "mdi:speaker-multiple",
          type: "sections",
          max_columns: 2,
          sections: musicSections,
        },
        {
          title: "Stations",
          path: "stations",
          icon: "mdi:radio",
          type: "panel",
          cards: [{ type: "custom:moode-stations-card" }],
        },
        {
          title: "FLACs",
          path: "flacs",
          icon: "mdi:album",
          type: "panel",
          cards: [{ type: "custom:moode-library-card" }],
        },
        {
          title: "Settings",
          path: "music-settings",
          icon: "mdi:cog",
          type: "sections",
          max_columns: 2,
          sections: settingsSections,
        },
      ],
    };
  }
}

if (!customElements.get("ll-strategy-dashboard-moode-music")) {
  customElements.define("ll-strategy-dashboard-moode-music", MoodeMusicStrategy);
}
