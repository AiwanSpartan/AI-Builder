"""Pipeline router and phased builder entrypoints.

`route_prompt(prompt, base_url)` classifies a prompt and dispatches to the
appropriate pipeline. For API prompts we provide a phased builder that:
- normalizes the prompt
- writes an `architect.txt` spec
- generates a minimal backend (`generated_app.py`)
- starts the app, runs the validator/repair loop, then stops the app

This module intentionally keeps the generated app simple so the validator
can exercise typical REST flows (GET lists, POST create, simple persistence).
"""
from typing import Optional
import subprocess
import os
import json
import time
import signal
import sys
import re

from prompt_normalizer import classify_prompt, normalize_prompt


PROJECT_ROOT = os.path.dirname(__file__)
PYTHON = os.path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe')


def run_api_pipeline(base_url: str = 'http://localhost:8000', log_path: Optional[str] = None) -> int:
    """Run the existing API pipeline (delegates to repair_loop).

    If `log_path` is provided, stdout/stderr will be written to that file.
    """
    cmd = f'"{PYTHON}" "{os.path.join(PROJECT_ROOT, "repair_loop.py")}"'
    if log_path:
        with open(log_path, 'wb') as fh:
            proc = subprocess.run(cmd, shell=True, stdout=fh, stderr=subprocess.STDOUT)
            return proc.returncode
    else:
        proc = subprocess.run(cmd, shell=True)
        return proc.returncode


def run_web_pipeline():
    # TODO: implement backend-first then UI phases
    print('web pipeline not implemented yet')
    return 1


def run_game_pipeline():
    # TODO: implement game-specific pipeline
    print('game pipeline not implemented yet')
    return 1


def _write_architect(requirements: list, path: str):
    with open(path, 'w', encoding='utf-8') as f:
        for line in requirements:
            f.write(f"- {line}\n")


def _parse_requirement_line(line: str):
    m = re.match(r"^(GET|POST|PUT|DELETE)\s+(/[^\s]*)", line, re.I)
    if not m:
        return None
    method = m.group(1).upper()
    path = m.group(2)
    brace = re.search(r"\{([^}]*)\}", line)
    keys = []
    if brace:
        body = brace.group(1)
        items = re.findall(r'"([^"]+)"|\b(\w+)\b', body)
        for a, b in items:
            k = a or b
            if k.lower() in ('number', 'string', 'int', 'float', 'bool'):
                continue
            keys.append(k)
    return {'method': method, 'path': path, 'keys': keys}


def build_backend_app(requirements: list, out_path: str, variant: int = 0):
    routes = []
    for r in requirements:
        parsed = _parse_requirement_line(r)
        if parsed:
            routes.append(parsed)

    tpl_lines = [
        "from http.server import BaseHTTPRequestHandler, HTTPServer",
        "import json",
        "items = {}",
        "counters = {}",
        "class Handler(BaseHTTPRequestHandler):",
        "    def _send(self, code, body, content_type='application/json'):",
        "        self.send_response(code)",
        "        self.send_header('Content-type', content_type)",
        "        self.end_headers()",
        "        if isinstance(body, (dict, list)):",
        "            self.wfile.write(json.dumps(body).encode())",
        "        else:",
        "            self.wfile.write(str(body).encode())",
        "    def do_GET(self):",
        "        path = self.path",
    ]

    for rt in routes:
        if rt['method'] == 'GET':
            lines = [
                f"        if path == '{rt['path']}':",
            ]
            if rt['keys']:
                # variant controls whether result is top-level object, wrapped, or list
                sample_obj = {k: (123 if k.lower().startswith('id') else k + '_sample') for k in rt['keys']}
                if variant == 0:
                    payload = json.dumps(sample_obj)
                elif variant == 1:
                    payload = json.dumps({'item': sample_obj})
                else:
                    payload = json.dumps([sample_obj])
                lines.append(f"            self._send(200, {payload})")
            else:
                lines.append("            self._send(200, {'status':'ok'})")
            tpl_lines.extend(lines)

    tpl_lines.extend([
        "        self._send(404, 'Not Found', 'text/plain')",
        "    def do_POST(self):",
        "        path = self.path",
        "        length = int(self.headers.get('content-length', 0))",
        "        body = self.rfile.read(length) if length else b''",
        "        try:",
        "            data = json.loads(body.decode()) if body else {}",
        "        except Exception:",
        "            self._send(400, 'Invalid JSON', 'text/plain')",
        "            return",
    ])

    for rt in routes:
        if rt['method'] == 'POST':
            tpl_lines.extend([
                f"        if path == '{rt['path']}':",
                "            key = path",
                "            cnt = counters.get(key, 0) + 1",
                "            counters[key] = cnt",
                "            item = {'id': cnt}",
                "            item.update(data)",
                "            arr = items.get(key, [])",
                "            arr.append(item)",
                "            items[key] = arr",
                # variant controls POST response shape
                ("            self._send(201, {'success': True, 'item': item})" if variant == 0 else
                 "            self._send(201, {'success': True, 'data': item})" if variant == 1 else
                 "            self._send(201, [item])"),
                "            return",
            ])

    tpl_lines.extend([
        "        self._send(404, 'Not Found', 'text/plain')",
        "def run(port: int = 8000):",
        "    server = HTTPServer(('0.0.0.0', port), Handler)",
        "    print(f'Serving on http://0.0.0.0:{port}')",
        "    try:",
        "        server.serve_forever()",
        "    except KeyboardInterrupt:",
        "        print('Shutting down')",
        "        server.server_close()",
        "if __name__ == '__main__':",
        "    run()",
    ])

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(tpl_lines))


