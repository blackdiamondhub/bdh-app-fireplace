/* Napoleon Astound fireplace renderer.
 *
 * Same look as the hass-napoleon Lovelace card shader, restructured so a weak
 * kiosk GPU (Adreno 506) can run it:
 *   - flame density (the expensive fireField() turbulence) is baked offline into
 *     looping grayscale videos, one pair (back/front flames) per flame height level
 *     (tools/bake). At runtime each video plays once and its frames are captured
 *     into an RGBA atlas texture (4 frames per texel); after that the flames are
 *     two texture fetches per layer.
 *   - everything that doesn't move (wall, logs, ember bed) is baked once on the
 *     device into half-float textures holding the coefficients of each light
 *     (fire, embers, top light, crack glow). The scene is linear in those lights,
 *     so colour/brightness/cycling settings stay fully live.
 *   - the per-frame shader only does texture fetches, flame colouring, sparks and
 *     tone mapping.
 */
(function (root) {
  "use strict";

  const BAKE_BASE = 0.13;
  const MAX_W = 1400;
  const SPARKS = 14;

  const VERT = "attribute vec2 aPos; void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }";

  // Verbatim from the card shader.
  const NOISE = `
float h21(vec2 p){ vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
float vnoise(vec2 p){
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(h21(i), h21(i + vec2(1.0, 0.0)), u.x),
             mix(h21(i + vec2(0.0, 1.0)), h21(i + vec2(1.0, 1.0)), u.x), u.y);
}
float fbm(vec2 p){
  float s = 0.0, a = 0.5;
  mat2 m = mat2(1.6, 1.2, -1.2, 1.6);
  for (int i = 0; i < 5; i++){ s += a * vnoise(p); p = m * p; a *= 0.5; }
  return s;
}`;

  // Static layers. Tracks the card's colour pipeline as a linear combination of
  // the lights; uPass selects which 4 coefficients to write.
  const BAKE_FRAG = `precision highp float;
uniform vec2 uRes; uniform float uMedia; uniform float uPass;
${NOISE}
float sdTaper(vec2 p, vec2 a, vec2 b, float ra, float rb, out float h){
  vec2 pa = p - a, ba = b - a;
  h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
  return length(pa - ba * h) - mix(ra, rb, h);
}
void main(){
  vec2 uv = gl_FragCoord.xy / uRes;
  float A = uRes.x / uRes.y;
  vec2 p = vec2(uv.x * A, uv.y);
  vec3 S1 = vec3(0.022, 0.019, 0.018) * (0.7 + 0.3 * fbm(p * vec2(6.0, 18.0)));
  vec3 S0 = S1;
  float baseY = uMedia < 0.5 ? 0.13 : 0.15;
  float F = 0.22 * exp(-max(p.y - baseY, 0.0) * 2.4) * exp(-pow((uv.x - 0.5) * 1.6, 2.0));
  float E = 0.12 * exp(-max(p.y - baseY, 0.0) * 6.0);
  float topFall = exp(-(1.0 - uv.y) * 7.5);
  float Tt = (topFall * 0.38 + 0.015) * (0.6 + 0.4 * sin(uv.x * 3.14159)) + smoothstep(0.975, 1.0, uv.y) * 0.9;
  float K = 1.0, Wc = 0.0, Wr = 0.0, X = 0.0, Be = 0.0, Bw = 0.0, id = 0.0;

  if (uMedia < 0.5){
    float px = 1.5 / uRes.y;
    for (int k = 0; k < 5; k++){
      vec2 la, lb; float ra, rb;
      if (k == 0)      { la = vec2(0.20 * A, 0.190); lb = vec2(0.80 * A, 0.215); ra = 0.060; rb = 0.054; }
      else if (k == 1) { la = vec2(0.82 * A, 0.080); lb = vec2(0.50 * A, 0.355); ra = 0.060; rb = 0.036; }
      else if (k == 2) { la = vec2(0.17 * A, 0.080); lb = vec2(0.52 * A, 0.365); ra = 0.064; rb = 0.038; }
      else if (k == 3) { la = vec2(0.40 * A, 0.065); lb = vec2(0.76 * A, 0.115); ra = 0.052; rb = 0.044; }
      else             { la = vec2(0.29 * A, 0.060); lb = vec2(0.38 * A, 0.135); ra = 0.058; rb = 0.050; }
      float hh;
      float d = sdTaper(p, la, lb, ra, rb, hh);
      float lr = mix(ra, rb, hh);
      float a = smoothstep(0.035, 0.0, d) * step(0.0, d);
      float sh1 = 1.0 - 0.55 * a - 0.25 * a;
      S1 *= sh1; S0 *= 1.0 - 0.25 * a; F *= sh1; E *= sh1; Tt *= sh1; K *= sh1; Wc *= sh1; Wr *= sh1;
      float mask = 1.0 - smoothstep(-px, px, d);
      if (mask <= 0.0) continue;
      float sd = float(k) * 7.31;
      vec2 axis = normalize(lb - la);
      vec2 nrm = vec2(-axis.y, axis.x);
      if (nrm.y < 0.0) nrm = -nrm;
      vec2 c = la + (lb - la) * hh;
      float v = clamp(dot(p - c, nrm) / lr, -1.0, 1.0);
      float along = dot(p - la, axis);
      float bark = fbm(vec2(along * 9.0 + sd, v * 6.0 + hh * 4.0)) * 0.6 + 0.4 * fbm(vec2(along * 40.0, v * 2.0 + sd));
      float tone = 0.8 + 0.4 * h21(vec2(sd, 1.0));
      vec3 wS = mix(vec3(0.05, 0.028, 0.015), vec3(0.17, 0.095, 0.05), bark) * tone;
      wS *= 0.45 + 0.55 * (v * 0.5 + 0.5);
      float wF = 0.22 * smoothstep(-0.2, 1.0, v) * (0.6 + 0.6 * bark);
      float wE = 0.25 * smoothstep(0.3, -1.0, v);
      float rn = fbm(vec2(along * 11.0 + sd, v * 1.8 + 3.0));
      float crack = pow(1.0 - abs(2.0 * rn - 1.0), 22.0) * smoothstep(0.45, 0.65, vnoise(vec2(along * 4.0 + sd, 7.0)));
      float wC = crack * 1.2 * smoothstep(-0.6, 0.6, v);
      float tr = dot(p - la, lb - la) / dot(lb - la, lb - la);
      float cap = max(smoothstep(0.0, -0.02, tr), smoothstep(1.0, 1.02, tr));
      vec2 ec = tr < 0.5 ? la : lb;
      float rr = length(p - ec) / lr;
      vec3 gS = mix(vec3(0.20, 0.12, 0.06), vec3(0.30, 0.19, 0.10), 0.5 + 0.5 * sin(rr * 26.0 + fbm(p * 30.0) * 3.0)) * tone;
      float rim = smoothstep(0.65, 0.95, rr) * 0.8;
      float gF = 0.12 * (1.0 - rim);
      gS *= 1.0 - rim;
      float cc = cap * 0.85;
      wS = mix(wS, gS, cc); wF = mix(wF, gF, cc); wE *= 1.0 - cc; wC *= 1.0 - cc;
      float wR = 0.7 * rim * cc;
      S1 = mix(S1, wS, mask); S0 = mix(S0, wS, mask); F = mix(F, wF, mask); E = mix(E, wE, mask);
      Tt *= 1.0 - mask; K *= 1.0 - mask; Wc = mix(Wc, wC, mask); Wr = mix(Wr, wR, mask);
      X = along * 6.0 + sd;
    }
  }

  float bedTop = baseY - 0.01 + 0.012 * vnoise(vec2(p.x * 9.0, 0.0));
  if (p.y < bedTop + 0.01){
    float bm = 1.0 - smoothstep(bedTop - 0.004, bedTop + 0.004, p.y);
    vec2 g = p * vec2(34.0, 40.0) * (uMedia < 0.5 ? 1.0 : 0.6);
    vec2 gi = floor(g), gf = fract(g);
    float md = 8.0, md2 = 8.0; float bid = 0.0;
    for (int j = -1; j <= 1; j++) for (int i = -1; i <= 1; i++){
      vec2 nb = vec2(float(i), float(j));
      vec2 pt = nb + vec2(h21(gi + nb), h21(gi + nb + 17.0)) - gf;
      float dd = dot(pt, pt);
      if (dd < md){ md2 = md; md = dd; bid = h21(gi + nb + 3.7); }
      else if (dd < md2){ md2 = dd; }
    }
    float cellEdge = sqrt(md2) - sqrt(md);
    vec3 bS; float bF, bE, bW = 0.0;
    if (uMedia < 0.5){
      float coal = smoothstep(0.0, 0.5, cellEdge) * (1.0 - smoothstep(0.25, 0.7, md));
      float hot = smoothstep(0.55, 1.0, bid);
      bS = vec3(0.028, 0.022, 0.02) * (0.4 + bid) * coal;
      bE = (1.0 - coal) * 0.55 + hot * coal * 0.9;
      bF = 0.12 * coal * bid;
    } else {
      float facet = smoothstep(0.0, 0.25, cellEdge);
      bS = vec3(0.05) * bid;
      bE = (0.5 + 0.9 * bid) * mix(1.4, 0.6, facet);
      bF = 0.12 * (0.4 + bid);
      bW = pow(bid, 16.0) * 0.6;
    }
    float vert = 0.55 + 0.45 * smoothstep(0.0, bedTop, p.y);
    bS *= vert; bE *= vert; bF *= vert; bW *= vert;
    S1 = mix(S1, bS, bm); S0 = mix(S0, bS, bm); F = mix(F, bF, bm);
    E *= 1.0 - bm; Tt *= 1.0 - bm; K *= 1.0 - bm; Wc *= 1.0 - bm; Wr *= 1.0 - bm;
    Be = bE * bm; Bw = bW * bm; id = bid;
  }

  if (uPass < 0.5) gl_FragColor = vec4(S1, K);
  else if (uPass < 1.5) gl_FragColor = vec4(S0, F);
  else if (uPass < 2.5) gl_FragColor = vec4(E, Tt, Be, id);
  else gl_FragColor = vec4(Wc, Wr, X, Bw);
}`;

  const MAIN_FRAG = `precision highp float;
uniform vec2 uRes;
uniform sampler2D uT0, uT1, uT2, uT3, uBack, uBack2, uFront, uFront2;
uniform vec4 uBackA, uBackB, uBackChA, uBackChB, uFrontA, uFrontB, uFrontChA, uFrontChB;
uniform float uBackMix, uFrontMix;
uniform vec4 uBackY, uFrontY;   // bake y0, y1, height scale, bake baseline
uniform float uBaseY, uOn, uBright, uYr, uH, uClock, uFlames;
uniform vec3 uFT, uEB, uTOP, uCrackC;
uniform float uG, uBW;
uniform vec3 uSpark[${SPARKS}];
${NOISE}
vec3 flameColor(float T, float yr){
  vec3 deep = vec3(0.45, 0.04, 0.0);
  vec3 org  = vec3(1.0, 0.33, 0.03);
  vec3 yel  = vec3(1.0, 0.70, 0.16);
  vec3 core = vec3(1.0, 0.92, 0.70);
  vec3 c = mix(deep, mix(org, yel, yr), smoothstep(0.05, 0.5, T));
  return mix(c, core, smoothstep(0.8, 1.25, T) * (0.5 + 0.5 * yr));
}
float dens(sampler2D sa, sampler2D sb, vec4 cA, vec4 chA, vec4 cB, vec4 chB, float m, float x, float y){
  if (y < 0.0 || y > 1.0) return 0.0;
  vec2 q = vec2(x, y);
  float a = dot(texture2D(sa, cA.xy + q * cA.zw), chA);
  float b = dot(texture2D(sb, cB.xy + q * cB.zw), chB);
  return mix(a, b, m) * 1.6;
}
void main(){
  vec2 uv = gl_FragCoord.xy / uRes;
  float A = uRes.x / uRes.y;
  vec2 p = vec2(uv.x * A, uv.y);
  vec4 t0 = texture2D(uT0, uv), t1 = texture2D(uT1, uv), t2 = texture2D(uT2, uv), t3 = texture2D(uT3, uv);
  vec3 col = mix(t1.rgb, t0.rgb, uOn);
  col += uFT * t1.a + uEB * t2.r + uTOP * t2.g;
  if (t3.r + t3.g > 0.0001){
    float n = 0.65 + 0.35 * vnoise(vec2(t3.b, uClock * 1.3));
    col += (uCrackC * t3.r + vec3(1.0, 0.33, 0.05) * t3.g) * uG * n;
  }
  if (t2.b + t3.a > 0.0001){
    float id = t2.a;
    float tw = 0.6 + 0.4 * sin(uClock * (1.0 + 2.0 * id) + id * 40.0);
    col += (uEB * t2.b + vec3(uBW * t3.a)) * tw;
  }
  if (uFlames > 0.5){
    float fy = p.y - uBaseY;
    float yb = (uBackY.w + fy * uBackY.z - uBackY.x) / (uBackY.y - uBackY.x);
    float T = dens(uBack, uBack2, uBackA, uBackChA, uBackB, uBackChB, uBackMix, uv.x, yb);
    float rise1 = clamp(fy / max(uH, 0.05), 0.0, 1.2);
    col += flameColor(T * (1.15 - 0.4 * rise1), uYr) * smoothstep(0.03, 0.4, T) * (1.0 - 0.35 * rise1) * uBright * 1.35 * t0.a;
    float yf = (uFrontY.w - 0.035 + (fy + 0.035) * uFrontY.z - uFrontY.x) / (uFrontY.y - uFrontY.x);
    float T2 = dens(uFront, uFront2, uFrontA, uFrontChA, uFrontB, uFrontChB, uFrontMix, uv.x, yf);
    col += flameColor(T2 * 0.85, uYr) * smoothstep(0.25, 0.9, T2) * uBright * 0.6;
    for (int i = 0; i < ${SPARKS}; i++){
      vec3 s = uSpark[i];
      if (s.z > 0.0) col += vec3(1.0, 0.55, 0.18) * s.z * smoothstep(0.0045, 0.0, length(p - s.xy));
    }
  }
  col *= 0.72 + 0.28 * smoothstep(0.0, 0.12, uv.y) * smoothstep(0.0, 0.08, uv.x) * smoothstep(0.0, 0.08, 1.0 - uv.x);
  col = 1.0 - exp(-col * 1.7);
  gl_FragColor = vec4(col, 1.0);
}`;

  const COPY_FRAG = `precision mediump float; uniform sampler2D uSrc; uniform vec4 uDst;
void main(){ gl_FragColor = vec4(texture2D(uSrc, (gl_FragCoord.xy - uDst.xy) / uDst.zw).r); }`;

  // JS copies of the shader helpers (float32 rounding, so sparks match the GPU closely).
  const f32 = Math.fround;
  const fract = (x) => x - Math.floor(x);
  function h21(x, y) {
    let a = fract(f32(x * 0.1031)), b = fract(f32(y * 0.1031)), c = a;
    const d = f32(a * f32(b + 33.33) + f32(b * f32(c + 33.33)) + f32(c * f32(a + 33.33)));
    a = f32(a + d); b = f32(b + d); c = f32(c + d);
    return fract(f32(f32(a + b) * c));
  }
  function vnoise(x, y) {
    const ix = Math.floor(x), iy = Math.floor(y), fx = x - ix, fy = y - iy;
    const ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
    const a = h21(ix, iy), b = h21(ix + 1, iy), c = h21(ix, iy + 1), d = h21(ix + 1, iy + 1);
    return (a + (b - a) * ux) * (1 - uy) + (c + (d - c) * ux) * uy;
  }
  function hsv2rgb(h, s, v) {
    return [0, 2 / 3, 1 / 3].map((o) => {
      const p = Math.min(1, Math.max(0, Math.abs(fract(h + 1 + o) * 6 - 3) - 1));
      return v * (1 + (p - 1) * s);
    });
  }
  const mix3 = (a, b, t) => a.map((x, i) => x + (b[i] - x) * t);

  const DEFAULT_TARGETS = {
    on: 0, height: 0.62, speed: 0.93, orange: 0.5, yellow: 0.5,
    ember: [1, 0.35, 0.12], emberBri: 0, emberCycle: 0, top: [0, 0, 0], topCycle: 0,
  };

  // Card model -> renderer targets (same mapping as the card's _targets()).
  function targetsFromModel(m) {
    m = m || {};
    const cl = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const lin = (rgb) => (rgb ? rgb.map((x) => x / 255) : [0, 0, 0]);
    const fs = cl(m.flame_speed || 3, 1, 5) - 1;
    return {
      on: m.power ? 1 : 0,
      height: 0.42 + 0.1 * fs,
      speed: 0.65 + 0.14 * fs,
      orange: cl(m.orange || 0, 0, 4) / 4,
      yellow: cl(m.yellow || 0, 0, 4) / 4,
      ember: lin(m.ember_rgb || [255, 90, 30]),
      emberBri: m.ember_on ? cl(m.ember_bri || 0, 0, 5) / 5 : 0,
      emberCycle: m.ember_cycle && m.ember_on ? 1 : 0,
      top: m.top_on ? lin(m.top_rgb || [255, 255, 255]) : [0, 0, 0],
      topCycle: m.top_cycle && m.top_on ? 1 : 0,
    };
  }

  class FlameRenderer {
    /**
     * @param {HTMLCanvasElement} canvas
     * @param {{assets: string, media?: "logs"|"crystals", scale?: number, maxFps?: number,
     *          loadFrames?: function, preserve?: boolean}} opts  assets = URL of the baked flames folder.
     */
    constructor(canvas, opts) {
      this.cv = canvas;
      this.opts = Object.assign({ media: "logs", scale: 0.8, maxFps: 60 }, opts || {});
      this.T = Object.assign({}, DEFAULT_TARGETS);
      this.stats = { frames: 0, bakeMs: 0, level: 0, live: {} };
      const gl = canvas.getContext("webgl", { antialias: false, alpha: false, premultipliedAlpha: false,
        powerPreference: "low-power", preserveDrawingBuffer: !!this.opts.preserve });
      if (!gl) throw new Error("WebGL unavailable");
      this.gl = gl;
      const hp = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT);
      if (!hp || !hp.precision) throw new Error("highp unsupported");
      this._initFloat();
      this.progMain = this._prog(MAIN_FRAG);
      this.progBake = this._prog(BAKE_FRAG);
      this.progCopy = this._prog(COPY_FRAG);
      this.buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      this.fields = { back: this._newField(), front: this._newField() };
      this.statics = null;
      this.level = 0;
      this._raf = null;
      this._metaCache = {};
    }

    _initFloat() {
      const gl = this.gl;
      const hf = gl.getExtension("OES_texture_half_float");
      gl.getExtension("OES_texture_half_float_linear");
      gl.getExtension("EXT_color_buffer_half_float");
      const fl = gl.getExtension("OES_texture_float");
      gl.getExtension("OES_texture_float_linear");
      gl.getExtension("WEBGL_color_buffer_float");
      const cands = [];
      if (hf) cands.push(hf.HALF_FLOAT_OES);
      if (fl) cands.push(gl.FLOAT);
      for (const type of cands) {
        const t = this._tex(4, 4, type);
        const fb = gl.createFramebuffer();
        gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
        const ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.deleteFramebuffer(fb); gl.deleteTexture(t);
        if (ok) { this.floatType = type; return; }
      }
      throw new Error("no renderable float textures");
    }

    _prog(frag) {
      const gl = this.gl;
      const sh = (type, src) => {
        const s = gl.createShader(type);
        gl.shaderSource(s, src); gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
        return s;
      };
      const p = gl.createProgram();
      gl.attachShader(p, sh(gl.VERTEX_SHADER, VERT));
      gl.attachShader(p, sh(gl.FRAGMENT_SHADER, frag));
      gl.bindAttribLocation(p, 0, "aPos");
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      const u = {};
      const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
      for (let i = 0; i < n; i++) {
        const name = gl.getActiveUniform(p, i).name.replace(/\[0\]$/, "");
        u[name] = gl.getUniformLocation(p, name);
      }
      return { p, u };
    }

    _tex(w, h, type, data) {
      const gl = this.gl;
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, type || gl.UNSIGNED_BYTE, data || null);
      return t;
    }

    _quad(prog) {
      const gl = this.gl;
      gl.useProgram(prog.p);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    }

    _newField() {
      return { meta: null, pages: [], cols: 0, perPage: 0, tw: 0, th: 0, got: null, count: 0,
        complete: false, live: null, pos: 0, video: null, gen: 0 };
    }

    // ---------------------------------------------------------------- targets
    setTargets(t) { Object.assign(this.T, t); this._ensureLevel(); this.kick(); }
    setModel(m) { this.setTargets(targetsFromModel(m)); }
    setMedia(media) {
      if (media === this.opts.media) return;
      this.opts.media = media;
      this.statics = null;
      this.kick();
    }

    _levelFor(height) { return Math.max(1, Math.min(5, Math.round((height - 0.42) / 0.1) + 1)); }

    _ensureLevel() {
      const lv = this._levelFor(this.T.height);
      if (lv === this.level) return;
      this.level = lv;
      this.stats.level = lv;
      for (const name of ["back", "front"]) this._loadFieldRetry(name, lv, 0);
    }

    _loadFieldRetry(name, lv, attempt) {
      this._loadField(name, lv).catch((e) => {
        this.stats.error = `${name}: ${e.message || e}`;
        delete this._metaCache[`${name}_${lv}`];
        const delay = Math.min(30000, 1000 * 2 ** attempt);
        setTimeout(() => { if (this.level === lv) this._loadFieldRetry(name, lv, attempt + 1); }, delay);
      });
    }

    _meta(name, lv) {
      const key = `${name}_${lv}`;
      if (!this._metaCache[key]) {
        this._metaCache[key] = fetch(`${this.opts.assets}/${key}.json`).then((r) => {
          if (!r.ok) throw new Error(`${key}.json: ${r.status}`);
          return r.json();
        });
      }
      return this._metaCache[key];
    }

    // Atlas = one or more pages of cells; each cell holds 4 frames (one per channel).
    _allocAtlas(f, meta) {
      const gl = this.gl;
      const max = Math.min(4096, gl.getParameter(gl.MAX_TEXTURE_SIZE));
      const cells = Math.ceil(meta.frames / 4);
      const cols = Math.max(1, Math.floor(max / meta.w));
      const rowsMax = Math.max(1, Math.floor(max / meta.h));
      const perPage = Math.min(cells, cols * rowsMax);
      const npages = Math.ceil(cells / perPage);
      const tw = Math.min(cols, perPage) * meta.w, th = Math.ceil(perPage / cols) * meta.h;
      if (f.pages.length !== npages || f.tw !== tw || f.th !== th) {
        for (const pg of f.pages) { gl.deleteTexture(pg.tex); gl.deleteFramebuffer(pg.fb); }
        f.pages = [];
        for (let i = 0; i < npages; i++) {
          const tex = this._tex(tw, th), fb = gl.createFramebuffer();
          gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
          gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
          f.pages.push({ tex, fb });
        }
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        f.tw = tw; f.th = th;
      }
      f.cols = cols; f.perPage = perPage;
    }

    _cellOf(f, i) {
      const ci = i >> 2, page = Math.floor(ci / f.perPage), local = ci % f.perPage;
      return { page, x: (local % f.cols) * f.meta.w, y: Math.floor(local / f.cols) * f.meta.h, ch: i & 3 };
    }

    _dropVideo(f) {
      if (!f.video) return;
      f.video.pause(); f.video.removeAttribute("src"); f.video.load();
      f.video = null;
    }

    async _loadField(name, lv) {
      const f = this.fields[name];
      const gen = ++f.gen;
      this._dropVideo(f);
      const meta = await this._meta(name, lv);
      if (gen !== f.gen) return;
      if (this.opts.loadFrames) {
        this._resetField(f, meta);
        // Test hook: frames supplied directly (Uint8Array luminance per frame, rows bottom-up).
        const frames = await this.opts.loadFrames(name, lv, meta);
        if (gen !== f.gen) return;
        const gl = this.gl;
        const tmp = this._tex(meta.w, meta.h);
        frames.forEach((px, i) => {
          const rgba = new Uint8Array(meta.w * meta.h * 4);
          for (let k = 0; k < px.length; k++) rgba[k * 4] = px[k];
          gl.bindTexture(gl.TEXTURE_2D, tmp);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, meta.w, meta.h, 0, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
          this._capture(f, tmp, i);
        });
        gl.deleteTexture(tmp);
        f.complete = true;
        this.kick();
        return;
      }
      this._playCapture(name, f, meta, lv, gen);
    }

    _resetField(f, meta) {
      f.meta = meta; f.got = new Uint8Array(meta.frames); f.count = 0; f.complete = false; f.hasLive = false;
      this._allocAtlas(f, meta);
    }

    /** True once both flame layers for the current level are fully in their atlases. */
    ready() {
      const want = 0.42 + 0.1 * (this.level - 1);
      return Object.values(this.fields).every((f) => f.complete && f.meta && Math.abs(f.meta.height - want) < 1e-6);
    }

    _playCapture(name, f, meta, lv, gen) {
      const gl = this.gl;
      const v = document.createElement("video");
      v.muted = true; v.playsInline = true; v.loop = true; v.preload = "auto"; v.crossOrigin = "anonymous";
      v.setAttribute("muted", ""); v.setAttribute("playsinline", "");
      v.src = `${this.opts.assets}/${name}_${lv}.mp4`;
      f.video = v;
      if (!f.live || f.liveW !== meta.w || f.liveH !== meta.h) {
        if (f.live) gl.deleteTexture(f.live);
        f.live = this._tex(meta.w, meta.h); f.liveW = meta.w; f.liveH = meta.h;
      }
      // The previous level keeps drawing from its atlas until the first new frame arrives.
      let first = true;
      const onFrame = (_now, md) => {
        if (gen !== f.gen) return;
        if (first) { this._resetField(f, meta); first = false; }
        const i = Math.round(md.mediaTime * meta.fps) % meta.frames;
        gl.bindTexture(gl.TEXTURE_2D, f.live);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, v);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        f.hasLive = true;
        if (!f.got[i]) this._capture(f, f.live, i);
        if (f.count >= meta.frames) {
          f.complete = true;
          f.pos = i;
          if (f.video === v) this._dropVideo(f);
          return;
        }
        v.requestVideoFrameCallback(onFrame);
      };
      v.requestVideoFrameCallback(onFrame);
      v.addEventListener("error", () => {
        if (gen !== f.gen) return;
        this.stats.error = `${name} video error ${v.error && v.error.code}`;
        this._dropVideo(f);
        setTimeout(() => { if (gen === f.gen) this._loadFieldRetry(name, lv, 2); }, 3000);
      });
      v.play().catch((e) => { this.stats.error = "video: " + e; });
    }

    _capture(f, src, i) {
      const gl = this.gl, m = f.meta;
      const c = this._cellOf(f, i), cx = c.x, cy = c.y, ch = c.ch;
      gl.bindFramebuffer(gl.FRAMEBUFFER, f.pages[c.page].fb);
      gl.viewport(cx, cy, m.w, m.h);
      gl.disable(gl.SCISSOR_TEST);
      gl.colorMask(ch === 0, ch === 1, ch === 2, ch === 3);
      const P = this.progCopy;
      this._quad(P);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, src);
      gl.uniform1i(P.u.uSrc, 0);
      gl.uniform4f(P.u.uDst, cx, cy, m.w, m.h);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.colorMask(true, true, true, true);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      if (!f.got[i]) { f.got[i] = 1; f.count++; }
    }

    // ---------------------------------------------------------------- statics
    _bakeStatics(w, h) {
      const gl = this.gl;
      if (this.statics) this.statics.tex.forEach((t) => gl.deleteTexture(t));
      const tex = [0, 1, 2, 3].map(() => this._tex(w, h, this.floatType));
      const fb = gl.createFramebuffer();
      const t0 = performance.now();
      const P = this.progBake;
      // Tiles keep each draw short so a slow GPU doesn't trip the watchdog.
      const TW = 512, TH = 256;
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      this._quad(P);
      gl.uniform2f(P.u.uRes, w, h);
      gl.uniform1f(P.u.uMedia, this.opts.media === "crystals" ? 1 : 0);
      gl.viewport(0, 0, w, h);
      gl.enable(gl.SCISSOR_TEST);
      for (let pass = 0; pass < 4; pass++) {
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex[pass], 0);
        gl.uniform1f(P.u.uPass, pass);
        for (let y = 0; y < h; y += TH) for (let x = 0; x < w; x += TW) {
          gl.scissor(x, y, TW, TH);
          gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
          gl.flush();
        }
      }
      gl.disable(gl.SCISSOR_TEST);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.deleteFramebuffer(fb);
      this.statics = { tex, w, h, media: this.opts.media };
      this.stats.bakeMs = Math.round(performance.now() - t0);
    }

    // ---------------------------------------------------------------- loop
    start() { this._running = true; this._ensureLevel(); this.kick(); }
    stop() { this._running = false; if (this._raf) cancelAnimationFrame(this._raf); this._raf = null; }
    kick() {
      if (!this._running || this._raf || document.hidden) return;
      this._last = performance.now() - 1000 / this.opts.maxFps;
      this._raf = requestAnimationFrame((t) => this._frame(t));
    }
    destroy() {
      this.stop();
      for (const f of Object.values(this.fields)) { f.gen++; this._dropVideo(f); }
      const ext = this.gl.getExtension("WEBGL_lose_context");
      if (ext) ext.loseContext();
    }

    _fieldUniforms(P, name, unit, dt) {
      const gl = this.gl, f = this.fields[name], c = this._cur;
      const pre = name === "back" ? "uBack" : "uFront";
      const U = (s) => P.u[pre + s];
      const m = f.meta;
      const bind = (u, tex) => { gl.activeTexture(gl.TEXTURE0 + u); gl.bindTexture(gl.TEXTURE_2D, tex); };
      gl.uniform1i(U(""), unit); gl.uniform1i(U("2"), unit + 1);
      if (!m || (!f.complete && !f.hasLive)) {
        bind(unit, null); bind(unit + 1, null);
        return false;
      }
      const Hc = Math.max(0.01, c.height * c.on);
      gl.uniform4f(U("Y"), m.y0, m.y1, m.height / Hc, m.base);
      if (!f.complete) {
        bind(unit, f.live); bind(unit + 1, f.live);
        gl.uniform4f(U("A"), 0, 0, 1, 1); gl.uniform4f(U("B"), 0, 0, 1, 1);
        gl.uniform4f(U("ChA"), 1, 0, 0, 0); gl.uniform4f(U("ChB"), 1, 0, 0, 0);
        gl.uniform1f(U("Mix"), 0);
        this.stats.live[name] = `${f.count}/${m.frames}`;
        return true;
      }
      delete this.stats.live[name];
      f.pos = (f.pos + dt * m.fps * (c.speed / m.speed)) % m.frames;
      const k = Math.floor(f.pos), k1 = (k + 1) % m.frames;
      const ca = this._cellOf(f, k), cb = this._cellOf(f, k1);
      const rect = (q) => [q.x / f.tw, q.y / f.th, m.w / f.tw, m.h / f.th];
      const chan = (q) => [0, 1, 2, 3].map((j) => (j === q.ch ? 1 : 0));
      bind(unit, f.pages[ca.page].tex); bind(unit + 1, f.pages[cb.page].tex);
      gl.uniform4fv(U("A"), rect(ca)); gl.uniform4fv(U("B"), rect(cb));
      gl.uniform4fv(U("ChA"), chan(ca)); gl.uniform4fv(U("ChB"), chan(cb));
      gl.uniform1f(U("Mix"), f.pos - k);
      return true;
    }

    /** Advance by dt seconds and draw one frame. Exposed for tests (fixed dt, explicit size). */
    step(dt, size) {
      const gl = this.gl, T = this.T;
      if (!this._cur) this._cur = Object.assign({}, T, { ember: [...T.ember], top: [...T.top],
        time: Math.random() * 100, clock: 0 });
      const c = this._cur;
      const k = 1 - Math.exp(-dt * 2.2);
      const kOn = 1 - Math.exp(-dt * (T.on > c.on ? 1.1 : 2.0));
      for (const key of ["height", "speed", "orange", "yellow", "emberBri", "emberCycle", "topCycle"]) c[key] += (T[key] - c[key]) * k;
      for (let i = 0; i < 3; i++) { c.ember[i] += (T.ember[i] - c.ember[i]) * k; c.top[i] += (T.top[i] - c.top[i]) * k; }
      c.on += (T.on - c.on) * kOn;
      if (Math.abs(T.on - c.on) < 0.002) c.on = T.on;
      c.time = (c.time + dt * c.speed) % 3600;
      c.clock = (c.clock + dt) % 3600;

      let w, h;
      if (size) { [w, h] = size; } else {
        const cv = this.cv, s = Math.min(window.devicePixelRatio || 1, 1.5) * this.opts.scale;
        w = Math.max(32, Math.min(MAX_W, Math.round(cv.clientWidth * s)));
        h = Math.max(16, Math.round(w * cv.clientHeight / Math.max(1, cv.clientWidth)));
      }
      if (this.cv.width !== w || this.cv.height !== h) { this.cv.width = w; this.cv.height = h; }
      if (!this.statics || this.statics.w !== w || this.statics.h !== h || this.statics.media !== this.opts.media) {
        this._bakeStatics(w, h);
      }

      const fl = Math.max(c.orange, c.yellow);
      const bright = fl > 0.001 ? 0.45 + 0.55 * fl : 0;
      const yr = c.yellow / Math.max(c.orange + c.yellow, 0.001);
      const H = c.height * c.on * (bright > 0 ? 1 : 0);
      const flick = 0.82 + 0.18 * vnoise(c.clock * 3.1, 0);
      const fireLight = bright * c.on * (0.4 + c.height) * flick;
      const fireTint = mix3([1, 0.38, 0.08], [1, 0.62, 0.22], yr);
      const ember = mix3(c.ember, hsv2rgb(fract(c.clock * 0.045), 0.95, 1), c.emberCycle);
      const top = mix3(c.top, hsv2rgb(fract(c.clock * 0.045 + 0.5), 0.95, 1), c.topCycle);
      const emberB = c.emberBri * c.on;
      const A = w / h;
      const baseY = this.opts.media === "crystals" ? 0.15 : 0.13;

      const P = this.progMain;
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, w, h);
      gl.disable(gl.SCISSOR_TEST);
      gl.colorMask(true, true, true, true);
      this._quad(P);
      const u = P.u;
      this.statics.tex.forEach((t, i) => {
        gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, t); gl.uniform1i(u[`uT${i}`], i);
      });
      const okB = this._fieldUniforms(P, "back", 4, dt);
      const okF = this._fieldUniforms(P, "front", 6, dt);
      gl.uniform2f(u.uRes, w, h);
      gl.uniform1f(u.uBaseY, baseY);
      gl.uniform1f(u.uOn, c.on);
      gl.uniform1f(u.uBright, bright);
      gl.uniform1f(u.uYr, yr);
      gl.uniform1f(u.uH, H);
      gl.uniform1f(u.uClock, c.clock);
      gl.uniform1f(u.uFlames, H >= 0.01 && okB && okF ? 1 : 0);
      gl.uniform3fv(u.uFT, fireTint.map((x) => x * fireLight));
      gl.uniform3fv(u.uEB, ember.map((x) => x * emberB));
      gl.uniform3fv(u.uTOP, top.map((x) => x * c.on));
      gl.uniform3fv(u.uCrackC, mix3([1, 0.28, 0.04], ember, 0.35));
      gl.uniform1f(u.uG, c.on * (0.35 * bright + 0.65 * Math.max(bright, emberB)));
      gl.uniform1f(u.uBW, emberB + fireLight);
      const sp = new Float32Array(SPARKS * 3);
      if (H > 0.02) {
        for (let i = 0; i < SPARKS; i++) {
          const r1 = h21(i, 1.3);
          const life = fract(c.time * (0.16 + 0.12 * r1) + r1 * 7.0);
          sp[i * 3] = (0.12 + 0.76 * h21(i, 7.7)) * A + Math.sin(life * 6.0 + i) * 0.025;
          sp[i * 3 + 1] = baseY + 0.05 + life * H * 1.35;
          sp[i * 3 + 2] = (1 - life) * (h21(i, Math.floor(c.time * 0.2 + r1 * 5.0)) >= 0.55 ? 1 : 0) * bright * 1.5;
        }
      }
      gl.uniform3fv(u.uSpark, sp);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      this.stats.frames++;
      return { settled: T.on === 0 && c.on === 0 };
    }

    _frame(now) {
      this._raf = null;
      if (!this._running || document.hidden) return;
      const elapsed = (now - this._last) / 1000;
      if (elapsed < 1 / this.opts.maxFps - 0.002) { this._raf = requestAnimationFrame((t) => this._frame(t)); return; }
      this._last = now;
      const r = this.step(Math.min(0.1, elapsed));
      const capturing = Object.values(this.fields).some((f) => f.meta && !f.complete);
      if (r.settled && !capturing) return;
      this._raf = requestAnimationFrame((t) => this._frame(t));
    }
  }

  FlameRenderer.targetsFromModel = targetsFromModel;
  root.FlameRenderer = FlameRenderer;
  if (typeof module !== "undefined" && module.exports) module.exports = { FlameRenderer, targetsFromModel, h21, vnoise };
})(typeof window !== "undefined" ? window : globalThis);
