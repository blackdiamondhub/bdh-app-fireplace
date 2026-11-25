import http.server, sys, os, json, base64
OUT = sys.argv[2]
class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        name = self.path.split("?name=")[-1]
        if name.endswith(".png"): body = base64.b64decode(body.split(b",", 1)[1])
        open(os.path.join(OUT, name), "wb").write(body)
        self.send_response(200); self.end_headers()
os.makedirs(OUT, exist_ok=True)
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