def run_phased_builder(prompt: str, base_url: str = 'http://localhost:8000', max_retries: int = 3) -> int:
    ok, intent = normalize_prompt(prompt)
    if not ok:
        print('Prompt too complex; split required:', intent)
        return 2

    requirements = intent.get('requirements', [])
    arch_path = os.path.join(PROJECT_ROOT, 'architect.txt')
    _write_architect(requirements, arch_path)
    print('Wrote architect spec to', arch_path)

    gen_app = os.path.join(PROJECT_ROOT, 'generated_app.py')

    last_rc = 1
    # Try multiple variants to increase chance of matching expected shapes
    attempts = []
    reports_dir = os.path.join(PROJECT_ROOT, 'test_reports')
    os.makedirs(reports_dir, exist_ok=True)
    ts = int(time.time())

    for attempt in range(max_retries):
        variant = attempt
        print(f'Build attempt {attempt+1}/{max_retries} using variant={variant}')
        build_backend_app(requirements, gen_app, variant=variant)
        print('Generated backend at', gen_app)
        # start the generated app and capture its output to a per-variant log
        proc = subprocess.Popen(f'"{PYTHON}" "{gen_app}"', shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log_file = os.path.join(reports_dir, f'pipeline_variant_{ts}_v{variant}.txt')
        try:
            time.sleep(1.0 + 0.5 * attempt)
            # run the validator/repair loop and capture its output
            rc = run_api_pipeline(base_url, log_path=log_file)
            last_rc = rc
            attempts.append({'variant': variant, 'rc': rc, 'log': log_file})
            if rc == 0:
                print('Variant succeeded')
                # write a summary report
                summary = {'prompt': prompt, 'attempts': attempts, 'result': 'success', 'timestamp': ts}
                summary_path = os.path.join(reports_dir, f'pipeline_variant_report_{ts}.json')
                with open(summary_path, 'w', encoding='utf-8') as sf:
                    json.dump(summary, sf, indent=2)
                return 0
            else:
                print('Variant failed, trying next variant')
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # write failure summary
    summary = {'prompt': prompt, 'attempts': attempts, 'result': 'failure', 'timestamp': ts}
    summary_path = os.path.join(reports_dir, f'pipeline_variant_report_{ts}.json')
    with open(summary_path, 'w', encoding='utf-8') as sf:
        json.dump(summary, sf, indent=2)
    print('All variants tried; pipeline failed')
    print('Wrote report to', summary_path)
    return last_rc


def route_prompt(prompt: str, base_url: Optional[str] = None) -> int:
    ok, intent = normalize_prompt(prompt)
    if not ok:
        print('Prompt rejected or needs splitting:', intent)
        return 2

    ptype = intent.get('app_type')
    if ptype == 'api':
        return run_phased_builder(prompt, base_url or 'http://localhost:8000')
    if ptype == 'web':
        return run_web_pipeline()
    if ptype == 'game':
        return run_game_pipeline()

    print('Unknown pipeline for prompt type', ptype)
    return 3


if __name__ == '__main__':
    print('Use route_prompt(prompt, base_url) to dispatch')
