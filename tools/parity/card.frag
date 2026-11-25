
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
uniform vec2  uRes;
uniform float uTime;      // flame time (scaled by flame speed)
uniform float uClock;     // wall-clock seconds (light cycling, twinkle)
uniform float uOn;        // 0..1 power fade
uniform float uHeight;    // flame height, fraction of firebox
uniform float uOrange;    // 0..1
uniform float uYellow;    // 0..1
uniform vec3  uEmber;     // ember bed colour 0..1
uniform float uEmberBri;  // 0..1
uniform float uEmberCycle;
uniform vec3  uTop;
uniform float uTopCycle;
uniform float uMedia;     // 0 logs, 1 crystals

// small-magnitude hash: stays stable when GPUs fuse/reorder math on large lattice coords
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
}
vec3 hsv2rgb(vec3 c){
  vec3 p = abs(fract(c.xxx + vec3(1.0, 2.0/3.0, 1.0/3.0)) * 6.0 - 3.0);
  return c.z * mix(vec3(1.0), clamp(p - 1.0, 0.0, 1.0), c.y);
}
float sdCap(vec2 p, vec2 a, vec2 b, float r, out float h){
  vec2 pa = p - a, ba = b - a;
  h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
  return length(pa - ba * h) - r;
}

float sdTaper(vec2 p, vec2 a, vec2 b, float ra, float rb, out float h){
  vec2 pa = p - a, ba = b - a;
  h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
  return length(pa - ba * h) - mix(ra, rb, h);
}
// One lattice of flame tongues: each cell holds a tongue that tapers to a tip.
float tongues(float x, float y, float t, float H, float seed){
  float xc = x * 4.2 + seed;
  float c0 = floor(xc);
  float best = -10.0;
  // tongues are jittered and can be wider than a cell, so check neighbours too
  for (int j = -1; j <= 1; j++){
    float ci = c0 + float(j);
    float u = xc - ci - 0.5 - (h21(vec2(ci, seed)) - 0.5) * 0.45;
    float wd = 0.65 + 0.7 * h21(vec2(ci + 3.1, seed));
    float hc = H * (0.3 + 0.7 * vnoise(vec2(ci * 1.7 + seed, t * 0.7)));
    float ny = y / hc;
    best = max(best, (1.0 - ny) - abs(u) * 2.0 / wd * (0.2 + 1.1 * ny));
  }
  return best;
}

// Turbulent flame density at p (x in aspect units, y = height above the burner).
float fireField(vec2 p, float t, float H, float seed){
  float y = p.y;
  if (y < -0.04 || H < 0.01) return 0.0;
  // tongues lean and sway as they rise
  float rise = y / max(H, 0.05);
  float sway = (vnoise(vec2(y * 2.5 - t * 1.1, p.x * 1.3 + seed)) - 0.5) * 0.22 * rise;
  float x = p.x + sway;
  // upward-scrolling, domain-warped turbulence
  vec2 q = vec2(x * 3.0, y * 2.0 - t * 1.4);
  vec2 w = vec2(fbm(q + vec2(seed, 0.0)), fbm(q + vec2(5.2 + seed, 1.3)));
  float n = fbm(vec2(x * 6.0, y * 4.2 - t * 2.8) + (w - 0.5) * 2.4);
  float xs = x + (w.x - 0.5) * 0.12;
  float shape = max(tongues(xs, y, t, H, seed), tongues(xs, y, t * 1.13, H * 0.85, seed + 0.5));
  float f = shape * 1.2 + (n - 0.5) * (0.45 + 1.35 * rise);
  f *= smoothstep(-0.04, 0.04, y);
  return clamp(f, -1.0, 1.6);
}
vec3 flameColor(float T, float yr){
  vec3 deep = vec3(0.45, 0.04, 0.0);
  vec3 org  = vec3(1.0, 0.33, 0.03);
  vec3 yel  = vec3(1.0, 0.70, 0.16);
  vec3 core = vec3(1.0, 0.92, 0.70);
  vec3 c = mix(deep, mix(org, yel, yr), smoothstep(0.05, 0.5, T));
  return mix(c, core, smoothstep(0.8, 1.25, T) * (0.5 + 0.5 * yr));
}

