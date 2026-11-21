/* ir-remote-card – Fernbedienung für die ZG-IR01-Skripte (todo.ir_codes). */
const ICONS_GERAET = {
  receiver: "mdi:audio-video",
  tv: "mdi:television",
  "led-lampe": "mdi:led-strip-variant",
  sonstiges: "mdi:remote",
};
const ICON_REGELN = [
  [/(kanal\s*\+|ch\s*\+|prog\s*\+)/i, "mdi:chevron-up-box"],
  [/(kanal\s*-|ch\s*-|prog\s*-)/i, "mdi:chevron-down-box"],
  [/(an\/aus|ein\/aus|power|\ban\b|\baus\b|\bon\b|\boff\b)/i, "mdi:power"],
  [/(lauter|vol\s*\+|volume\s*up)/i, "mdi:volume-plus"],
  [/(leiser|vol\s*-|volume\s*down)/i, "mdi:volume-minus"],
  [/(mute|stumm|ton aus)/i, "mdi:volume-off"],
  [/(hoch|oben|\bup\b)/i, "mdi:arrow-up-bold"],
  [/(runter|unten|\bdown\b)/i, "mdi:arrow-down-bold"],
  [/(links|left)/i, "mdi:arrow-left-bold"],
  [/(rechts|right)/i, "mdi:arrow-right-bold"],
  [/(\bok\b|enter|bestätigen)/i, "mdi:checkbox-blank-circle"],
  [/(zurück|back)/i, "mdi:arrow-u-left-top"],
  [/(home|start)/i, "mdi:home"],
  [/(menü|menu)/i, "mdi:menu"],
  [/(quelle|source|input|hdmi)/i, "mdi:import"],
  [/(play|pause)/i, "mdi:play-pause"],
  [/(heller|bright\s*\+)/i, "mdi:brightness-7"],
  [/(dunkler|bright\s*-)/i, "mdi:brightness-5"],
  [/(rot|grün|blau|farbe|color)/i, "mdi:palette"],
  [/(netflix|youtube|prime|disney)/i, "mdi:play-box"],
];

