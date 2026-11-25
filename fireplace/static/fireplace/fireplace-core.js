/* Fireplace card logic, ported from hass-napoleon's napoleon-fireplace-card
 * (_model / _act / optimistic _pending / _debounced). No DOM here, so the node
 * tests can load it: node --test tests/js/*.test.js
 */
(function (root) {
  "use strict";

  const PENDING_MS = 12000;
  const EMBER_MAX = 4; // pynapoleon's ember_bed_brightness is 0..4
  const PRESETS = [
    { option: "partytime", name: "Party Time", icon: "fa-champagne-glasses" },
    { option: "campfirewarmth", name: "Campfire", icon: "fa-campground" },
    { option: "summerday", name: "Summer Day", icon: "fa-sun" },
    { option: "glowingsunset", name: "Sunset", icon: "fa-cloud-sun" },
  ];
  const SWATCHES = ["#ff5a1e", "#ff2a00", "#ffb020", "#ffffff", "#3a7bff", "#00c8ff", "#b040ff", "#30e070"];
  const MEDIA = ["logs", "crystals"];

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const hexToRgb = (hex) => {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
    return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [255, 90, 30];
  };
  const rgbToHex = (a) =>
    Array.isArray(a) && a.length === 3
      ? "#" + a.map((x) => clamp(Math.round(+x), 0, 255).toString(16).padStart(2, "0")).join("")
      : "#ff5a1e";
  const sameVal = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  // The renderer's ember brightness is on the card's 0..5 scale.
  function rendererModel(m) {
    return Object.assign({}, m, { ember_bri: ((m.ember_bri || 0) * 5) / (m.ember_max || EMBER_MAX) });
  }

  function statusText(m) {
    if (!m.available) return "Unavailable";
    if (!m.power) return "Off";
    const heat = m.heater ? `Heat <b>${m.heater === 2 ? "High" : "Low"}</b> · ${m.setpoint == null ? "–" : m.setpoint}°C` : "Heater off";
    return `<b>On</b> · Flame ${m.flame_speed}/5 · ${heat}`;
  }

  class CardState {
    /**
     * @param {(action: string, value: any) => Promise<any>} send  POSTs one action; resolves with
     *   the server's shaped fireplace ({model}) or rejects with an Error.
     * @param {{onChange?: function, onError?: function, now?: function, setTimeout?: function,
     *          clearTimeout?: function}} opts
     */
    constructor(send, opts) {
      this.send = send;
      this.opts = Object.assign({ onChange() {}, onError() {}, now: () => Date.now() }, opts || {});
      this._setTimeout = this.opts.setTimeout || ((f, ms) => setTimeout(f, ms));
      this._clearTimeout = this.opts.clearTimeout || ((t) => clearTimeout(t));
      this.server = null;
      this._pending = {};
      this._timers = {};
      this.panel = null;
    }

    setServer(model) {
      this.server = model;
    }

    model() {
      const m = Object.assign({}, this.server || { available: false, power: false });
      const now = this.opts.now();
      for (const [k, p] of Object.entries(this._pending)) {
        if (now > p.until || sameVal(m[k], p.value)) delete this._pending[k];
        else m[k] = p.value;
      }
      if (!m.power) m.heater = 0;
      return m;
    }

    _optimistic(key, value) {
      this._pending[key] = { value, until: this.opts.now() + PENDING_MS };
    }

    _debounced(key, fn, ms = 400) {
      this._clearTimeout(this._timers[key]);
      this._timers[key] = this._setTimeout(fn, ms);
    }

    _call(action, value) {
      let p;
      try {
        p = Promise.resolve(this.send(action, value));
      } catch (e) {
        p = Promise.reject(e);
      }
      return p
        .then((fp) => {
          if (fp && fp.model) this.setServer(fp.model);
          this.opts.onChange();
        })
        .catch((err) => {
          this._pending = {};
          this.opts.onChange();
          this.opts.onError(err && err.message ? err.message : String(err));
        });
    }

    act(action, value) {
      const m = this.model();
      switch (action) {
        case "power":
          this._optimistic("power", !m.power);
          if (m.power) this.panel = null;
          this._call("power", !m.power);
          break;
        case "heater":
          this._optimistic("heater", value);
          if (value && !m.power) this._optimistic("power", true);
          this._call("heater", value);
          break;
        case "temp": {
          const base = m.setpoint == null ? m.minT : m.setpoint;
          const next = clamp(base + value * m.stepT, m.minT, m.maxT);
          this._optimistic("setpoint", next);
          this._debounced("temp", () => this._call("temp", next), 700);
          break;
        }
        case "flame_speed":
        case "orange":
        case "yellow":
          this._optimistic(action, value);
          this._debounced(action, () => this._call(action, value));
          break;
        case "ember_bri":
          this._optimistic("ember_bri", value);
          this._optimistic("ember_on", value > 0);
          if (value > 0 && !m.power) this._optimistic("power", true);
          this._debounced("ember", () => this._call("ember_bri", value));
          break;
        case "ember_rgb":
          this._optimistic("ember_rgb", value);
          this._debounced("ember_rgb", () => this._call("ember_rgb", value));
          break;
        case "top_on":
          this._optimistic("top_on", value);
          if (value && !m.power) this._optimistic("power", true);
          this._call("top_on", value);
          break;
        case "top_rgb":
          this._optimistic("top_rgb", value);
          this._optimistic("top_on", true);
          this._debounced("top_rgb", () => this._call("top_rgb", value));
          break;
        case "eco":
        case "boost":
        case "ember_cycle":
        case "top_cycle":
          this._optimistic(action, value);
          this._call(action, value);
          break;
        case "favourite":
          // The server powers the fireplace on first if needed.
          this._optimistic("favourite", value);
          if (!m.power) this._optimistic("power", true);
          this._call("favourite", value);
          break;
        default:
          return;
      }
      this.opts.onChange();
    }
  }

  const api = { CardState, PENDING_MS, EMBER_MAX, PRESETS, SWATCHES, MEDIA, clamp, hexToRgb, rgbToHex, sameVal,
    rendererModel, statusText };
  root.FireplaceCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
