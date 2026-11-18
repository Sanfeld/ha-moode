/*
 * moOde Stations manager – Home Assistant sidebar panel.
 * Lists, checks, deletes, favourites and adds radio stations on any moOde box,
 * searches radio-browser.info, and syncs station lists between boxes.
 */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const STYLE = `
:host { display:block; color:var(--primary-text-color); background:var(--primary-background-color); min-height:100vh; }
.bar { display:flex; align-items:center; gap:8px; padding:0 12px; height:56px; background:var(--app-header-background-color, var(--primary-color)); color:var(--app-header-text-color, #fff); position:sticky; top:0; z-index:2; }
.bar h1 { font-size:20px; font-weight:400; margin:0; flex:1; }
.bar select { background:transparent; color:inherit; border:1px solid rgba(255,255,255,.5); border-radius:6px; padding:6px; font-size:14px; }
.bar select option { color:#000; }
.wrap { max-width:1000px; margin:0 auto; padding:12px; }
.tabs { display:flex; gap:4px; margin-bottom:12px; border-bottom:1px solid var(--divider-color); }
.tab { padding:10px 16px; cursor:pointer; border-bottom:2px solid transparent; color:var(--secondary-text-color); }
.tab.on { border-color:var(--primary-color); color:var(--primary-text-color); }
.card { background:var(--card-background-color); border-radius:12px; padding:12px; margin-bottom:12px; box-shadow:var(--ha-card-box-shadow, none); border:1px solid var(--divider-color); }
.row { display:flex; align-items:center; gap:10px; padding:8px 4px; border-bottom:1px solid var(--divider-color); }
.row:last-child { border-bottom:none; }
.row img, .row .noimg { width:40px; height:40px; border-radius:6px; object-fit:contain; background:var(--secondary-background-color); flex:none; }
.row .txt { flex:1; min-width:0; }
.row .nm { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.row .meta { font-size:12px; color:var(--secondary-text-color); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ctrls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:8px; }
input[type=text], input[type=url], select.f { padding:8px 10px; border:1px solid var(--divider-color); border-radius:8px; background:var(--card-background-color); color:var(--primary-text-color); font-size:14px; }
input.grow { flex:1; min-width:160px; }
button { padding:7px 12px; border-radius:8px; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color); cursor:pointer; font-size:13px; }
button.pri { background:var(--primary-color); color:var(--text-primary-color, #fff); border-color:var(--primary-color); }
button.danger { color:var(--error-color); }
button:disabled { opacity:.5; cursor:default; }
button.ic { border:none; background:none; font-size:18px; padding:4px 6px; }
.ok { color:var(--success-color, #4caf50); } .bad { color:var(--error-color); } .muted { color:var(--secondary-text-color); }
.badge { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid currentColor; white-space:nowrap; }
.toast { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); background:var(--primary-text-color); color:var(--card-background-color); padding:10px 16px; border-radius:8px; font-size:14px; z-index:5; max-width:90vw; }
.hint { font-size:13px; color:var(--secondary-text-color); margin:4px 0 10px; }
h2 { font-size:16px; font-weight:500; margin:4px 0 10px; }
label.cb { display:flex; align-items:center; gap:6px; font-size:14px; }
`;

const EMBED_STYLE = `
:host { min-height:0; background:transparent; }
.bar { background:transparent; color:var(--primary-text-color); position:static; height:48px; padding:0 12px; }
.bar select { border-color:var(--divider-color); }
.bar select option { color:initial; }
.wrap { padding:0 12px 12px; }
`;

// The self-building dashboard re-creates this card whenever the favourites
// change (a star adds/removes a favourite button). Keep the view the user had
// (box, filter, search, tab) for the life of the page, so it survives that.
const VIEW = {};
const VIEW_KEYS = ["entry", "show", "filter", "tab"];

class MoodeStationsPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.s = {
      boxes: [], entry: null, stations: [], status: {}, filter: "", show: "all",
      sel: new Set(), tab: "mine", results: [], added: new Set(), busy: "", syncOpen: false,
    };
    VIEW_KEYS.forEach((k) => { if (VIEW[k] !== undefined) this.s[k] = VIEW[k]; });
  }

  saveView() { VIEW_KEYS.forEach((k) => { VIEW[k] = this.s[k]; }); }

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (this._menu) this._menu.hass = h;
    if (first) this._init();
  }
  set narrow(n) { this._narrow = n; if (this._menu) this._menu.narrow = n; }

  ws(type, data = {}) { return this._hass.callWS({ type, ...data }); }

  toast(msg) {
    this.s.toast = msg; this.render();
    clearTimeout(this._tt); this._tt = setTimeout(() => { this.s.toast = ""; this.render(); }, 4000);
  }

  async _init() {
    this.render();
    try {
      this.s.boxes = await this.ws("moode/boxes");
      if (!this.s.boxes.some((b) => b.entry_id === this.s.entry)) this.s.entry = this.s.boxes[0]?.entry_id || null;
      await this.load();
    } catch (e) { this.toast(`Couldn't load boxes: ${e.message || e}`); }
  }

  async load(retries = 0) {
    if (!this.s.entry) return this.render();
    const box = this.s.entry;
    this.s.busy = "Loading stations…"; this.render();
    for (let attempt = 0; ; attempt++) {
      try {
        const list = await this.ws("moode/stations", { entry_id: box });
        if (box !== this.s.entry) return;            // user switched box meanwhile
        this.s.stations = list; this.s.sel.clear(); this.s.loadError = "";
        break;
      } catch (e) {
        if (attempt < retries) { await new Promise((r) => setTimeout(r, 2000)); continue; }
        // Never blank the list on a failed read: keep what we had and say so.
        this.s.loadError = `Couldn't refresh the station list (${e.message || e}). Showing the last known list. Refresh to try again.`;
        break;
      }
    }
    this.s.busy = ""; this.render();
  }

  visible() {
    const f = this.s.filter.toLowerCase();
    return this.s.stations.filter((st) => {
      if (f && !`${st.name} ${st.genre} ${st.country}`.toLowerCase().includes(f)) return false;
      const k = this.s.status[st.url];
      if (this.s.show === "broken") return k === "dead";
      if (this.s.show === "unsure") return k === "unsure";
      if (this.s.show === "unchecked") return k === undefined;
      if (this.s.show === "fav") return st.favourite;
      if (this.s.show === "builtin") return st.builtin;
      if (this.s.show === "user") return !st.builtin;
      return true;
    });
  }

  boxTitle(id) { return this.s.boxes.find((b) => b.entry_id === id)?.title || ""; }

  // ------------------------------------------------------------------ actions
  async checkAll() {
    const urls = this.visible().map((s) => s.url);
    if (!urls.length) return;
    for (const u of urls) this.s.status[u] = "checking";
    this.s.busy = `Checking ${urls.length} stations…`; this.render();
    for (let i = 0; i < urls.length; i += 20) {
      try {
        const res = await this.ws("moode/stations/check", { urls: urls.slice(i, i + 20) });
        Object.assign(this.s.status, res);
      } catch (e) { this.toast(`Check failed: ${e.message || e}`); break; }
      this.s.busy = `Checked ${Math.min(i + 20, urls.length)} of ${urls.length}…`; this.renderList();
    }
    const broken = this.s.stations.filter((s) => this.s.status[s.url] === "dead").length;
    const unsure = this.s.stations.filter((s) => this.s.status[s.url] === "unsure").length;
    this.s.busy = ""; this.render();
    if (urls.length >= 10 && broken / urls.length > 0.6)
      return this.toast(`${broken} of ${urls.length} look broken. That's unusually many; check that HA can reach the internet before deleting.`);
    this.toast((broken ? `${broken} broken` : "No broken stations") + (unsure ? `, ${unsure} unsure (slow; not included in Delete broken)` : ""));
  }

  async del(names) {
    if (!names.length) return;
    const what = names.length === 1 ? `"${names[0]}"` : `${names.length} stations`;
    if (!confirm(`Delete ${what} from ${this.boxTitle(this.s.entry)}? This can't be undone.`)) return;
    this.s.busy = `Deleting ${what}…`; this.render();
    try {
      const r = await this.ws("moode/stations/delete", { entry_id: this.s.entry, names });
      this.toast(`Deleted ${r.deleted.length}` + (r.failed.length ? `, ${r.failed.length} failed: ${r.failed[0].error}` : ""));
    } catch (e) { this.toast(`Delete failed: ${e.message || e}`); }
    await new Promise((r) => setTimeout(r, 1500));
    await this.load(3);
  }

  async fav(name, on) {
    try {
      await this.ws("moode/stations/favourite", { entry_id: this.s.entry, name, favourite: on });
      const st = this.s.stations.find((s) => s.name === name); if (st) st.favourite = on;
      this.renderList();
      this.toast(on ? `★ ${name} added to favourites` : `${name} removed from favourites`);
    } catch (e) { this.toast(`Couldn't change favourite: ${e.message || e}`); }
  }

  async play(url, name) {
    try { await this.ws("moode/stations/play", { entry_id: this.s.entry, url }); this.toast(`Playing ${name} on ${this.boxTitle(this.s.entry)}`); }
    catch (e) { this.toast(`Couldn't play: ${e.message || e}`); }
  }

  async search() {
    const q = (id) => this.shadowRoot.getElementById(id)?.value.trim() || "";
    const name = q("q-name"), country = q("q-country"), tag = q("q-tag");
    if (!name && !country && !tag) return this.toast("Type a name, country or genre first");
    this.s.busy = "Searching radio-browser.info…"; this.render();
    try { this.s.results = await this.ws("moode/stations/search", { name, country, tag, limit: 80 }); }
    catch (e) { this.toast(`Search failed: ${e.message || e}`); this.s.results = []; }
    this.s.busy = ""; this.render();
    if (!this.s.results.length) this.toast("No stations found");
  }

  async add(stations) {
    this.s.busy = `Adding ${stations.length === 1 ? stations[0].name : stations.length + " stations"}…`; this.render();
    try {
      const r = await this.ws("moode/stations/add", { entry_id: this.s.entry, stations });
      r.added.forEach((n) => this.s.added.add(n.toLowerCase()));
      stations.forEach((s) => { if (r.added.includes(s.name)) this.s.added.add(s.url); });
      this.toast(r.added.length ? `Added ${r.added.join(", ")}` : `Not added: ${r.failed.map((f) => `${f.name} (${f.error})`).join(", ")}`);
      await this.load(3);
    } catch (e) { this.toast(`Add failed: ${e.message || e}`); this.s.busy = ""; this.render(); }
  }

  async manualAdd() {
    const v = (id) => this.shadowRoot.getElementById(id).value.trim();
    const st = { name: v("m-name"), url: v("m-url"), genre: v("m-genre"), country: v("m-country") };
    if (!st.name || !/^https?:\/\//.test(st.url)) return this.toast("Enter a name and a stream URL starting with http");
    await this.add([st]);
  }

  async sync() {
    const targets = [...this.shadowRoot.querySelectorAll("input[data-sync]:checked")].map((c) => c.dataset.sync);
    const mirror = this.shadowRoot.getElementById("s-mirror")?.checked;
    if (!targets.length) return this.toast("Tick at least one box");
    const names = targets.map((t) => this.boxTitle(t)).join(", ");
    const msg = mirror
      ? `Make ${names} match ${this.boxTitle(this.s.entry)} exactly? Stations they have that ${this.boxTitle(this.s.entry)} doesn't will be deleted.`
      : `Copy ${this.boxTitle(this.s.entry)}'s stations that are missing on ${names}?`;
    if (!confirm(msg)) return;
    this.s.busy = "Syncing… this can take a minute with logos"; this.s.syncOpen = false; this.render();
    try {
      const r = await this.ws("moode/stations/sync", { source: this.s.entry, targets, mirror: !!mirror });
      this.toast(Object.entries(r).map(([id, x]) => x.error ? `${this.boxTitle(id)}: ${x.error}` :
        `${this.boxTitle(id)}: +${x.added}${mirror ? ` −${x.removed}` : ""}${x.failed.length ? `, ${x.failed.length} failed` : ""}`).join(" · "));
    } catch (e) { this.toast(`Sync failed: ${e.message || e}`); }
    this.s.busy = ""; this.render();
  }

  async backup() {
    const names = this.s.stations.map((s) => s.name);
    if (!names.length) return this.toast("Nothing to back up");
    const out = [];
    try {
      for (let i = 0; i < names.length; i += 20) {
        this.s.busy = `Backing up ${Math.min(i + 20, names.length)} of ${names.length} (with logos)…`; this.renderList();
        out.push(...(await this.ws("moode/stations/backup", { entry_id: this.s.entry, names: names.slice(i, i + 20) })));
      }
    } catch (e) { this.s.busy = ""; this.renderList(); return this.toast(`Backup failed: ${e.message || e}`); }
    const box = this.boxTitle(this.s.entry);
    const file = { format: "moode-stations", version: 1, box, created: new Date().toISOString(), stations: out };
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(file)], { type: "application/json" }));
    a.download = `moode-stations-${box.replace(/\W+/g, "-").toLowerCase()}-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    this.s.busy = ""; this.renderList();
    this.toast(`Saved ${out.length} stations to ${a.download}`);
  }

  async restore(file) {
    let data;
    try { data = JSON.parse(await file.text()); } catch { return this.toast("That isn't a station backup file"); }
    const list = Array.isArray(data) ? data : data?.stations;
    if (!Array.isArray(list) || !list.length || !list[0].name) return this.toast("That isn't a station backup file");
    const box = this.boxTitle(this.s.entry);
    if (!confirm(`Restore ${list.length} stations${data.box ? ` (backed up from ${data.box})` : ""} to ${box}?\nStations ${box} already has are left alone.`)) return;
    const exact = confirm(`Also make ${box} match the backup exactly?\nOK = delete stations that aren't in the backup.\nCancel = only add the missing ones.`);
    const keep = list.map((s) => s.name);
    let added = 0, removed = 0, failed = 0;
    try {
      for (let i = 0; i < list.length; i += 10) {
        const last = i + 10 >= list.length;
        this.s.busy = `Restoring ${Math.min(i + 10, list.length)} of ${list.length}…`; this.renderList();
        const r = await this.ws("moode/stations/restore", {
          entry_id: this.s.entry,
          stations: list.slice(i, i + 10).map(({ builtin, ...st }) => st),
          delete_others: exact && last, keep: exact && last ? keep : [],
        });
        added += r.added.length; removed += r.removed.length; failed += r.failed.length;
      }
    } catch (e) { this.toast(`Restore failed: ${e.message || e}`); }
    this.toast(`Restored: +${added}${exact ? ` −${removed}` : ""}${failed ? `, ${failed} failed` : ""}`);
    this.shadowRoot.getElementById("restore-file").value = "";
    await this.load(3);
  }

  // ------------------------------------------------------------------ rendering
  statusBadge(url) {
    const k = this.s.status[url];
    if (k === "checking") return `<span class="badge muted">checking</span>`;
    if (k === "ok") return `<span class="badge ok">works</span>`;
    if (k === "dead") return `<span class="badge bad">broken</span>`;
    if (k === "unsure") return `<span class="badge muted" title="Timed out twice: maybe slow or busy, maybe gone. Try ▶ to listen.">unsure</span>`;
    return "";
  }

  listHtml() {
    const rows = this.visible();
    if (!this.s.stations.length) return `<p class="hint">${this.s.busy ? "" : "No stations on this box."}</p>`;
    if (!rows.length) return `<p class="hint">No stations match.</p>`;
    return rows.map((st) => `
      <div class="row">
        <input type="checkbox" data-sel="${esc(st.name)}" ${this.s.sel.has(st.name) ? "checked" : ""}>
        <img src="${esc(st.logo)}" loading="lazy" onerror="this.style.visibility='hidden'">
        <div class="txt"><div class="nm">${esc(st.name)}</div>
          <div class="meta">${esc([st.builtin ? "built-in" : "", st.genre, st.country, st.bitrate && st.bitrate + " kbps", st.format].filter(Boolean).join(" · "))}</div></div>
        ${this.statusBadge(st.url)}
        <button class="ic" title="Favourite" data-fav="${esc(st.name)}" data-on="${st.favourite ? 0 : 1}">${st.favourite ? "★" : "☆"}</button>
        <button class="ic" title="Play on this box" data-play="${esc(st.url)}" data-name="${esc(st.name)}">▶</button>
        <button class="ic" title="Edit" data-edit="${esc(st.name)}">✎</button>
        <button class="ic danger" title="Delete" data-del="${esc(st.name)}">🗑</button>
      </div>`).join("");
  }

  renderList() {
    const el = this.shadowRoot.getElementById("list");
    if (el) el.innerHTML = this.listHtml();
    const b = this.shadowRoot.getElementById("busy");
    if (b) b.textContent = this.s.busy;
    const broken = this.s.stations.filter((s) => this.s.status[s.url] === "dead").length;
    const db = this.shadowRoot.getElementById("del-broken");
    if (db) { db.textContent = `Delete broken (${broken})`; db.disabled = !broken; }
    const bi = this.s.stations.filter((s) => s.builtin).length;
    const dbi = this.shadowRoot.getElementById("del-builtin");
    if (dbi) { dbi.textContent = `Delete built-in (${bi})`; dbi.disabled = !bi; }
    const sa = this.shadowRoot.getElementById("sel-all");
    if (sa) {
      const shown = this.visible();
      sa.textContent = shown.length && shown.every((s) => this.s.sel.has(s.name)) ? "Deselect all" : "Select all shown";
    }
    const ds = this.shadowRoot.getElementById("del-sel");
    if (ds) { ds.textContent = `Delete selected (${this.s.sel.size})`; ds.disabled = !this.s.sel.size; }
  }

  mineHtml() {
    const others = this.s.boxes.filter((b) => b.entry_id !== this.s.entry);
    return `
      <div class="card">
        <div class="ctrls">
          <input id="filter" type="text" class="grow" placeholder="Search ${this.s.stations.length} stations…" value="${esc(this.s.filter)}">
          <select id="show" class="f">
            ${[["all", "All"], ["broken", "Broken"], ["unsure", "Unsure (slow)"], ["unchecked", "Not checked"], ["fav", "Favourites"], ["builtin", "Built-in (moOde)"], ["user", "Added by you"]]
              .map(([v, l]) => `<option value="${v}" ${this.s.show === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
        </div>
        <div class="ctrls">
          <button id="check" class="pri">Check stations</button>
          <button id="sel-all">Select all shown</button>
          <button id="del-sel" class="danger" disabled>Delete selected (0)</button>
          <button id="del-broken" class="danger" disabled>Delete broken (0)</button>
          ${others.length ? `<button id="sync-open">Sync to other boxes…</button>` : ""}
        </div>
        <div class="ctrls">
          <button id="backup">Back up…</button>
          <button id="restore">Restore…</button>
          <input id="restore-file" type="file" accept=".json,application/json" style="display:none">
          <button id="del-builtin" class="danger" disabled>Delete built-in (0)</button>
        </div>
        ${this.s.syncOpen ? `
          <div class="card">
            <h2>Copy ${esc(this.boxTitle(this.s.entry))}'s stations to</h2>
            ${others.map((b) => `<label class="cb"><input type="checkbox" data-sync="${b.entry_id}" checked> ${esc(b.title)}</label>`).join("")}
            <label class="cb" style="margin-top:8px"><input type="checkbox" id="s-mirror"> Make them identical (also delete stations they have extra)</label>
            <div class="ctrls" style="margin-top:10px"><button id="sync-go" class="pri">Sync</button><button id="sync-cancel">Cancel</button></div>
          </div>` : ""}
        ${this.s.editing ? this.editHtml() : ""}
        ${this.s.loadError ? `<p class="bad">${esc(this.s.loadError)}</p>` : ""}
        <div class="hint" id="busy">${esc(this.s.busy)}</div>
        <div id="list">${this.listHtml()}</div>
      </div>`;
  }

  editHtml() {
    const st = this.s.stations.find((x) => x.name === this.s.editing);
    if (!st) return "";
    return `
      <div class="card" id="edit-card">
        <h2>Edit station</h2>
        <div class="ctrls">
          <img id="e-preview" src="${esc(this.s.editLogo || st.logo)}" style="width:64px;height:64px;border-radius:8px;object-fit:contain;background:var(--secondary-background-color)">
          <div style="display:flex;flex-direction:column;gap:6px">
            <button id="e-logo-btn">Change logo…</button>
            <input id="e-logo" type="file" accept="image/*" style="display:none">
          </div>
        </div>
        <div class="ctrls"><input id="e-name" type="text" class="grow" value="${esc(st.name)}" placeholder="Name"></div>
        <div class="ctrls"><input id="e-url" type="url" class="grow" value="${esc(st.url)}" placeholder="https://stream…"></div>
        <div class="ctrls">
          <input id="e-genre" type="text" value="${esc(st.genre)}" placeholder="Genre">
          <input id="e-country" type="text" value="${esc(st.country)}" placeholder="Country">
          <input id="e-bitrate" type="text" value="${esc(st.bitrate)}" placeholder="kbps" style="width:80px" title="Bitrate in kbps (read from the stream automatically when the URL changes)">
        </div>
        <div class="ctrls">
          <button id="e-save" class="pri">Save</button>
          <button id="e-test">▶ Test URL</button>
          <button id="e-cancel">Cancel</button>
        </div>
      </div>`;
  }

  async pickLogo(file) {
    // Resize to a ≤400px JPEG (moOde stores logos as .jpg) and preview it.
    const url = URL.createObjectURL(file);
    const img = await new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok(i); i.onerror = bad; i.src = url; });
    const scale = Math.min(1, 400 / Math.max(img.width, img.height));
    const c = document.createElement("canvas");
    c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
    const g = c.getContext("2d"); g.fillStyle = "#000"; g.fillRect(0, 0, c.width, c.height);
    g.drawImage(img, 0, 0, c.width, c.height);
    URL.revokeObjectURL(url);
    this.s.editLogo = c.toDataURL("image/jpeg", 0.88);
    const p = this.shadowRoot.getElementById("e-preview"); if (p) p.src = this.s.editLogo;
  }

  async saveEdit() {
    const v = (id) => this.shadowRoot.getElementById(id).value.trim();
    const name = this.s.editing;
    const data = { entry_id: this.s.entry, name, new_name: v("e-name"), url: v("e-url"), genre: v("e-genre"), country: v("e-country"), bitrate: v("e-bitrate") };
    if (!data.new_name) return this.toast("A station needs a name");
    if (!/^https?:\/\//.test(data.url)) return this.toast("The stream URL must start with http:// or https://");
    if (this.s.editLogo) data.logo_data = this.s.editLogo;
    this.s.busy = `Saving ${name}…`; this.renderList();
    try {
      const r = await this.ws("moode/stations/update", data);
      this.toast((r.name !== name ? `Renamed to ${r.name}` : `Saved ${name}`) +
        (this.s.editLogo && r.logo_ok === false ? " (moOde didn't take the logo, see the HA log)" : ""));
      const hadLogo = !!this.s.editLogo;
      this.s.editing = null; this.s.editLogo = null;
      // moOde moves a new logo into place ~3 s after saving the station
      await new Promise((ok) => setTimeout(ok, hadLogo ? 5000 : 1500));
      await this.load(3);
    } catch (e) { this.s.busy = ""; this.renderList(); this.toast(`Couldn't save: ${e.message || e}`); }
  }

  addHtml() {
    const res = this.s.results.map((r, i) => {
      const done = this.s.added.has(r.url) || this.s.stations.some((s) => s.name.toLowerCase() === r.name.toLowerCase());
      const img = r.logo_url && r.logo_url.startsWith("https://") ? `<img src="${esc(r.logo_url)}" loading="lazy" onerror="this.style.visibility='hidden'">` : `<div class="noimg"></div>`;
      return `<div class="row">${img}
        <div class="txt"><div class="nm">${esc(r.name)}</div>
          <div class="meta">${esc([r.genre, r.country, r.bitrate && r.bitrate !== "0" && r.bitrate + " kbps", r.format].filter(Boolean).join(" · "))}</div></div>
        <button class="ic" title="Listen on ${esc(this.boxTitle(this.s.entry))}" data-play="${esc(r.url)}" data-name="${esc(r.name)}">▶</button>
        <button data-add="${i}" ${done ? "disabled" : ""}>${done ? "Added ✓" : "Add"}</button></div>`;
    }).join("");
    return `
      <div class="card">
        <h2>Find stations</h2>
        <p class="hint">Searches radio-browser.info, a free directory of ~50,000 stations. ▶ test-plays on ${esc(this.boxTitle(this.s.entry))}.</p>
        <div class="ctrls">
          <input id="q-name" type="text" class="grow" placeholder="Name, e.g. P4 or Jazz FM">
          <input id="q-country" type="text" placeholder="Country, e.g. SE or Sweden" style="width:170px">
          <input id="q-tag" type="text" placeholder="Genre, e.g. rock" style="width:140px">
          <button id="q-go" class="pri">Search</button>
        </div>
        <div class="hint">${esc(this.s.busy)}</div>
        <div>${res}</div>
      </div>
      <div class="card">
        <h2>Add by stream URL</h2>
        <div class="ctrls">
          <input id="m-name" type="text" placeholder="Station name" class="grow">
          <input id="m-url" type="url" placeholder="https://stream.example.com/live.mp3" class="grow">
        </div>
        <div class="ctrls">
          <input id="m-genre" type="text" placeholder="Genre (optional)">
          <input id="m-country" type="text" placeholder="Country (optional)">
          <button id="m-go" class="pri">Add station</button>
        </div>
      </div>`;
  }

  render() {
    const s = this.s;
    this.saveView();
    this.shadowRoot.innerHTML = `<style>${STYLE}${this._embedded ? EMBED_STYLE : ""}</style>
      <div class="bar"><span id="menu"></span><h1>Stations</h1>
        ${s.boxes.length ? `<select id="box">${s.boxes.map((b) => `<option value="${b.entry_id}" ${b.entry_id === s.entry ? "selected" : ""}>${esc(b.title)}</option>`).join("")}</select>` : ""}
      </div>
      <div class="wrap">
        ${!s.boxes.length ? `<p class="hint">${s.busy || "No moOde boxes found. Add one under Settings → Devices & services."}</p>` : `
        <div class="tabs">
          <div class="tab ${s.tab === "mine" ? "on" : ""}" data-tab="mine">Stations on ${esc(this.boxTitle(s.entry))}</div>
          <div class="tab ${s.tab === "add" ? "on" : ""}" data-tab="add">Add stations</div>
        </div>
        ${s.tab === "mine" ? this.mineHtml() : this.addHtml()}`}
      </div>
      ${s.toast ? `<div class="toast">${esc(s.toast)}</div>` : ""}`;
    if (!this._embedded && customElements.get("ha-menu-button")) {
      this._menu = document.createElement("ha-menu-button");
      this._menu.hass = this._hass; this._menu.narrow = this._narrow;
      this.shadowRoot.getElementById("menu").appendChild(this._menu);
    }
    this.bind();
    this.renderList();
  }

  bind() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    $("box")?.addEventListener("change", (e) => { this.s.entry = e.target.value; this.s.status = {}; this.s.results = []; this.load(); });
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((t) => t.addEventListener("click", () => { this.s.tab = t.dataset.tab; this.render(); }));
    $("filter")?.addEventListener("input", (e) => { this.s.filter = e.target.value; this.saveView(); this.renderList(); });
    $("show")?.addEventListener("change", (e) => { this.s.show = e.target.value; this.saveView(); this.renderList(); });
    $("check")?.addEventListener("click", () => this.checkAll());
    $("sel-all")?.addEventListener("click", () => {
      const shown = this.visible();
      const allSel = shown.length && shown.every((s) => this.s.sel.has(s.name));
      if (allSel) this.s.sel.clear(); else shown.forEach((s) => this.s.sel.add(s.name));
      this.renderList();
    });
    $("del-sel")?.addEventListener("click", () => this.del([...this.s.sel]));
    $("del-broken")?.addEventListener("click", () => this.del(this.s.stations.filter((s) => this.s.status[s.url] === "dead").map((s) => s.name)));
    $("sync-open")?.addEventListener("click", () => { this.s.syncOpen = true; this.render(); });
    $("sync-cancel")?.addEventListener("click", () => { this.s.syncOpen = false; this.render(); });
    $("sync-go")?.addEventListener("click", () => this.sync());
    $("backup")?.addEventListener("click", () => this.backup());
    $("restore")?.addEventListener("click", () => $("restore-file").click());
    $("restore-file")?.addEventListener("change", (e) => e.target.files[0] && this.restore(e.target.files[0]));
    $("del-builtin")?.addEventListener("click", () => {
      const names = this.s.stations.filter((s) => s.builtin).map((s) => s.name);
      if (names.length && confirm(`Tip: press "Back up…" first, so you can restore them later.\n\nContinue to delete ${names.length} built-in stations?`)) this.del(names);
    });
    $("e-cancel")?.addEventListener("click", () => { this.s.editing = null; this.s.editLogo = null; this.render(); });
    $("e-save")?.addEventListener("click", () => this.saveEdit());
    $("e-test")?.addEventListener("click", () => this.play($("e-url").value.trim(), $("e-name").value.trim()));
    $("e-logo-btn")?.addEventListener("click", () => $("e-logo").click());
    $("e-logo")?.addEventListener("change", (e) => e.target.files[0] && this.pickLogo(e.target.files[0]).catch(() => this.toast("Couldn't read that image")));
    $("q-go")?.addEventListener("click", () => this.search());
    ["q-name", "q-country", "q-tag"].forEach((id) => $(id)?.addEventListener("keydown", (e) => e.key === "Enter" && this.search()));
    $("m-go")?.addEventListener("click", () => this.manualAdd());
    // delegated row actions (list is re-rendered on its own)
    this.shadowRoot.querySelector(".wrap").addEventListener("click", (e) => {
      const b = e.target.closest("button, input[data-sel]");
      if (!b) return;
      if (b.dataset.sel !== undefined) { b.checked ? this.s.sel.add(b.dataset.sel) : this.s.sel.delete(b.dataset.sel); this.renderList(); }
      else if (b.dataset.fav !== undefined) this.fav(b.dataset.fav, b.dataset.on === "1");
      else if (b.dataset.play !== undefined) this.play(b.dataset.play, b.dataset.name);
      else if (b.dataset.del !== undefined) this.del([b.dataset.del]);
      else if (b.dataset.edit !== undefined) {
        this.s.editing = b.dataset.edit; this.s.editLogo = null; this.render();
        this.shadowRoot.getElementById("edit-card")?.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      else if (b.dataset.add !== undefined) this.add([this.s.results[+b.dataset.add]]);
    });
  }
}

if (!customElements.get("moode-stations-panel")) customElements.define("moode-stations-panel", MoodeStationsPanel);

/* The same manager as a dashboard card: `type: custom:moode-stations-card`
   (the self-building moOde dashboard puts it on its own "Stations" tab). */
class MoodeStationsCard extends MoodeStationsPanel {
  constructor() { super(); this._embedded = true; }
  setConfig(config) { this._config = config || {}; }
  getCardSize() { return 12; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }
}
if (!customElements.get("moode-stations-card")) {
  customElements.define("moode-stations-card", MoodeStationsCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "moode-stations-card", name: "moOde Stations manager",
    description: "Manage, check, add, back up and sync moOde radio stations." });
}
