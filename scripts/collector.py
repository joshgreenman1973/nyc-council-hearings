#!/usr/bin/env python3
"""Local sink for Legistar rows harvested in the browser.

The Legistar calendar is HTTPS, so it cannot fetch this HTTP server directly
(mixed content). Instead the harvest page window.opens /receiver here and
postMessages the payload across; the receiver, being same-origin with us,
POSTs it to /drop/<name>.
"""
import http.server, os, sys, datetime

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'raw'))
os.makedirs(OUT, exist_ok=True)

RECEIVER = b"""<!doctype html><meta charset=utf-8><title>collector</title>
<body style="font:14px/1.5 ui-monospace,monospace;padding:1rem">
<div id=log>waiting for payload...</div>
<script>
const log = document.getElementById('log');
window.addEventListener('message', async (e) => {
  const d = e.data;
  if (!d || typeof d !== 'object' || !d.__collect) return;
  log.textContent = 'received ' + d.name + ' (' + d.payload.length + ' bytes), posting...';
  const r = await fetch('/drop/' + encodeURIComponent(d.name), {method:'POST', body: d.payload});
  log.textContent = d.name + ' -> ' + r.status;
  if (e.source) e.source.postMessage({__collected: d.name, status: r.status}, '*');
});
if (window.opener) window.opener.postMessage({__collectorReady: true}, '*');
</script>
"""


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(RECEIVER)))
        self.end_headers()
        self.wfile.write(RECEIVER)

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n)
        name = self.path.rsplit('/', 1)[-1] or 'drop'
        name = ''.join(c for c in name if c.isalnum() or c in '-_.')
        path = os.path.join(OUT, name + '.json')
        with open(path, 'wb') as f:
            f.write(body)
        print(f'{datetime.datetime.now():%H:%M:%S}  {name}.json  {len(body):,} bytes', flush=True)
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    print(f'collector on http://127.0.0.1:{port}  -> {OUT}', flush=True)
    http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()
