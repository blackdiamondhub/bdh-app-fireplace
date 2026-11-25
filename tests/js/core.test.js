// Card logic (fireplace-core.js) and the renderer mapping. Run: node --test tests/js/*.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../../fireplace/static/fireplace/fireplace-core.js');
const { targetsFromModel } = require('../../fireplace/static/fireplace/flames.js');

function model(over) {
  return Object.assign({
    available: true, power: true, heater: 1, setpoint: 21, minT: 18, maxT: 23, stepT: 1,
    flame_speed: 3, orange: 2, yellow: 2, ember_on: true, ember_bri: 4, ember_max: 4, ember_rgb: [255, 90, 30],
    top_on: false, top_rgb: [0, 0, 0], favourite: null, eco: false, boost: false, ember_cycle: false, top_cycle: false,
  }, over || {});
}

// A CardState with a fake clock/timers and a recording send().
function harness(serverModel, opts) {
  const h = { now: 1000, timers: [], sent: [], errors: [], changes: 0, fail: null };
  h.card = new core.CardState((action, value) => {
    h.sent.push([action, value]);
    if (h.fail) return Promise.reject(new Error(h.fail));
    return Promise.resolve(opts && opts.reply ? { model: opts.reply(action, value) } : null);
  }, {
    now: () => h.now,
    setTimeout: (fn, ms) => { const t = { fn, at: h.now + ms }; h.timers.push(t); return t; },
    clearTimeout: (t) => { h.timers = h.timers.filter((x) => x !== t); },
    onChange: () => h.changes++,
    onError: (m) => h.errors.push(m),
  });
  h.card.setServer(model(serverModel));
  h.advance = (ms) => {
    h.now += ms;
    const due = h.timers.filter((t) => t.at <= h.now);
    h.timers = h.timers.filter((t) => t.at > h.now);
    due.forEach((t) => t.fn());
  };
  h.flush = () => new Promise((r) => setImmediate(r));
  return h;
}

test('hex <-> rgb', () => {
  assert.deepEqual(core.hexToRgb('#3a7bff'), [0x3a, 0x7b, 0xff]);
  assert.deepEqual(core.hexToRgb('FFFFFF'), [255, 255, 255]);
  assert.deepEqual(core.hexToRgb('nope'), [255, 90, 30]);
  assert.equal(core.rgbToHex([255, 90, 30]), '#ff5a1e');
  assert.equal(core.rgbToHex([300, -4, 7.6]), '#ff0008');
  for (const hex of core.SWATCHES) assert.equal(core.rgbToHex(core.hexToRgb(hex)), hex);
});

test('the renderer gets ember brightness on its 0..5 scale', () => {
  assert.equal(core.rendererModel(model({ ember_bri: 4 })).ember_bri, 5);
  assert.equal(core.rendererModel(model({ ember_bri: 2 })).ember_bri, 2.5);
  assert.equal(core.rendererModel(model({ ember_bri: 0 })).ember_bri, 0);
  // Full ember brightness on the device is full brightness in the renderer.
  const full = targetsFromModel(core.rendererModel(model({ ember_bri: 4 })));
  const five = targetsFromModel(model({ ember_bri: 5 }));
  assert.equal(full.emberBri, 1);
  assert.equal(full.emberBri, five.emberBri);
  assert.equal(targetsFromModel(core.rendererModel(model({ ember_bri: 2 }))).emberBri, 0.5);
  const off = targetsFromModel(core.rendererModel(model({ power: false, ember_on: false, ember_bri: 0 })));
  assert.equal(off.on, 0);
});

test('status line', () => {
  assert.equal(core.statusText(model({ available: false })), 'Unavailable');
  assert.equal(core.statusText(model({ power: false })), 'Off');
  assert.equal(core.statusText(model()), '<b>On</b> · Flame 3/5 · Heat <b>Low</b> · 21°C');
  assert.equal(core.statusText(model({ heater: 2, setpoint: null })), '<b>On</b> · Flame 3/5 · Heat <b>High</b> · –°C');
  assert.equal(core.statusText(model({ heater: 0 })), '<b>On</b> · Flame 3/5 · Heater off');
});

test('model: off fireplace shows the heater off', () => {
  const h = harness({ power: false, heater: 2 });
  assert.equal(h.card.model().heater, 0);
});

