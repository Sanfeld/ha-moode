/*
 * moOde music library – the dashboard's FLACs tab (`type: custom:moode-library-card`).
 * Browse a box's own music by artist, album and genre, search it, see what was added
 * recently, and play, shuffle or queue it on that box (and so on its zone).
 */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const LETTERS = ["#", ..."ABCDEFGHIJKLMNOPQRSTUVWXYZ"];
const firstLetter = (s) => {
  const c = String(s || "").replace(/^(the|a|an)\s+/i, "").trim().charAt(0).toUpperCase();
  return c >= "A" && c <= "Z" ? c : "#";
};
const sortKey = (s) => String(s || "").replace(/^(the)\s+/i, "").toLocaleLowerCase();
const mmss = (s) => (s == null ? "" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
const PAGE = 120;
const icon = (name) => `<ha-icon icon="mdi:${name}"></ha-icon>`;

const STYLE = `
:host { display:block; color:var(--primary-text-color); }
.wrap { max-width:1200px; margin:0 auto; padding:0 12px 16px; }
.top { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:12px 0 6px; }
.top h1 { font-size:22px; font-weight:400; margin:0; flex:1; min-width:120px; }
.where { font-size:13px; color:var(--secondary-text-color); margin:0 0 10px; }
.np { display:flex; align-items:center; gap:10px; background:var(--card-background-color); border:1px solid var(--divider-color);
      border-radius:12px; padding:8px 10px; margin-bottom:12px; }
.np img { width:44px; height:44px; border-radius:6px; object-fit:cover; background:var(--secondary-background-color); }
.np .txt { flex:1; min-width:0; }
.nm { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.meta { font-size:12px; color:var(--secondary-text-color); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tabs { display:flex; gap:4px; border-bottom:1px solid var(--divider-color); margin-bottom:10px; overflow-x:auto; }
.tab { padding:10px 14px; cursor:pointer; border-bottom:2px solid transparent; color:var(--secondary-text-color); white-space:nowrap; }
.tab.on { border-color:var(--primary-color); color:var(--primary-text-color); }
.ctrls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
input[type=search], select { padding:8px 10px; border:1px solid var(--divider-color); border-radius:8px;
  background:var(--card-background-color); color:var(--primary-text-color); font-size:14px; }
input[type=search] { flex:1; min-width:180px; }
button { padding:7px 12px; border-radius:8px; border:1px solid var(--divider-color); background:var(--card-background-color);
  color:var(--primary-text-color); cursor:pointer; font-size:13px; }
button.pri { background:var(--primary-color); color:var(--text-primary-color, #fff); border-color:var(--primary-color); }
button.ic { border:none; background:none; padding:6px; border-radius:50%; }
button { display:inline-flex; align-items:center; gap:6px; }
button ha-icon { --mdc-icon-size:18px; }
button.ic ha-icon { --mdc-icon-size:26px; }
.ph ha-icon { --mdc-icon-size:48px; } .bigph ha-icon { --mdc-icon-size:80px; }
.letters { display:flex; flex-wrap:wrap; gap:2px; margin-bottom:10px; }
.letters span { min-width:26px; text-align:center; padding:4px 2px; border-radius:6px; cursor:pointer; font-size:13px; color:var(--secondary-text-color); }
.letters span.on { background:var(--primary-color); color:var(--text-primary-color, #fff); }
.letters span.off { opacity:.3; cursor:default; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:12px; }
.alb { cursor:pointer; min-width:0; }
.alb .cv { aspect-ratio:1; border-radius:10px; overflow:hidden; background:var(--secondary-background-color); position:relative; }
.alb .cv img { width:100%; height:100%; object-fit:cover; display:block; position:relative; z-index:1; }
.alb .cv .ph { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:42px; opacity:.35; }
.alb .nm { margin-top:6px; font-size:14px; }
.list .row { display:flex; align-items:center; gap:10px; padding:9px 6px; border-bottom:1px solid var(--divider-color); cursor:pointer; }
.list .row:hover { background:var(--secondary-background-color); }
.av { width:36px; height:36px; border-radius:50%; background:var(--secondary-background-color); display:flex; align-items:center;
      justify-content:center; font-weight:500; flex:none; }
.chips { display:flex; flex-wrap:wrap; gap:8px; }
.chip { padding:8px 14px; border-radius:18px; border:1px solid var(--divider-color); background:var(--card-background-color); cursor:pointer; }
.head { display:flex; gap:16px; align-items:flex-end; margin:6px 0 14px; flex-wrap:wrap; }
.head img, .head .bigph { width:200px; height:200px; border-radius:12px; object-fit:cover; background:var(--secondary-background-color); flex:none; }
.head .bigph { display:flex; align-items:center; justify-content:center; font-size:72px; opacity:.4; }
.head h2 { font-size:24px; font-weight:400; margin:0 0 4px; }
.head .sub { color:var(--secondary-text-color); margin-bottom:12px; }
.link { cursor:pointer; text-decoration:underline; text-decoration-color:var(--divider-color); }
.trk { display:grid; grid-template-columns:34px 1fr auto; gap:8px; align-items:center; padding:9px 6px;
       border-bottom:1px solid var(--divider-color); cursor:pointer; }
.trk:hover { background:var(--secondary-background-color); }
.trk .no { color:var(--secondary-text-color); text-align:right; font-size:13px; }
.crumb { display:flex; align-items:center; gap:6px; margin:4px 0 6px; color:var(--secondary-text-color); font-size:14px; }
h3 { font-size:15px; font-weight:500; margin:16px 0 8px; }
.hint { font-size:13px; color:var(--secondary-text-color); margin:8px 0; }
.more { display:block; margin:14px auto 0; }
.toast { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); background:var(--primary-text-color);
         color:var(--card-background-color); padding:10px 16px; border-radius:8px; font-size:14px; z-index:5; max-width:90vw; }
`;

// The self-building dashboard can re-create this card (e.g. when favourites change).
// Remember where the user was for the life of the page.
const VIEW = {};
const VIEW_KEYS = ["entry", "mode", "stack", "letter", "query"];

class MoodeLibraryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.s = { boxes: [], entry: null, mode: "artists", stack: [], letter: "", query: "",
               data: {}, page: null, limit: PAGE, busy: "", toast: "" };
    VIEW_KEYS.forEach((k) => { if (VIEW[k] !== undefined) this.s[k] = VIEW[k]; });
  }

  setConfig(config) { this._config = config || {}; }
  getCardSize() { return 12; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }

  set hass(h) {
    const first = !this._hass;
    const old = this._hass;
    this._hass = h;
    if (first) { this._init(); return; }
    // keep the "plays in" line and the now-playing bar current without re-rendering everything
    const b = this.box();
    if (b && old && (old.states[b.player] !== h.states[b.player] || old.states[b.zone] !== h.states[b.zone]))
      this.renderLive();
  }

  ws(type, data = {}) { return this._hass.callWS({ type: `moode/library/${type}`, entry_id: this.s.entry, ...data }); }
  box() { return this.s.boxes.find((b) => b.entry_id === this.s.entry); }
  save() { VIEW_KEYS.forEach((k) => { VIEW[k] = this.s[k]; }); }
  toast(msg) {
    this.s.toast = msg; this.renderToast();
    clearTimeout(this._tt); this._tt = setTimeout(() => { this.s.toast = ""; this.renderToast(); }, 3500);
  }

  async _init() {
    this.s.busy = "Loading your library…"; this.render();
    try {
      this.s.boxes = await this._hass.callWS({ type: "moode/library/boxes" });
    } catch (e) { this.s.busy = `Couldn't load the library: ${e.message || e}`; return this.render(); }
    if (!this.s.boxes.some((b) => b.entry_id === this.s.entry)) this.s.entry = this.s.boxes[0]?.entry_id || null;
    this.s.busy = "";
    await this.load();
  }

  // ---------------------------------------------------------------- data
  async load() {
    if (!this.s.entry) return this.render();
    const top = this.s.stack[this.s.stack.length - 1];
    this.s.busy = "Loading…"; this.render();
    try {
      if (top) this.s.page = await this.loadPage(top);
      else if (this.s.mode === "search") this.s.data.search = this.s.query.trim().length >= 2
        ? await this.ws("search", { query: this.s.query.trim() }) : null;
      else if (!this.s.data[this.s.mode]) {
        const cmd = { artists: "artists", albums: "albums", genres: "genres", recent: "recent" }[this.s.mode];
        this.s.data[this.s.mode] = await this.ws(cmd);
      }
    } catch (e) { this.toast(`Couldn't load: ${e.message || e}`); }
    this.s.busy = ""; this.save(); this.render();
  }

  async loadPage(p) {
    if (p.type === "album") return { ...(await this.ws("tracks", { artist: p.artist, album: p.album })) };
    if (p.type === "artist") return { albums: await this.ws("albums", { artist: p.artist }) };
    if (p.type === "genre") {
      const [artists, albums] = await Promise.all([this.ws("artists", { genre: p.genre }), this.ws("albums", { genre: p.genre })]);
      return { artists, albums };
    }
    return {};
  }

  open(page) { this.s.stack = [...this.s.stack, page]; this.s.limit = PAGE; this.load(); this.scrollTop(); }
  back() { this.s.stack = this.s.stack.slice(0, -1); this.s.limit = PAGE; this.load(); }
  home() { this.s.stack = []; this.s.limit = PAGE; this.load(); }
  scrollTop() { this.scrollIntoView?.({ block: "start", behavior: "smooth" }); }

  async play(what, opts = {}, label = "") {
    try {
      await this.ws("play", { what, ...opts });
      this.toast(opts.queue ? `Added ${label} to the queue` : `${opts.shuffle ? "Shuffling" : "Playing"} ${label}`);
    } catch (e) { this.toast(`Couldn't play: ${e.message || e}`); }
  }

  async playSong(t) {
    try {
      const { tracks } = await this.ws("tracks", { artist: t.album_artist, album: t.album });
      const pos = Math.max(0, tracks.findIndex((x) => x.file === t.file));
      await this.play("album", { artist: t.album_artist, album: t.album, position: pos }, `“${t.title}”`);
    } catch (e) { this.toast(`Couldn't play: ${e.message || e}`); }
  }

  player(service) {
    const b = this.box();
    if (b?.player) this._hass.callService("media_player", service, { entity_id: b.player });
  }

  // ---------------------------------------------------------------- rendering
  whereHtml() {
    const b = this.box();
    if (!b) return "";
    const z = b.zone && this._hass.states[b.zone];
    if (b.sender && z) {
      const rooms = Object.entries(z.attributes.rooms || {}).filter(([, v]) => v === "On").map(([k]) => k);
      return rooms.length
        ? `Plays through ${esc(z.attributes.friendly_name)} in: ${esc(rooms.join(", "))}`
        : `No rooms are on in ${esc(z.attributes.friendly_name)}. Playing something switches them all on.`;
    }
    return `Plays on ${esc(b.title)}`;
  }

  npHtml() {
    const p = this.box()?.player && this._hass.states[this.box().player];
    if (!p || !["playing", "paused"].includes(p.state)) return "";
    const a = p.attributes;
    return `<div class="np">
      ${a.entity_picture ? `<img src="${esc(a.entity_picture)}" alt="">` : ""}
      <div class="txt"><div class="nm">${esc(a.media_title || a.media_channel || "")}</div>
        <div class="meta">${esc([a.media_artist, a.media_album_name].filter(Boolean).join(" · "))}</div></div>
      <button class="ic" data-act="prev" title="Previous">${icon("skip-previous")}</button>
      <button class="ic" data-act="pp" title="${p.state === "playing" ? "Pause" : "Play"}">${icon(p.state === "playing" ? "pause" : "play")}</button>
      <button class="ic" data-act="next" title="Next">${icon("skip-next")}</button></div>`;
  }

  albumGrid(albums, showArtist = true) {
    const shown = albums.slice(0, this.s.limit);
    this._albums = shown;
    return `<div class="grid">${shown.map((a, i) => `
      <div class="alb" data-alb="${i}">
        <div class="cv"><div class="ph">${icon("album")}</div><img loading="lazy" src="${esc(a.cover)}" alt="" onerror="this.remove()"></div>
        <div class="nm">${esc(a.album)}</div>
        <div class="meta">${esc([showArtist ? a.artist : "", a.year].filter(Boolean).join(" · "))}</div>
      </div>`).join("")}</div>
      ${albums.length > shown.length ? `<button class="more" data-act="more">Show more (${albums.length - shown.length} left)</button>` : ""}`;
  }

  lettersHtml(items, key) {
    const have = new Set(items.map((x) => firstLetter(key(x))));
    return `<div class="letters"><span class="${this.s.letter ? "" : "on"}" data-letter="">All</span>${LETTERS.map((l) =>
      `<span class="${this.s.letter === l ? "on" : have.has(l) ? "" : "off"}" data-letter="${l}">${l}</span>`).join("")}</div>`;
  }

  byLetter(items, key) {
    return this.s.letter ? items.filter((x) => firstLetter(key(x)) === this.s.letter) : items;
  }

  homeHtml() {
    const s = this.s;
    const tabs = [["artists", "Artists"], ["albums", "Albums"], ["genres", "Genres"], ["recent", "Recently added"]];
    let body = "";
    if (s.mode === "search") {
      const r = s.data.search;
      if (!r) body = `<p class="hint">Type at least two letters.</p>`;
      else if (!r.artists.length && !r.albums.length && !r.tracks.length) body = `<p class="hint">Nothing found for “${esc(s.query)}”.</p>`;
      else {
        this._artists = r.artists; this._songs = r.tracks;
        body = (r.artists.length ? `<h3>Artists</h3><div class="list">${r.artists.map((a, i) =>
                `<div class="row" data-art="${i}"><div class="av">${esc(firstLetter(a))}</div><div class="nm">${esc(a)}</div></div>`).join("")}</div>` : "")
          + (r.albums.length ? `<h3>Albums</h3>${this.albumGrid(r.albums)}` : "")
          + (r.tracks.length ? `<h3>Songs</h3>${this.songsHtml(r.tracks)}` : "");
      }
    } else if (s.mode === "artists" && s.data.artists) {
      const list = this.byLetter(s.data.artists, (a) => a).sort((a, b) => sortKey(a).localeCompare(sortKey(b)));
      const shown = list.slice(0, s.limit);
      this._artists = shown;
      body = this.lettersHtml(s.data.artists, (a) => a) + `<div class="list">${shown.map((a, i) =>
        `<div class="row" data-art="${i}"><div class="av">${esc(firstLetter(a))}</div><div class="nm">${esc(a)}</div></div>`).join("")}</div>`
        + (list.length > shown.length ? `<button class="more" data-act="more">Show more (${list.length - shown.length} left)</button>` : "");
    } else if (s.mode === "albums" && s.data.albums) {
      body = this.lettersHtml(s.data.albums, (a) => a.album) + this.albumGrid(this.byLetter(s.data.albums, (a) => a.album));
    } else if (s.mode === "genres" && s.data.genres) {
      this._genres = s.data.genres;
      body = s.data.genres.length
        ? `<div class="chips">${s.data.genres.map((g, i) => `<span class="chip" data-gen="${i}">${esc(g)}</span>`).join("")}</div>`
        : `<p class="hint">Your music has no genre tags.</p>`;
    } else if (s.mode === "recent" && s.data.recent) {
      body = this.albumGrid(s.data.recent);
    }
    return `
      <div class="ctrls">
        <input type="search" id="q" placeholder="Search artists, albums and songs" value="${esc(s.query)}">
        <button class="pri" data-act="shuffle-all">${icon("shuffle-variant")} Shuffle everything</button>
      </div>
      <div class="tabs">${tabs.map(([k, l]) => `<div class="tab ${s.mode === k ? "on" : ""}" data-mode="${k}">${l}</div>`).join("")}
        ${s.mode === "search" ? `<div class="tab on">Search</div>` : ""}</div>
      ${s.busy ? `<p class="hint">${esc(s.busy)}</p>` : body}`;
  }

  songsHtml(tracks) {
    return tracks.map((t, i) => `<div class="trk" data-song="${i}"><span class="no">${icon("play")}</span>
      <div><div class="nm">${esc(t.title)}</div><div class="meta">${esc([t.artist, t.album].filter(Boolean).join(" · "))}</div></div>
      <span class="meta">${mmss(t.duration)}</span></div>`).join("");
  }

  pageHtml(p) {
    const d = this.s.page || {};
    const crumb = `<div class="crumb"><button data-act="back">${icon("arrow-left")} Back</button>${this.s.stack.length > 1 ? `<button data-act="home">Library</button>` : ""}</div>`;
    if (this.s.busy) return crumb + `<p class="hint">${esc(this.s.busy)}</p>`;
    if (p.type === "album") {
      const t = d.tracks || [];
      this._tracks = t;
      const total = t.reduce((n, x) => n + (x.duration || 0), 0);
      const genre = t.find((x) => x.genre)?.genre;
      const year = t.find((x) => x.year)?.year;
      const multiDisc = new Set(t.map((x) => x.disc).filter(Boolean)).size > 1;
      return crumb + `<div class="head">
          <div style="position:relative"><div class="bigph">${icon("album")}</div>${d.cover ? `<img src="${esc(d.cover)}" alt="" style="position:absolute;inset:0" onerror="this.remove()">` : ""}</div>
          <div><h2>${esc(p.album)}</h2>
            <div class="sub"><span class="link" data-act="artist">${esc(p.artist)}</span>${year ? ` · ${esc(year)}` : ""}${genre ? ` · <span class="link" data-act="genre">${esc(genre)}</span>` : ""} · ${t.length} songs, ${Math.round(total / 60)} min</div>
            <div class="ctrls"><button class="pri" data-act="play-album">${icon("play")} Play</button>
              <button data-act="shuffle-album">${icon("shuffle-variant")} Shuffle</button><button data-act="queue-album">${icon("playlist-plus")} Add to queue</button></div></div></div>
        <div>${t.map((x, i) => `<div class="trk" data-trk="${i}"><span class="no">${esc(multiDisc && x.disc ? `${String(x.disc).split("/")[0]}-` : "")}${esc(String(x.track || i + 1).split("/")[0])}</span>
          <div><div class="nm">${esc(x.title)}</div>${x.artist && x.artist !== p.artist ? `<div class="meta">${esc(x.artist)}</div>` : ""}</div>
          <span class="meta">${mmss(x.duration)}</span></div>`).join("")}</div>`;
    }
    if (p.type === "artist") {
      const albums = d.albums || [];
      return crumb + `<div class="head" style="align-items:center"><div class="av" style="width:72px;height:72px;font-size:30px">${esc(firstLetter(p.artist))}</div>
          <div><h2>${esc(p.artist)}</h2><div class="sub">${albums.length} album${albums.length === 1 ? "" : "s"}</div>
          <div class="ctrls"><button class="pri" data-act="shuffle-artist">${icon("shuffle-variant")} Shuffle</button><button data-act="play-artist">${icon("play")} Play all</button>
            <button data-act="queue-artist">${icon("playlist-plus")} Add to queue</button></div></div></div>` + this.albumGrid(albums, false);
    }
    if (p.type === "genre") {
      const arts = d.artists || [];
      this._artists = arts;
      return crumb + `<div class="head" style="align-items:center"><div><h2>${esc(p.genre)}</h2>
          <div class="sub">${arts.length} artists · ${(d.albums || []).length} albums</div>
          <div class="ctrls"><button class="pri" data-act="shuffle-genre">${icon("shuffle-variant")} Shuffle ${esc(p.genre)}</button>
            <button data-act="play-genre">${icon("play")} Play all</button></div></div></div>
        <h3>Artists</h3><div class="chips">${arts.map((a, i) => `<span class="chip" data-art="${i}">${esc(a)}</span>`).join("")}</div>
        <h3>Albums</h3>${this.albumGrid(d.albums || [])}`;
    }
    return crumb;
  }

  render() {
    const s = this.s;
    const b = this.box();
    const top = s.stack[s.stack.length - 1];
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><div class="wrap">
      <div class="top"><h1>FLACs</h1>
        ${s.boxes.length > 1 ? `<select id="box">${s.boxes.map((x) => `<option value="${x.entry_id}" ${x.entry_id === s.entry ? "selected" : ""}>${esc(x.title)}</option>`).join("")}</select>` : ""}
        ${b ? `<span class="meta">${b.artists} artists · ${b.albums} albums · ${b.songs} songs</span>` : ""}</div>
      ${!s.boxes.length ? `<p class="hint">${esc(s.busy || "None of your moOde boxes has a music library. Add music in moOde (Library → Update) and it shows up here.")}</p>` : `
      <p class="where" id="where">${this.whereHtml()}</p>
      <div id="np">${this.npHtml()}</div>
      ${top ? this.pageHtml(top) : this.homeHtml()}`}
      </div><div id="toast">${s.toast ? `<div class="toast">${esc(s.toast)}</div>` : ""}</div>`;
    this.bind();
  }

  renderLive() {
    const w = this.shadowRoot.getElementById("where");
    const n = this.shadowRoot.getElementById("np");
    if (w) w.innerHTML = this.whereHtml();
    if (n) n.innerHTML = this.npHtml();
  }

  renderToast() {
    const t = this.shadowRoot.getElementById("toast");
    if (t) t.innerHTML = this.s.toast ? `<div class="toast">${esc(this.s.toast)}</div>` : "";
  }

  bind() {
    const root = this.shadowRoot;
    root.getElementById("box")?.addEventListener("change", (e) => {
      this.s.entry = e.target.value; this.s.data = {}; this.s.stack = []; this.s.letter = ""; this.load();
    });
    const q = root.getElementById("q");
    q?.addEventListener("input", (e) => {
      this.s.query = e.target.value;
      clearTimeout(this._qt);
      this._qt = setTimeout(async () => {
        if (this.s.query.trim()) { this.s.mode = "search"; }
        else if (this.s.mode === "search") { this.s.mode = "artists"; }
        await this.load();
        const nq = this.shadowRoot.getElementById("q");
        if (nq) { nq.focus(); nq.setSelectionRange(nq.value.length, nq.value.length); }
      }, 400);
    });
    root.querySelector(".wrap").addEventListener("click", (e) => {
      const el = e.target.closest("[data-act],[data-mode],[data-letter],[data-alb],[data-art],[data-gen],[data-trk],[data-song]");
      if (!el) return;
      const d = el.dataset;
      const top = this.s.stack[this.s.stack.length - 1];
      if (d.mode) { this.s.mode = d.mode; this.s.letter = ""; this.s.limit = PAGE; this.s.query = ""; return this.load(); }
      if (d.letter !== undefined) { if (!el.classList.contains("off")) { this.s.letter = d.letter; this.s.limit = PAGE; this.save(); this.render(); } return; }
      if (d.alb !== undefined) { const a = this._albums[+d.alb]; return this.open({ type: "album", artist: a.artist, album: a.album }); }
      if (d.art !== undefined) return this.open({ type: "artist", artist: this._artists[+d.art] });
      if (d.gen !== undefined) return this.open({ type: "genre", genre: this._genres[+d.gen] });
      if (d.trk !== undefined) return this.play("album", { artist: top.artist, album: top.album, position: +d.trk }, `“${this._tracks[+d.trk].title}”`);
      if (d.song !== undefined) return this.playSong(this._songs[+d.song]);
      switch (d.act) {
        case "back": return this.back();
        case "home": return this.home();
        case "more": this.s.limit += PAGE * 2; return this.render();
        case "prev": return this.player("media_previous_track");
        case "next": return this.player("media_next_track");
        case "pp": return this.player("media_play_pause");
        case "shuffle-all": return this.play("all", { shuffle: true }, "your whole library");
        case "artist": return this.open({ type: "artist", artist: top.artist });
        case "genre": return this.open({ type: "genre", genre: this._tracks.find((x) => x.genre)?.genre });
        case "play-album": return this.play("album", { artist: top.artist, album: top.album }, top.album);
        case "shuffle-album": return this.play("album", { artist: top.artist, album: top.album, shuffle: true }, top.album);
        case "queue-album": return this.play("album", { artist: top.artist, album: top.album, queue: true }, top.album);
        case "play-artist": return this.play("artist", { artist: top.artist }, top.artist);
        case "shuffle-artist": return this.play("artist", { artist: top.artist, shuffle: true }, top.artist);
        case "queue-artist": return this.play("artist", { artist: top.artist, queue: true }, top.artist);
        case "play-genre": return this.play("genre", { genre: top.genre }, top.genre);
        case "shuffle-genre": return this.play("genre", { genre: top.genre, shuffle: true }, top.genre);
      }
    });
  }
}

if (!customElements.get("moode-library-card")) {
  customElements.define("moode-library-card", MoodeLibraryCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "moode-library-card", name: "moOde music library",
    description: "Browse, search and play the music stored on a moOde box." });
}
