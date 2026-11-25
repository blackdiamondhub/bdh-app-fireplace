// Static checks for the kiosk page. Run: node --test tests/js/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const base = path.join(__dirname, '../../fireplace');
const read = (p) => fs.readFileSync(path.join(base, p), 'utf8');
const css = read('static/fireplace/fireplace.css');
const js = read('static/fireplace/fireplace.js');
const coreSrc = read('static/fireplace/fireplace-core.js');
const html = read('templates/fireplace/index.html');
const service = read('service.py');
const core = require('../../fireplace/static/fireplace/fireplace-core.js');

const rule = (sel) => {
  const esc = sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const m = css.match(new RegExp('(?:^|\\n)' + esc + '\\{([^}]*)\\}'));
  assert.ok(m, 'no rule for ' + sel);
  return m[1];
};

test('the page never scrolls on the 1920x1080 kiosk', () => {
  assert.match(rule('body.fp-page'), /overflow:hidden/);
  const root = rule('.fp');
  for (const d of ['position:fixed', 'top:0', 'left:0', 'right:0', 'bottom:0', 'overflow:hidden', 'flex-direction:column']) {
    assert.ok(root.includes(d), '.fp needs ' + d);
  }
  // The card and the firebox shrink to whatever is left; everything else keeps its size.
  assert.match(rule('.fp-card'), /flex:1 1 auto;min-height:0/);
  assert.match(rule('.fp-box'), /flex:1 1 auto;min-height:200px/);
  assert.match(rule('.fp-head,.fp-panel,.fp-presets'), /flex:none/);
  assert.doesNotMatch(css, /aspect-ratio/);
  assert.match(html, /\{% block body_class %\}fp-page\{% endblock %\}/);
});

test('nothing in the page scrolls, focuses or re-routes', () => {
  const bad = /\bscroll(?:To|By|IntoView)\s*\(|\bscrollTop\s*[-+]?=|\.focus\s*\(|location\.(?:hash|href)\s*=/;
  assert.doesNotMatch(js, bad);
  assert.doesNotMatch(coreSrc, bad);
});

test('touch targets are at least 44px', () => {
  const px = (sel, prop) => parseInt(rule(sel).match(new RegExp('(?:^|;)' + prop + ':(\\d+)px'))[1], 10);
  for (const sel of ['.fp-puck', '.fp-pwr', '.fp-step button', '.fp-sw button']) {
    assert.ok(px(sel, 'width') >= 44 && px(sel, 'height') >= 44, sel);
  }
  assert.ok(px('.fp-tab', 'min-height') >= 44);
  assert.ok(px('.fp-media', 'height') >= 44);
  assert.ok(px('.fp-row', 'min-height') >= 44);
});

test('the flames use FlameRenderer at the measured kiosk settings', () => {
  assert.match(js, /new window\.FlameRenderer\([^{]*\{[^}]*scale: 0\.8, maxFps: 30/);
  assert.doesNotMatch(js + coreSrc, /getContext\(/, 'no hand-rolled WebGL');
  const scripts = [...html.matchAll(/static '([^']+\.js)'/g)].map((m) => m[1]);
  assert.deepEqual(scripts, ['fireplace/flames.js', 'fireplace/fireplace-core.js', 'fireplace/fireplace.js']);
});

test('flames stop while the page is hidden and the debug overlay is opt-in', () => {
  const vis = js.slice(js.indexOf('addEventListener("visibilitychange"'));
  assert.match(vis.slice(0, 300), /document\.hidden[\s\S]*renderer\.stop\(\)/);
  assert.match(js, /fpdebug=1/);
  assert.match(js, /if \(DEBUG\) startDebug\(\)/);
});

test('media is remembered per fireplace', () => {
  assert.match(js, /fireplace\.media\.\$\{dsn\}/);
  assert.deepEqual(core.MEDIA, ['logs', 'crystals']);
});

test('presets match the server favourites', () => {
  const block = service.match(/FAVOURITES = \(([\s\S]*?)\n\)/)[1];
  const server = [...block.matchAll(/\("(\w+)", "([^"]+)"\)/g)].map((m) => [m[1], m[2]]);
  assert.deepEqual(core.PRESETS.map((p) => [p.option, p.name]), server);
});

test('every action the page sends is one the server accepts and the card handles', () => {
  const actions = service.match(/ACTIONS = \(([\s\S]*?)\)\n/)[1];
  const levels = [...service.match(/LEVELS = \{([\s\S]*?)\n\}/)[1].matchAll(/"(\w+)": \(/g)].map((m) => m[1]);
  const flags = [...service.match(/FLAGS = \{([\s\S]*?)\n\}/)[1].matchAll(/"(\w+)": \(/g)].map((m) => m[1]);
  const accepted = new Set([...actions.matchAll(/"(\w+)"/g)].map((m) => m[1]).concat(levels, flags));
  const sent = new Set(['power', 'heater', 'temp', 'favourite']);
  for (const m of js.matchAll(/(?:slider|tog|swatches)\("(\w+)"/g)) sent.add(m[1]);
  for (const a of sent) {
    assert.ok(accepted.has(a), `server doesn't accept ${a}`);
    assert.match(coreSrc, new RegExp(`case "${a}":`), `CardState.act doesn't handle ${a}`);
  }
  assert.ok(sent.size >= 15, [...sent].join(','));
});

test('every Font Awesome icon used exists in the hub (6.0.0-beta3, solid)', () => {
  // Checked against the hub's FA build; extend when adding icons.
  const known = new Set(['fa-fire', 'fa-power-off', 'fa-temperature-half', 'fa-lightbulb', 'fa-braille', 'fa-tree',
    'fa-gem', 'fa-gear', 'fa-triangle-exclamation', 'fa-champagne-glasses', 'fa-campground', 'fa-sun', 'fa-cloud-sun']);
  const used = new Set([...(js + coreSrc + html).matchAll(/"(fa-[a-z-]+)"|ICON\("(fa-[a-z-]+)"\)|fas (fa-[a-z-]+)/g)]
    .map((m) => m[1] || m[2] || m[3]));
  for (const i of used) assert.ok(known.has(i), i);
});
