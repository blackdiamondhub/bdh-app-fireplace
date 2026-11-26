# bdh-app-fireplace

Black Diamond Hub app for Napoleon Astound fireplaces. It talks to the Napoleon cloud through
[pynapoleon](https://github.com/sslivins/pynapoleon), with no Home Assistant involved.

The page is a port of hass-napoleon's `napoleon-fireplace-card`. It has the firebox with live
flames, power, presets/favourites, heater and set temperature, flame speed and orange/yellow
flame levels, ember bed colour and brightness, top light colour, and the eco/boost/colour-cycle
toggles. Each fireplace on the account gets a card, with tabs when there is more than one. Logs or
crystals media is remembered per fireplace in the browser.

### Your own presets

"Save preset" stores the current flame height, orange/yellow levels, ember bed and top light
(never the heater, eco or boost) under a name, up to 8 per folder. Presets live on the hub, so
every screen sees them and any fireplace can use them. Owners (owner mode, or hub admins) save
house presets. Guests see the house presets but can't change them; they save into a separate
guest folder, and guest presets are deleted 14 days after they were last saved.

## Settings

| Key | Type | |
| --- | --- | --- |
| `napoleon_email` | text | Napoleon (Astound app) account email |
| `napoleon_password` | secret | Account password |
| `napoleon_europe` | bool | Use the EU cloud |

If the settings are empty, the page shows a "fill in the settings" message. An account with no
fireplaces shows a friendly empty state.

## Homepage

- Status: "Fireplace on", "1 of 2 fireplaces on", or "Fireplaces off"/"Fireplace off". It shows
  "Not set up" when there are no credentials, and nothing when the account has no fireplaces.
- Shortcut: "Fireplaces off" (or "Fireplace off" with a single fireplace) appears only while one
  is lit. It turns off just the lit ones. If none are lit by the time it runs, it answers
  "Fireplaces are already off". This behaviour moved here from the Controls app.
- Tile icon: animated flames (`hub/icon-on.svg`, SVG SMIL) while a fireplace is on, and an
  empty firebox (`hub/icon.svg`) otherwise. Needs a hub with `Tile.icon_on` support.

## How it works

- **One cloud client.** `cloud.py` runs one `NapoleonClient` on an asyncio thread.
  - Sign-ins are latched. There is at most one login per 60 s. After repeated rejected passwords
    the gap backs off 60, 120, 240, 480 and then 900 s, and the page shows "Check Napoleon
    email/password in settings".
  - Changing the credentials resets the latch.
  - A session the cloud rejects gets one fresh sign-in, still subject to the 60 s gap.
- **Polling.** `worker.py` polls every 12 s while someone has had the page open in the last
  2 minutes, and every 5 minutes otherwise. That is enough for the homepage status. Whether a
  fireplace is online comes from the Ayla device list's `connection_status`, because pynapoleon's
  `is_online` is always `None`.
- **API.**
  - `GET api/state/` returns `{configured, error, fireplaces: [{id, name, online, model}]}`, where
    `model` is the card's `_model()` shape plus heater, setpoint, temperature range and favourites.
  - `POST api/act/` takes `{id, action, value}` using the card's action names: `power`, `heater`
    (0 off, 1 low, 2 high), `temp` (an absolute setpoint, 18-23 °C), `flame_speed`/`orange`/`yellow`
    (1-5), `ember_bri` (0-4; 0 turns the ember bed off), `ember_rgb`/`top_rgb` (`[r,g,b]` 0-255),
    `top_on`, `eco`, `boost`, `ember_cycle`, `top_cycle` and `favourite`.
  - Everything is validated server-side. Power is the master switch, like the HA integration:
    other actions on an off fireplace return 409.
- **No snap-back.** After a write, the server re-reads that fireplace. Values it just set stay
  pending for 12 s, so a lagging cloud read can't flip the UI back. The page also keeps its own
  optimistic pending values and debounces sliders and temperature.
- **Ember brightness** is 0-4 on these fireplaces (HA's light showed 0-5). The flames scale it to
  the renderer's 0-5.
- **Memory.** The SDK image smoke test measures 58 MB RSS idle. pynapoleon and aiohttp add about
  32 MB once signed in, so `memory_mb` is 128.

## Tests

- Python: `manage.py test fireplace` inside the hub, run by the SDK's `app-ci.yml`. The tests use
  fake pynapoleon clients and never touch real fireplaces.
- JS: `node --test tests/js/*.test.js` on Node 22, with no dependencies. It covers the card state
  and optimistic logic, the model-to-flame mapping, and the kiosk no-scroll and touch-size rules.

## Flame renderer

The flames look like the `napoleon-fireplace-card` shader from hass-napoleon, but its noise field is
far too expensive for the kiosk display's GPU (about 1 fps on an Adreno 506). Instead, the flame
density is baked offline into short H.264 loops (`fireplace/static/fireplace/flames/`). These are
unpacked once into an RGBA atlas on the GPU and composited with the card's colour and lighting maths
by `flames.js`. That gives 46 fps at 1120x448 on the Sun Peaks display. The page runs it at a 0.8
render scale, capped at 30 fps for even pacing.

Add `?fpdebug=1` to the page URL to show an overlay with the renderer stats and the measured fps.

- `tools/bake/` renders the card's `fireField()` frame by frame and encodes the loops.
  `python tools/bake/bake_server.py --out <dir> --gpu --chrome <edge-or-chrome> --crf -1` dumps raw
  frames on a real GPU (this matches the card's look; SwiftShader rounds the noise hash differently).
  Then encode them with the ffmpeg line in `encode()`.
- `tools/parity/` renders the card's original shader and `FlameRenderer` side by side and reports
  PSNR (`pwsh tools/parity/run.ps1`). Currently that is 44-53 dB with flames and 76 dB with them off.