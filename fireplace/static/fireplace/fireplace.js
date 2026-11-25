/* Fireplace page: one card per fireplace, ported from hass-napoleon's
 * napoleon-fireplace-card (_build/_render) onto plain DOM + FlameRenderer.
 * State/act logic lives in fireplace-core.js.
 */
(function () {
  "use strict";
  const { CardState, PRESETS, SWATCHES, MEDIA, hexToRgb, rgbToHex, rendererModel, statusText } = window.FireplaceCore;

  const root = document.getElementById("fp");
  if (!root) return;
  const STATE_URL = root.dataset.stateUrl;
  const ACT_URL = root.dataset.actUrl;
  const ASSETS = root.dataset.assets.replace(/\/$/, "");
  const CSRF = root.dataset.csrf;
  const DEBUG = /[?&]fpdebug=1\b/.test(location.search);
  const POLL_MS = 5000;

  const el = (id) => document.getElementById(id);
  const tabsEl = el("fp-tabs");
  const cardEl = el("fp-card");
  const emptyEl = el("fp-empty");
  const bannerEl = el("fp-banner");
  const toastEl = el("fp-toast");

  let data = null;
  let current = localStorage.getItem("fireplace.current") || null;
  const cards = {}; // dsn -> CardState
  let ui = null; // the built card for `current`
  let pollTimer = null;
  let toastTimer = null;

  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("fp-show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("fp-show"), 5000);
  }

  async function post(id, action, value) {
    const r = await fetch(ACT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify({ id, action, value }),
    });
    let body = {};
    try { body = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error(body.error || `Fireplace didn't respond (${r.status})`);
    return body.fireplace;
  }

  function cardFor(dsn) {
    if (!cards[dsn]) {
      cards[dsn] = new CardState((action, value) => post(dsn, action, value), {
        onChange: () => { if (current === dsn) render(); },
        onError: toast,
      });
    }
    return cards[dsn];
  }

  const mediaKey = (dsn) => `fireplace.media.${dsn}`;
  const mediaFor = (dsn) => (MEDIA.includes(localStorage.getItem(mediaKey(dsn))) ? localStorage.getItem(mediaKey(dsn)) : "logs");

  // ── card DOM (the card's _build) ──
  const ICON = (cls) => `<i class="fas ${cls}"></i>`;
  const tog = (flag) => `<label class="fp-tog"><input type="checkbox" data-flag="${flag}"><span></span></label>`;
  const row = (label, inner) => `<div class="fp-row"><span class="fp-rl">${label}</span>${inner}</div>`;
  const slider = (key, min, max) =>
    `<input type="range" class="fp-range" min="${min}" max="${max}" step="1" data-set="${key}"><span class="fp-rv"></span>`;
  const swatches = (key) =>
    `<div class="fp-sw" data-sw="${key}">${SWATCHES.map((h) =>
      `<button type="button" data-hex="${h}" style="background:${h}" title="${h}"></button>`).join("")}` +
    `<input type="color" title="Custom colour"></div>`;

  function build(dsn) {
    destroyUi();
    cardEl.innerHTML = `
      <div class="fp-head">
        <div class="fp-ic">${ICON("fa-fire")}</div>
        <div class="fp-tx"><div class="fp-nm"></div><div class="fp-st"></div></div>
        <button type="button" class="fp-media" title="Firebox media"></button>
        <button type="button" class="fp-pwr" title="Power">${ICON("fa-power-off")}</button>
      </div>
      <div class="fp-box">
        <canvas></canvas>
        <div class="fp-glass"></div>
        <div class="fp-offmsg">${ICON("fa-fire")}<span></span></div>
        <button type="button" class="fp-puck" data-p="heat" style="left:9%;top:18%">${ICON("fa-temperature-half")}<span class="fp-lb">Heat</span></button>
        <button type="button" class="fp-puck" data-p="top" style="left:50%;top:16%">${ICON("fa-lightbulb")}<span class="fp-lb">Top light</span></button>
        <button type="button" class="fp-puck" data-p="flame" style="left:91%;top:18%">${ICON("fa-fire")}<span class="fp-lb">Flame</span></button>
        <button type="button" class="fp-puck" data-p="ember" style="left:91%;top:82%">${ICON("fa-braille")}<span class="fp-lb">Ember bed</span></button>
      </div>
      <div class="fp-panel" data-panel="heat">
        <h4>${ICON("fa-temperature-half")}Heat</h4>
        ${row("Heater", `<div class="fp-seg"><button type="button" data-heater="0">Off</button><button type="button" data-heater="1">Low</button><button type="button" data-heater="2">High</button></div>`)}
        ${row("Target", `<div class="fp-step"><button type="button" data-temp="-1">−</button><span class="fp-tv"></span><button type="button" data-temp="1">+</button></div>`)}
        <div class="fp-pair">
          ${row("Eco", `<span class="fp-sp"></span>${tog("eco")}`)}
          ${row("Boost", `<span class="fp-sp"></span>${tog("boost")}`)}
        </div>
      </div>
      <div class="fp-panel" data-panel="flame">
        <h4>${ICON("fa-fire")}Flame</h4>
        ${row("Height", slider("flame_speed", 1, 5))}
        ${row("Orange", slider("orange", 0, 4))}
        ${row("Yellow", slider("yellow", 0, 4))}
      </div>
      <div class="fp-panel" data-panel="ember">
        <h4>${ICON("fa-braille")}Ember bed</h4>
        ${row("Brightness", slider("ember_bri", 0, 4))}
        ${row("Colour", swatches("ember_rgb"))}
        ${row("Colour cycle", `<span class="fp-sp"></span>${tog("ember_cycle")}`)}
      </div>
      <div class="fp-panel" data-panel="top">
        <h4>${ICON("fa-lightbulb")}Top light</h4>
        ${row("On", `<span class="fp-sp"></span>${tog("top_on")}`)}
        ${row("Colour", swatches("top_rgb"))}
        ${row("Colour cycle", `<span class="fp-sp"></span>${tog("top_cycle")}`)}
      </div>
      <div class="fp-presets">
        ${PRESETS.map((p) => `<button type="button" data-fav="${p.option}">${ICON(p.icon)}${p.name}</button>`).join("")}
      </div>`;

    const $ = (q) => cardEl.querySelector(q);
    const $$ = (q) => cardEl.querySelectorAll(q);
    const card = cardFor(dsn);
    const act = (a, v) => card.act(a, v);
    ui = {
      dsn,
      name: $(".fp-nm"), status: $(".fp-st"), icon: $(".fp-ic"), pwr: $(".fp-pwr"), media: $(".fp-media"),
      box: $(".fp-box"), canvas: $("canvas"), offmsg: $(".fp-offmsg span"), tv: $(".fp-tv"),
      dragging: false, renderer: null, debug: null,
    };

    ui.pwr.addEventListener("click", () => act("power"));
    ui.media.addEventListener("click", () => {
      const next = mediaFor(dsn) === "logs" ? "crystals" : "logs";
      localStorage.setItem(mediaKey(dsn), next);
      if (ui.renderer) ui.renderer.setMedia(next);
      render();
    });
    $$(".fp-puck").forEach((b) => b.addEventListener("click", () => {
      card.panel = card.panel === b.dataset.p ? null : b.dataset.p;
      render();
    }));
    $$("[data-heater]").forEach((b) => b.addEventListener("click", () => act("heater", +b.dataset.heater)));
    $$("[data-temp]").forEach((b) => b.addEventListener("click", () => act("temp", +b.dataset.temp)));
    $$("[data-flag]").forEach((i) => i.addEventListener("change", () => act(i.dataset.flag, i.checked)));
    $$("[data-set]").forEach((i) => {
      i.addEventListener("pointerdown", () => (ui.dragging = true));
      const end = () => (ui.dragging = false);
      i.addEventListener("pointerup", end);
      i.addEventListener("pointercancel", end);
      i.addEventListener("change", end);
      i.addEventListener("input", () => act(i.dataset.set, +i.value));
    });
    $$("[data-sw]").forEach((wrap) => {
      const key = wrap.dataset.sw;
      wrap.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => act(key, hexToRgb(b.dataset.hex))));
      wrap.querySelector("input").addEventListener("input", (e) => act(key, hexToRgb(e.target.value)));
    });
    $$("[data-fav]").forEach((b) => b.addEventListener("click", () => act("favourite", b.dataset.fav)));

    try {
      ui.renderer = new window.FlameRenderer(ui.canvas, { assets: ASSETS, media: mediaFor(dsn), scale: 0.8, maxFps: 30 });
    } catch (e) {
      ui.box.insertAdjacentHTML("beforeend", `<div class="fp-nogl">Flames unavailable (${String(e.message || e)})</div>`);
    }
    if (DEBUG) startDebug();
  }

  function destroyUi() {
    if (!ui) return;
    if (ui.renderer) ui.renderer.destroy();
    if (ui.debug) clearInterval(ui.debug.timer);
    ui = null;
    cardEl.innerHTML = "";
  }

  function startDebug() {
    const box = document.createElement("pre");
    box.className = "fp-debug";
    ui.box.appendChild(box);
    let last = { t: performance.now(), frames: 0 };
    const timer = setInterval(() => {
      const r = ui && ui.renderer;
      if (!r) return;
      const now = performance.now();
      const fps = ((r.stats.frames - last.frames) * 1000) / (now - last.t);
      last = { t: now, frames: r.stats.frames };
      const s = Object.assign({}, r.stats, { live: undefined });
      box.textContent = `fps ${fps.toFixed(1)}  ${ui.canvas.width}x${ui.canvas.height}\n` +
        JSON.stringify(s) + "\n" + JSON.stringify(r.stats.live || {});
    }, 1000);
    ui.debug = { timer };
  }

  // ── the card's _render ──
  function render() {
    if (!ui) return;
    const card = cards[ui.dsn];
    const m = card.model();
    const fp = data.fireplaces.find((f) => f.id === ui.dsn);
    const media = mediaFor(ui.dsn);

    ui.name.textContent = fp ? fp.name : "";
    ui.status.innerHTML = statusText(m);
    ui.icon.classList.toggle("fp-on", !!m.power);
    ui.pwr.classList.toggle("fp-on", !!m.power);
    ui.pwr.disabled = !m.available;
    ui.media.innerHTML = media === "logs" ? `${ICON("fa-tree")}Logs` : `${ICON("fa-gem")}Crystals`;
    ui.box.classList.toggle("fp-on", !!m.power);
    ui.offmsg.textContent = m.available ? "Off — tap power or a preset" : "Unavailable";
    if (!m.power) card.panel = null;

    cardEl.querySelectorAll(".fp-puck").forEach((b) => b.classList.toggle("fp-sel", b.dataset.p === card.panel));
    cardEl.querySelectorAll(".fp-panel").forEach((p) => p.classList.toggle("fp-open", p.dataset.panel === card.panel));
    cardEl.querySelectorAll("[data-heater]").forEach((b) => b.classList.toggle("fp-act", +b.dataset.heater === m.heater));
    ui.tv.textContent = m.setpoint == null ? "–" : `${m.setpoint}°C`;
    cardEl.querySelectorAll("[data-flag]").forEach((i) => (i.checked = !!m[i.dataset.flag]));
    cardEl.querySelectorAll("[data-set]").forEach((i) => {
      if (!ui.dragging && m[i.dataset.set] != null) i.value = m[i.dataset.set];
      i.nextElementSibling.textContent = `${i.value}/${i.max}`;
    });
    cardEl.querySelectorAll("[data-sw]").forEach((w) => {
      const hex = m[w.dataset.sw] ? rgbToHex(m[w.dataset.sw]) : null;
      w.querySelectorAll("button").forEach((b) => b.classList.toggle("fp-act", hex === b.dataset.hex));
      const ci = w.querySelector("input");
      if (hex && document.activeElement !== ci) ci.value = hex;
    });
    cardEl.querySelectorAll("[data-fav]").forEach((b) => {
      b.classList.toggle("fp-act", !!m.power && m.favourite === b.dataset.fav);
      b.disabled = !m.available;
    });

    if (ui.renderer) {
      ui.renderer.setModel(rendererModel(m));
      if (!document.hidden) ui.renderer.start();
    }
  }

  function renderTabs() {
    const fps = data.fireplaces;
    tabsEl.hidden = fps.length < 2;
    tabsEl.innerHTML = "";
    fps.forEach((f) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "fp-tab";
      b.classList.toggle("fp-act", f.id === current);
      b.classList.toggle("fp-lit", !!cardFor(f.id).model().power);
      b.innerHTML = `${ICON("fa-fire")}<span></span>`;
      b.querySelector("span").textContent = f.name;
      b.addEventListener("click", () => select(f.id));
      tabsEl.appendChild(b);
    });
  }

  function select(dsn) {
    if (dsn === current && ui && ui.dsn === dsn) return;
    current = dsn;
    localStorage.setItem("fireplace.current", dsn);
    build(dsn);
    renderTabs();
    render();
  }

  function showEmpty(msg, icon) {
    destroyUi();
    tabsEl.hidden = true;
    cardEl.hidden = true;
    emptyEl.hidden = false;
    emptyEl.innerHTML = `${ICON(icon)}<p></p>`;
    emptyEl.querySelector("p").textContent = msg;
  }

  function apply(next) {
    data = next;
    bannerEl.hidden = !(data.error && data.fireplaces.length);
    bannerEl.textContent = data.error || "";
    if (!data.configured) {
      return showEmpty("Fireplace isn't set up yet — an admin needs to enter the Napoleon email and password in Settings.", "fa-gear");
    }
    if (!data.fireplaces.length) {
      return showEmpty(data.error || "No fireplaces on this Napoleon account.", data.error ? "fa-triangle-exclamation" : "fa-fire");
    }
    emptyEl.hidden = true;
    cardEl.hidden = false;
    data.fireplaces.forEach((f) => cardFor(f.id).setServer(Object.assign({ available: f.online }, f.model)));
    if (!data.fireplaces.some((f) => f.id === current)) current = data.fireplaces[0].id;
    if (!ui || ui.dsn !== current) build(current);
    renderTabs();
    render();
  }

  async function poll() {
    clearTimeout(pollTimer);
    try {
      const r = await fetch(STATE_URL, { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      apply(await r.json());
    } catch (e) {
      if (!data) showEmpty("Couldn't reach the fireplace service — retrying…", "fa-triangle-exclamation");
    }
    if (!document.hidden) pollTimer = setTimeout(poll, POLL_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(pollTimer);
      if (ui && ui.renderer) ui.renderer.stop();
    } else {
      poll();
    }
  });

  poll();
})();