test('power toggles optimistically and closes the open panel', async () => {
  const h = harness();
  h.card.panel = 'flame';
  h.card.act('power');
  assert.equal(h.card.model().power, false);
  assert.equal(h.card.panel, null);
  assert.deepEqual(h.sent, [['power', false]]);
  // A stale poll still saying "on" doesn't flip it back...
  h.card.setServer(model({ power: true }));
  assert.equal(h.card.model().power, false);
  // ...until the pending window runs out.
  h.advance(core.PENDING_MS + 1);
  assert.equal(h.card.model().power, true);
});

test('pending clears as soon as the server agrees', () => {
  const h = harness();
  h.card.act('eco', true);
  h.card.setServer(model({ eco: true }));
  assert.equal(h.card.model().eco, true);
  // Changed elsewhere afterwards: the new server value shows right away.
  h.card.setServer(model({ eco: false }));
  assert.equal(h.card.model().eco, false);
});

test('temp sends an absolute, clamped setpoint after a debounce', () => {
  const h = harness({ setpoint: 21 });
  h.card.act('temp', 1);
  h.card.act('temp', 1);
  h.card.act('temp', 1); // 24 is past maxT
  assert.equal(h.card.model().setpoint, 23);
  assert.deepEqual(h.sent, []);
  h.advance(700);
  assert.deepEqual(h.sent, [['temp', 23]]);
  h.card.act('temp', -1);
  h.advance(700);
  assert.deepEqual(h.sent.at(-1), ['temp', 22]);
  const low = harness({ setpoint: null });
  low.card.act('temp', -1);
  low.advance(700);
  assert.deepEqual(low.sent, [['temp', 18]]);
});

test('sliders are debounced and only the last value is sent', () => {
  const h = harness();
  [1, 2, 4, 5].forEach((v) => h.card.act('flame_speed', v));
  h.card.act('orange', 0);
  assert.equal(h.card.model().flame_speed, 5);
  h.advance(399);
  assert.deepEqual(h.sent, []);
  h.advance(1);
  assert.deepEqual(h.sent, [['flame_speed', 5], ['orange', 0]]);
});

test('ember brightness lights the ember bed and powers on', () => {
  const h = harness({ power: false, ember_on: false, ember_bri: 0 });
  h.card.act('ember_bri', 3);
  const m = h.card.model();
  assert.deepEqual([m.power, m.ember_on, m.ember_bri], [true, true, 3]);
  h.advance(400);
  assert.deepEqual(h.sent, [['ember_bri', 3]]);
});

test('top colour turns the top light on', () => {
  const h = harness();
  h.card.act('top_rgb', [0, 200, 255]);
  assert.equal(h.card.model().top_on, true);
  assert.deepEqual(h.card.model().top_rgb, [0, 200, 255]);
  h.advance(400);
  assert.deepEqual(h.sent, [['top_rgb', [0, 200, 255]]]);
});

test('a preset on an off fireplace shows it on right away', () => {
  const h = harness({ power: false });
  h.card.act('favourite', 'partytime');
  assert.deepEqual([h.card.model().power, h.card.model().favourite], [true, 'partytime']);
  assert.deepEqual(h.sent, [['favourite', 'partytime']]);
});

test('heater on powers on; immediate actions are sent at once', () => {
  const h = harness({ power: false, heater: 0 });
  h.card.act('heater', 2);
  assert.deepEqual([h.card.model().power, h.card.model().heater], [true, 2]);
  h.card.act('top_on', true);
  h.card.act('boost', true);
  assert.deepEqual(h.sent, [['heater', 2], ['top_on', true], ['boost', true]]);
});

test('a failed action rolls back and reports the error', async () => {
  const h = harness();
  h.fail = 'Turn the fireplace on first';
  h.card.act('eco', true);
  assert.equal(h.card.model().eco, true);
  await h.flush();
  assert.equal(h.card.model().eco, false);
  assert.deepEqual(h.errors, ['Turn the fireplace on first']);
});

test("the server's reply replaces the model", async () => {
  const h = harness({}, { reply: () => model({ flame_speed: 1, orange: 4 }) });
  h.card.act('eco', true);
  await h.flush();
  assert.equal(h.card.server.orange, 4);
});

test('unknown actions do nothing', () => {
  const h = harness();
  h.card.act('explode', 1);
  assert.deepEqual(h.sent, []);
  assert.equal(h.changes, 0);
});