function iconFuerTaste(name) {
  for (const [re, icon] of ICON_REGELN) if (re.test(name)) return icon;
  return "mdi:gesture-tap-button";
}
function slug(s) {
  return s.toLowerCase().replace(/\s+/g, "-");
}
function haSlug(s) {
  return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

class IrRemoteCard extends HTMLElement {
  setConfig(config) {
    this._cfg = Object.assign(
      {
        todo: "todo.ir_codes",
        geraete: "input_select.ir_gerat",
        sender: "input_select.ir_sender",
        status: "input_text.ir_status",
        script_senden: "script.ir_code_senden",
        script_loeschen: "script.ir_code_loeschen",
        title: "IR-Fernbedienung",
        leere_geraete: false,
      },
      config || {}
    );
    this._items = [];
    this._edit = false;
    this._busy = {};
    this._lastTodo = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
    const t = hass.states[this._cfg.todo];
    const stamp = t ? t.last_updated + "|" + t.state : null;
    if (stamp !== this._lastTodo) {
      this._lastTodo = stamp;
      this._laden();
    }
    this._render();
  }

  async _laden() {
    try {
      const res = await this._hass.callWS({ type: "todo/item/list", entity_id: this._cfg.todo });
      this._items = (res.items || []).map((i) => i.summary);
    } catch (e) {
      this._items = [];
    }
    this._render();
  }

  _tasten(geraet) {
    const p = geraet + " | ";
    return this._items
      .filter((s) => s.startsWith(p))
      .map((s) => s.slice(p.length).trim())
      .sort((a, b) => a.localeCompare(b, "de"));
  }

  _senden(geraet, taste) {
    const key = geraet + "|" + taste;
    this._busy[key] = true;
    this._render();
    setTimeout(() => {
      delete this._busy[key];
      this._render();
    }, 1500);
    this._hass.callService("script", "turn_on", {
      entity_id: this._cfg.script_senden,
      variables: { geraet, taste },
    });
  }

  _loeschen(geraet, taste) {
    if (!confirm(`„${taste}“ (${geraet}) wirklich löschen?`)) return;
    this._hass.callService("script", "turn_on", {
      entity_id: this._cfg.script_loeschen,
      variables: { geraet, taste },
    });
  }

  _senderWaehlen(option) {
    this._hass.callService("input_select", "select_option", { entity_id: this._cfg.sender, option });
  }

  _render() {
    if (!this._hass || !this.shadowRoot) return;
    const h = this._hass;
    const sig = JSON.stringify([
      this._items, this._edit, Object.keys(this._busy),
      h.states[this._cfg.geraete]?.attributes?.options,
      h.states[this._cfg.sender]?.state, h.states[this._cfg.sender]?.attributes?.options,
      h.states[this._cfg.status]?.state,
      ((h.states[this._cfg.geraete] || {}).attributes?.options || []).map((g) => h.states["input_select.ir_sender_" + haSlug(g)]?.state),
    ]);
    if (sig === this._sig) return;
    this._sig = sig;
    const geraete = (h.states[this._cfg.geraete] || {}).attributes?.options || [];
    const senderState = h.states[this._cfg.sender];
    const senderOpts = senderState?.attributes?.options || [];
    const sender = senderState?.state;
    const status = h.states[this._cfg.status]?.state || "";

    const chips = senderOpts
      .map((o) => `<button class="chip ${o === sender ? "aktiv" : ""}" data-sender="${esc(o)}">
          <ha-icon icon="${o === "Alle" ? "mdi:access-point-network" : "mdi:access-point"}"></ha-icon>${esc(o.replace("Zigbee IR Remote ", "Sender "))}</button>`)
      .join("");

    const mitTasten = geraete.filter((g) => this._tasten(g).length);
    const sichtbar = this._cfg.leere_geraete || !mitTasten.length ? geraete : mitTasten;
    const bloecke = sichtbar
      .map((g) => {
        const tasten = this._tasten(g);
        const icon = ICONS_GERAET[slug(g)] || "mdi:remote";
        const btns = tasten.length
          ? tasten
              .map((t) => {
                const busy = this._busy[g + "|" + t];
                return `<button class="taste ${busy ? "busy" : ""} ${this._edit ? "edit" : ""}" data-g="${esc(g)}" data-t="${esc(t)}" title="${esc(t)}">
                  <ha-icon icon="${this._edit ? "mdi:delete-outline" : iconFuerTaste(t)}"></ha-icon>
                  <span>${esc(t)}</span></button>`;
              })
              .join("")
          : `<div class="leer">Noch keine Tasten. Auf der Seite „Anlernen“ hinzufügen.</div>`;
        const gs = h.states["input_select.ir_sender_" + haSlug(g)];
        const auswahl = gs
          ? `<select class="gsender" data-entity="${esc(gs.entity_id)}" title="Fester Sender für ${esc(g)}">${(gs.attributes.options || [])
              .map((o) => `<option value="${esc(o)}" ${o === gs.state ? "selected" : ""}>${esc(o === "Standard" ? "Standard (" + (sender || "") + ")" : o).replace("Zigbee IR Remote ", "Sender ")}</option>`)
              .join("")}</select>`
          : "";
        return `<section class="geraet">
            <div class="kopf"><ha-icon icon="${icon}"></ha-icon><span>${esc(g)}</span><small>${tasten.length || ""}</small>${auswahl}</div>
            <div class="raster">${btns}</div></section>`;
      })
      .join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; }
        ha-card { padding: 16px; }
        .top { display:flex; align-items:center; gap:8px; margin-bottom:12px; }
        .top h2 { margin:0; font-size:1.25rem; font-weight:500; flex:1; }
        .icon-btn { background:none; border:none; cursor:pointer; color:var(--secondary-text-color); padding:6px; border-radius:50%; }
        .icon-btn.aktiv { color:var(--error-color); background:color-mix(in srgb, var(--error-color) 12%, transparent); }
        .label { font-size:.8rem; color:var(--secondary-text-color); margin:0 0 6px; text-transform:uppercase; letter-spacing:.04em; }
        .chips { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
        .chip { display:flex; align-items:center; gap:6px; border:1px solid var(--divider-color); background:var(--card-background-color);
                color:var(--primary-text-color); border-radius:18px; padding:6px 12px; font-size:.9rem; cursor:pointer; }
        .chip ha-icon { --mdc-icon-size:18px; }
        .chip.aktiv { background:var(--primary-color); color:var(--text-primary-color, #fff); border-color:var(--primary-color); }
        .status { font-size:.85rem; color:var(--secondary-text-color); background:var(--secondary-background-color);
                  border-radius:10px; padding:8px 12px; margin-bottom:16px; min-height:1.2em; }
        .geraet { margin-bottom:18px; }
        .kopf { display:flex; align-items:center; gap:8px; font-weight:500; margin-bottom:8px; }
        .kopf small { color:var(--secondary-text-color); font-weight:400; flex:1; }
        .gsender { font:inherit; font-size:.8rem; font-weight:400; color:var(--primary-text-color); background:var(--secondary-background-color);
                   border:1px solid var(--divider-color); border-radius:12px; padding:3px 8px; cursor:pointer; }
        .raster { display:grid; grid-template-columns:repeat(auto-fill, minmax(96px, 1fr)); gap:8px; }
        .taste { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:6px; min-height:76px;
                 border:none; border-radius:14px; cursor:pointer; padding:8px 6px; font-size:.82rem; line-height:1.15;
                 background:var(--secondary-background-color); color:var(--primary-text-color);
                 transition: transform .08s ease, background .2s ease; }
        .taste ha-icon { --mdc-icon-size:26px; color:var(--primary-color); }
        .taste span { overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; text-align:center; }
        .taste:active { transform:scale(.95); }
        .taste.busy { background:color-mix(in srgb, var(--primary-color) 22%, transparent); }
        .taste.edit { outline:2px dashed var(--error-color); }
        .taste.edit ha-icon { color:var(--error-color); }
        .leer { grid-column:1/-1; color:var(--secondary-text-color); font-size:.9rem; padding:8px 0; }
      </style>
      <ha-card>
        <div class="top">
          <h2>${esc(this._cfg.title)}</h2>
          <button class="icon-btn ${this._edit ? "aktiv" : ""}" id="edit" title="${this._edit ? "Bearbeiten beenden" : "Tasten löschen"}">
            <ha-icon icon="${this._edit ? "mdi:check" : "mdi:pencil"}"></ha-icon></button>
        </div>
        <div class="label">Standard-Sender</div>
        <div class="chips">${chips}</div>
        <div class="status">${esc(status)}</div>
        ${bloecke}
        ${!this._cfg.leere_geraete && mitTasten.length && mitTasten.length < geraete.length
          ? `<div class="leer">Ohne Tasten: ${esc(geraete.filter((g) => !mitTasten.includes(g)).join(", "))}. Neue Tasten legst du unter „Anlernen“ an.</div>`
          : ""}
      </ha-card>`;

    this.shadowRoot.getElementById("edit").onclick = () => {
      this._edit = !this._edit;
      this._render();
    };
    this.shadowRoot.querySelectorAll(".gsender").forEach(
      (el) => (el.onchange = () => this._hass.callService("input_select", "select_option", { entity_id: el.dataset.entity, option: el.value }))
    );
    this.shadowRoot.querySelectorAll(".chip").forEach((el) => (el.onclick = () => this._senderWaehlen(el.dataset.sender)));
    this.shadowRoot.querySelectorAll(".taste").forEach(
      (el) =>
        (el.onclick = () =>
          this._edit ? this._loeschen(el.dataset.g, el.dataset.t) : this._senden(el.dataset.g, el.dataset.t))
    );
  }

  getCardSize() {
    return 6;
  }
  getGridOptions() {
    return { columns: "full", rows: "auto" };
  }
}

if (!customElements.get("ir-remote-card")) customElements.define("ir-remote-card", IrRemoteCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "ir-remote-card", name: "IR-Fernbedienung", description: "Tasten aus todo.ir_codes senden" });
