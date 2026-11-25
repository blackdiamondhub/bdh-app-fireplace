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
