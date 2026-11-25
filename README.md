# bdh-app-fireplace

Black Diamond Hub app for Napoleon Astound fireplaces, talking to the Napoleon cloud through
[pynapoleon](https://github.com/sslivins/pynapoleon) (no Home Assistant).

## Flame renderer

The flames look like the 
apoleon-fireplace-card shader from hass-napoleon, but the noise field is
far too expensive for the kiosk display's GPU (about 1 fps on an Adreno 506). So the flame density is
baked offline into short H.264 loops (ireplace/static/fireplace/flames/), unpacked once into an RGBA
atlas on the GPU, and composited with the card's colour/lighting maths by lames.js
(46 fps at 1120x448 on the Sun Peaks display).

- 	ools/bake/ - renders the card's ireField() frame by frame and encodes the loops.
  python tools/bake/bake_server.py --out <dir> --gpu --chrome <edge-or-chrome> --crf -1 dumps raw
  frames on a real GPU (matches the card's look; SwiftShader rounds the noise hash differently), then
  encode with the ffmpeg line in ncode().
- 	ools/parity/ - renders the card's original shader and FlameRenderer side by side and reports
  PSNR (pwsh tools/parity/run.ps1). Current: 44-53 dB with flames, 76 dB off.
