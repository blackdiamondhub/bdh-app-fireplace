"""Bake the fireplace flame density loops.

Serves bake.html to a headless Chromium (SwiftShader WebGL), collects the frames it
renders with the original card's fireField() and encodes each loop to a grayscale
H.264 mp4 under --out. Usage (builder.mi):

  python3 tools/bake/bake_server.py --out fireplace/static/fireplace/flames
"""
import argparse, http.server, json, os, pathlib, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent
ASPECT, BASE, FPS = 2.5, 0.13, 30


def jobs(scale):
    w = int(round(700 * scale / 2)) * 2
    out = []
    for level in range(1, 6):
        fs = level - 1
        common = dict(aspect=ASPECT, base=BASE, fps=FPS, height=0.42 + 0.1 * fs,
                      speed=0.65 + 0.14 * fs, t0=10.0 + 37.0 * level)
        # Back flames: whole firebox, 8 s loop. Front flames: lower 60%, 7.3 s loop,
        # so the two loops drift against each other and the repeat is hard to spot.
        out.append(dict(common, name=f"back_{level}", field="back", w=w, h=int(round(w / ASPECT / 2)) * 2,
                        y0=0.0, y1=1.0, frames=240, xfade=45))
        out.append(dict(common, name=f"front_{level}", field="front", w=w, h=int(round(w / ASPECT * 0.6 / 2)) * 2,
                        y0=0.0, y1=0.6, frames=219, xfade=45))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--only", default="")
    ap.add_argument("--crf", type=int, default=14)
    ap.add_argument("--chrome", default="")
    ap.add_argument("--gpu", action="store_true", help="use the local GPU instead of SwiftShader")
    a = ap.parse_args()
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    js = [j for j in jobs(a.scale) if not a.only or j["name"] in a.only.split(",")]
    byname = {j["name"]: j for j in js}
    frames = {}
    done = threading.Event()
    err = []

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *x, **k):
            super().__init__(*x, directory=str(HERE), **k)

        def log_message(self, *x):
            pass

        def do_GET(self):
            if self.path.startswith("/jobs.json"):
                body = json.dumps(js).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            super().do_GET()

        def do_POST(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if u.path == "/frame":
                j = byname[q["job"]]
                w, h = j["w"], j["h"]
                # readPixels is bottom-up; video rows are top-down
                frames.setdefault(q["job"], {})[int(q["i"])] = b"".join(
                    body[(h - 1 - r) * w:(h - r) * w] for r in range(h))
            elif u.path == "/done":
                if q["job"] in frames:  # retried POSTs must not re-encode
                    encode(byname[q["job"]], frames.pop(q["job"]), out, a.crf)
            elif u.path == "/alldone":
                done.set()
            elif u.path == "/error":
                err.append(body.decode("utf-8", "replace")); done.set()
            self.send_response(204); self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{a.port}/bake.html"
    chrome = ([a.chrome] if os.path.exists(a.chrome) else a.chrome.split()) if a.chrome else [
        "docker", "run", "--rm", "--network", "host", "--name", "fp-bake", "zenika/alpine-chrome:latest"]
    sw = [] if a.gpu else ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    prof = ["--user-data-dir=" + str(out / ".prof")] if a.chrome else []
    cmd = chrome + ["--headless=new", "--no-sandbox", *sw, *prof,
                    "--ignore-gpu-blocklist", "--disable-gpu-watchdog", "--remote-debugging-port=0", url]
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    done.wait()
    if a.chrome == "":
        subprocess.run(["docker", "rm", "-f", "fp-bake"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.kill()
    if err:
        sys.exit("bake failed: " + err[0])
    print(f"baked {len(js)} loops in {time.time() - t0:.0f}s")


def encode(j, fr, out, crf):
    n = j["frames"]
    missing = [i for i in range(n) if i not in fr]
    if missing:
        raise RuntimeError(f"{j['name']}: missing frames {missing[:5]}")
    raw = b"".join(fr[i] for i in range(n))
    dst = out / f"{j['name']}.mp4"
    (out / f"{j['name']}.json").write_text(json.dumps({k: j[k] for k in (
        "w", "h", "y0", "y1", "frames", "fps", "height", "speed", "aspect", "base", "xfade")}))
    if crf < 0:  # raw dump only; encode elsewhere with encode_raw.sh
        (out / f"{j['name']}.gray").write_bytes(raw)
        print(f"{j['name']}: raw {len(raw) // 1024} KB", flush=True)
        return
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{j['w']}x{j['h']}",
                    "-r", str(j["fps"]), "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
                    "-tune", "grain", "-pix_fmt", "yuv420p", "-g", "30", "-movflags", "+faststart", str(dst)],
                   input=raw, check=True)
    print(f"{j['name']}: {dst.stat().st_size // 1024} KB", flush=True)


if __name__ == "__main__":
    main()
