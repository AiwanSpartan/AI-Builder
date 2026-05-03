from http.server import BaseHTTPRequestHandler, HTTPServer
import json
items = {}
counters = {}
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type='application/json'):
        self.send_response(code)
        self.send_header('Content-type', content_type)
        self.end_headers()
        if isinstance(body, (dict, list)):
            self.wfile.write(json.dumps(body).encode())
        else:
            self.wfile.write(str(body).encode())
    def do_GET(self):
        path = self.path
        if path == '/weather':
            self._send(200, {"temp": "temp_sample", "condition": "condition_sample"})
        if path == '/weather':
            self._send(200, {'status':'ok'})
        self._send(404, 'Not Found', 'text/plain')
    def do_POST(self):
        path = self.path
        length = int(self.headers.get('content-length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            data = json.loads(body.decode()) if body else {}
        except Exception:
            self._send(400, 'Invalid JSON', 'text/plain')
            return
        self._send(404, 'Not Found', 'text/plain')
def run(port: int = 8000):
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f'Serving on http://0.0.0.0:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Shutting down')
        server.server_close()
if __name__ == '__main__':
    run()