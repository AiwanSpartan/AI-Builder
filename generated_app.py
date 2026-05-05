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
        if path == '/todos':
            self._send(200, items.get('/todos', []))
            return
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
        if path == '/todos':
            key = path
            cnt = counters.get(key, 0) + 1
            counters[key] = cnt
            item = {'id': cnt}
            item.update(data)
            arr = items.get(key, [])
            arr.append(item)
            items[key] = arr
            self._send(201, {'success': True, 'item': item})
            return
        self._send(404, 'Not Found', 'text/plain')
import sys, os
def run(port: int = 8000):
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f'Serving on http://0.0.0.0:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Shutting down')
        server.server_close()
if __name__ == '__main__':
    import sys, os
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('PORT', 8000))
    run(port)