void main(){
  vec2 uv = gl_FragCoord.xy / uRes;
  float A = uRes.x / uRes.y;
  vec2 p = vec2(uv.x * A, uv.y);
  float on = uOn;

  float o = uOrange, yv = uYellow;
  float fl = max(o, yv);
  float bright = fl > 0.001 ? (0.45 + 0.55 * fl) : 0.0;
  float yr = yv / max(o + yv, 0.001);
  float H = uHeight * on * (bright > 0.0 ? 1.0 : 0.0);
  float t = uTime;
  float flick = 0.82 + 0.18 * vnoise(vec2(uClock * 3.1, 0.0));
  float fireLight = bright * on * (0.4 + uHeight) * flick;
  vec3 fireTint = mix(vec3(1.0, 0.38, 0.08), vec3(1.0, 0.62, 0.22), yr);

  vec3 ember = mix(uEmber, hsv2rgb(vec3(fract(uClock * 0.045), 0.95, 1.0)), uEmberCycle);
  vec3 top   = mix(uTop,   hsv2rgb(vec3(fract(uClock * 0.045 + 0.5), 0.95, 1.0)), uTopCycle);
  float emberB = uEmberBri * on;

  // back wall: dark reflective panel lit by fire, embers and top light
  vec3 col = vec3(0.022, 0.019, 0.018) * (0.7 + 0.3 * fbm(p * vec2(6.0, 18.0)));
  float baseY = uMedia < 0.5 ? 0.13 : 0.15;
  float wallGlow = exp(-max(p.y - baseY, 0.0) * 2.4) * exp(-pow((uv.x - 0.5) * 1.6, 2.0));
  col += fireTint * fireLight * 0.22 * wallGlow;
  col += ember * emberB * 0.12 * exp(-max(p.y - baseY, 0.0) * 6.0);
  float topFall = exp(-(1.0 - uv.y) * 7.5);
  col += top * on * (topFall * 0.38 + 0.015) * (0.6 + 0.4 * sin(uv.x * 3.14159));
  col += top * on * smoothstep(0.975, 1.0, uv.y) * 0.9;

  // back flames
  vec2 fp = vec2(p.x, p.y - baseY);
  float edge = smoothstep(0.0, 0.07 * A, uv.x * A) * smoothstep(0.0, 0.07 * A, (1.0 - uv.x) * A);
  float Tr = fireField(fp, t, H, 0.0) * edge;
  float T = max(Tr, 0.0);
  float rise1 = clamp(fp.y / max(H, 0.05), 0.0, 1.2);
  col += flameColor(T * (1.15 - 0.4 * rise1), yr) * smoothstep(0.03, 0.4, T) * (1.0 - 0.35 * rise1) * bright * 1.35;

  // media: crossed log stack, painted back to front
  if (uMedia < 0.5){
    float px = 1.5 / uRes.y;
    for (int k = 0; k < 5; k++){
      vec2 la, lb; float ra, rb;
if (k == 0)      { la = vec2(0.20 * A, 0.190); lb = vec2(0.80 * A, 0.215); ra = 0.060; rb = 0.054; } // back log
      else if (k == 1) { la = vec2(0.82 * A, 0.080); lb = vec2(0.50 * A, 0.355); ra = 0.060; rb = 0.036; } // right lean
      else if (k == 2) { la = vec2(0.17 * A, 0.080); lb = vec2(0.52 * A, 0.365); ra = 0.064; rb = 0.038; } // left lean
      else if (k == 3) { la = vec2(0.40 * A, 0.065); lb = vec2(0.76 * A, 0.115); ra = 0.052; rb = 0.044; } // front cross log
      else             { la = vec2(0.29 * A, 0.060); lb = vec2(0.38 * A, 0.135); ra = 0.058; rb = 0.050; } // stub pointing at viewer
      float hh;
      float d = sdTaper(p, la, lb, ra, rb, hh);
      float lr = mix(ra, rb, hh);
      // contact shadow onto whatever is behind
      col *= 1.0 - 0.55 * smoothstep(0.035, 0.0, d) * on * step(0.0, d) - 0.25 * smoothstep(0.035, 0.0, d) * step(0.0, d);
      float mask = 1.0 - smoothstep(-px, px, d);
      if (mask <= 0.0) continue;
      float sd = float(k) * 7.31;
      vec2 axis = normalize(lb - la);
      vec2 nrm = vec2(-axis.y, axis.x);
      if (nrm.y < 0.0) nrm = -nrm;
      vec2 c = la + (lb - la) * hh;
      float v = clamp(dot(p - c, nrm) / lr, -1.0, 1.0);      // -1 bottom .. 1 top
      float along = dot(p - la, axis);
      float bark = fbm(vec2(along * 9.0 + sd, v * 6.0 + hh * 4.0)) * 0.6 + 0.4 * fbm(vec2(along * 40.0, v * 2.0 + sd));
      float tone = 0.8 + 0.4 * h21(vec2(sd, 1.0));
      vec3 wood = mix(vec3(0.05, 0.028, 0.015), vec3(0.17, 0.095, 0.05), bark) * tone;
      wood *= 0.45 + 0.55 * (v * 0.5 + 0.5);
      wood += fireTint * fireLight * 0.22 * smoothstep(-0.2, 1.0, v) * (0.6 + 0.6 * bark);
      wood += ember * emberB * 0.25 * smoothstep(0.3, -1.0, v);
      float rn = fbm(vec2(along * 11.0 + sd, v * 1.8 + 3.0));
      float crack = pow(1.0 - abs(2.0 * rn - 1.0), 22.0) * smoothstep(0.45, 0.65, vnoise(vec2(along * 4.0 + sd, 7.0)));
      float glowAmt = on * (0.35 * bright + 0.65 * max(bright, emberB)) * (0.65 + 0.35 * vnoise(vec2(along * 6.0 + sd, uClock * 1.3)));
      wood += mix(vec3(1.0, 0.28, 0.04), ember, 0.35) * crack * glowAmt * 1.2 * smoothstep(-0.6, 0.6, v);
      // cut ends: lighter end grain with rings, charred glowing rim
      float tr = dot(p - la, lb - la) / dot(lb - la, lb - la);
      float cap = max(smoothstep(0.0, -0.02, tr), smoothstep(1.0, 1.02, tr));
      vec2 ec = tr < 0.5 ? la : lb;
      float rr = length(p - ec) / lr;
      vec3 grain = mix(vec3(0.20, 0.12, 0.06), vec3(0.30, 0.19, 0.10), 0.5 + 0.5 * sin(rr * 26.0 + fbm(p * 30.0) * 3.0)) * tone;
      grain += fireTint * fireLight * 0.12;
      grain = mix(grain, vec3(1.0, 0.33, 0.05) * glowAmt * 0.7, smoothstep(0.65, 0.95, rr) * 0.8);
      wood = mix(wood, grain, cap * 0.85);
      col = mix(col, wood, mask);
    }
  }

  // ember / crystal bed
  float bedTop = baseY - 0.01 + 0.012 * vnoise(vec2(p.x * 9.0, 0.0));
  if (p.y < bedTop + 0.01){
    float bm = 1.0 - smoothstep(bedTop - 0.004, bedTop + 0.004, p.y);
    vec2 g = p * vec2(34.0, 40.0) * (uMedia < 0.5 ? 1.0 : 0.6);
    vec2 gi = floor(g), gf = fract(g);
    float md = 8.0, md2 = 8.0; float id = 0.0;
    for (int j = -1; j <= 1; j++) for (int i = -1; i <= 1; i++){
      vec2 nb = vec2(float(i), float(j));
      vec2 pt = nb + vec2(h21(gi + nb), h21(gi + nb + 17.0)) - gf;
      float dd = dot(pt, pt);
      if (dd < md){ md2 = md; md = dd; id = h21(gi + nb + 3.7); }
      else if (dd < md2){ md2 = dd; }
    }
    float cellEdge = sqrt(md2) - sqrt(md);
    float tw = 0.6 + 0.4 * sin(uClock * (1.0 + 2.0 * id) + id * 40.0);
    vec3 bed;
    if (uMedia < 0.5){
      float coal = smoothstep(0.0, 0.5, cellEdge) * (1.0 - smoothstep(0.25, 0.7, md));
      float hot = smoothstep(0.55, 1.0, id);
      bed = vec3(0.028, 0.022, 0.02) * (0.4 + id) * coal;
      bed += ember * emberB * ((1.0 - coal) * 0.55 + hot * coal * 0.9) * tw;
      bed += fireTint * fireLight * 0.12 * coal * id;
    } else {
      float facet = smoothstep(0.0, 0.25, cellEdge);
      bed = vec3(0.05) * id;
      bed += ember * emberB * (0.5 + 0.9 * id) * mix(1.4, 0.6, facet) * tw;
      bed += fireTint * fireLight * 0.12 * (0.4 + id);
      bed += vec3(1.0) * pow(id, 16.0) * (emberB + fireLight) * 0.6 * tw;
    }
    bed *= 0.55 + 0.45 * smoothstep(0.0, bedTop, p.y);
    col = mix(col, bed, bm);
  }

  // front flames, lower and softer, lick over the media
  vec2 fp2 = vec2(p.x + 0.37, p.y - baseY + 0.035);
  float T2 = max(fireField(fp2, t * 1.08, H * 0.42, 11.7), 0.0) * edge;
  col += flameColor(T2 * 0.85, yr) * smoothstep(0.25, 0.9, T2) * bright * 0.6;

  // sparks
  if (H > 0.02){
    for (int i = 0; i < 14; i++){
      float fi = float(i);
      float r1 = h21(vec2(fi, 1.3));
      float life = fract(uTime * (0.16 + 0.12 * r1) + r1 * 7.0);
      vec2 sp = vec2((0.12 + 0.76 * h21(vec2(fi, 7.7))) * A + sin(life * 6.0 + fi) * 0.025,
                     baseY + 0.05 + life * H * 1.35);
      float dd = length(p - sp);
      float s = (1.0 - life) * smoothstep(0.0045, 0.0, dd) * step(0.55, h21(vec2(fi, floor(uTime * 0.2 + r1 * 5.0))));
      col += vec3(1.0, 0.55, 0.18) * s * bright * 1.5;
    }
  }

  // glass vignette
  col *= 0.72 + 0.28 * smoothstep(0.0, 0.12, uv.y) * smoothstep(0.0, 0.08, uv.x) * smoothstep(0.0, 0.08, 1.0 - uv.x);

  col = 1.0 - exp(-col * 1.7);
  gl_FragColor = vec4(col, 1.0);
}