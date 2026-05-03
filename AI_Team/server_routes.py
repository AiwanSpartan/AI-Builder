"""Flask and websocket routes for the AI Office Builder backend."""

import atexit
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import zipfile
from urllib.error import URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

import ollama
from flask import jsonify, make_response, request, send_file

from server_ai import clamp_text, extract_code, try_repair_code, validate_generated_code
from server_config import BASE_DIR, BUILDS_DIR, MAX_AUTO_REPAIR_ATTEMPTS, MODELS, NPC_CHAT_MODEL
from server_io import (
    create_project_bundle,
    ensure_generated_templates_for_project,
    get_latest_built_file,
    get_latest_project_dir,
    read_text,
    refresh_project_zip,
    save_repaired_project_main,
)
from server_personas import NPC_PERSONAS
from server_pipeline import build_pipeline, classify_request_scope
from server_state import (
    app,
    broadcast,
    build_state,
    clients,
    log,
    reset_build_state_for_new_request,
    sock,
)


APP_RUNTIME_BIND_HOST = "0.0.0.0"
APP_RUNTIME_LOCAL_HEALTH_HOST = "127.0.0.1"
APP_RUNTIME_PORT_START = 5600
APP_RUNTIME_PORT_END = 5699
APP_RUNTIME_START_TIMEOUT_SECONDS = 12
NPC_MAX_REPLY_SENTENCES = 2
NPC_MAX_REPLY_CHARS = 180
WEB_SEARCH_TIMEOUT_SECONDS = 2
WEB_SEARCH_MAX_SNIPPETS = 2
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_SMALLTALK_RE = re.compile(
    r"^(?:\s*)(hi|hello|hey|hey there|yo|sup|what'?s up|hii+|heyy+)(?:[\s!,.?]*)$",
    flags=re.IGNORECASE,
)
_FACTUAL_RE = re.compile(
    r'\b(what is|what are|who is|who are|when did|when is|where is|where are|'
    r'define|explain|how does|how do|why does|why is|latest|current|news|'
    r'tell me about|can you tell|do you know|fact about|history of)\b',
    flags=re.IGNORECASE,
)

def _needs_web_search(question):
    """Return True only for questions that look factual/lookup — skip for smalltalk."""
    q = question.strip()
    if not q or len(q) < 8:
        return False
    if _SMALLTALK_RE.match(q):
        return False
    return bool(_FACTUAL_RE.search(q))

_NO_ANSWER_MARKERS = (
    "i do not know",
    "i don't know",
    "dont know",
    "don't have information",
    "do not have information",
    "cannot access",
    "can't access",
    "no information",
    "not sure",
)
_REQUIREMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_IGNORE_REQUIREMENT_NAMES = {
    "src",
    "app",
    "apps",
    "main",
    "project",
    "tests",
    "test",
}

_RUNTIME_LAUNCHER_CODE = """
import os
import runpy

host = os.environ.get("AIBUILDER_APP_HOST", "0.0.0.0")
port = int(os.environ.get("AIBUILDER_APP_PORT", "5600"))

try:
    import flask
    _original_flask_run = flask.Flask.run

    def _forced_flask_run(self, *args, **kwargs):
        kwargs["host"] = host
        kwargs["port"] = port
        kwargs["use_reloader"] = False
        return _original_flask_run(self, *args, **kwargs)

    flask.Flask.run = _forced_flask_run
except Exception:
    pass

runpy.run_path(os.environ["AIBUILDER_MAIN_FILE"], run_name="__main__")
"""

_runtime_lock = threading.Lock()
_runtime = {
    "process": None,
    "project_dir": None,
    "port": None,
    "url": None,
    "log_path": None,
    "mode": None,
    "summary": None,
    "app_label": None,
}


def _is_process_running(proc):
    return proc is not None and proc.poll() is None


def _clear_runtime_state_locked():
    _runtime["process"] = None
    _runtime["project_dir"] = None
    _runtime["port"] = None
    _runtime["url"] = None
    _runtime["log_path"] = None
    _runtime["mode"] = None
    _runtime["summary"] = None
    _runtime["app_label"] = None


def _stop_runtime_locked():
    proc = _runtime.get("process")
    if _is_process_running(proc):
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    _clear_runtime_state_locked()


def _find_free_port(host, start_port, end_port):
    for port in range(start_port, end_port + 1):
        # Quick listener check prevents collisions with stale runtime processes.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
            probe_sock.settimeout(0.2)
            if probe_sock.connect_ex((host, port)) == 0:
                continue

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock_obj:
            # On Windows, SO_REUSEADDR can report a port as available while another
            # process is still listening, so prefer exclusive bind semantics.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                try:
                    sock_obj.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass
            try:
                sock_obj.bind((host, port))
            except OSError:
                continue
            return port
    return None


def _wait_for_http_port(host, port, timeout_seconds):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock_obj:
            sock_obj.settimeout(0.35)
            try:
                sock_obj.connect((host, port))
                return True
            except OSError:
                pass
        time.sleep(0.2)
    return False


def _read_log_tail(log_path, max_chars=1200):
    if not log_path or not os.path.exists(log_path):
        return ""
    try:
        text = read_text(log_path)
        return text[-max_chars:]
    except Exception:
        return ""


def _start_runtime_process(project_dir, main_file, bind_host, port, log_path):
    env = os.environ.copy()
    env["AIBUILDER_MAIN_FILE"] = main_file
    env["AIBUILDER_APP_HOST"] = bind_host
    env["AIBUILDER_APP_PORT"] = str(port)
    env["PORT"] = str(port)
    env["FLASK_RUN_PORT"] = str(port)

    with open(log_path, "a", encoding="utf-8") as log_handle:
        log_handle.write(f"\n=== Launch at {time.strftime('%Y-%m-%d %H:%M:%S')} on port {port} ===\n")

    with open(log_path, "a", encoding="utf-8") as log_handle:
        return subprocess.Popen(
            [sys.executable, "-u", "-c", _RUNTIME_LAUNCHER_CODE],
            cwd=project_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )


_HTML_FENCE_RE = re.compile(r"```(?:html)?\s*([\s\S]*?)```", re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_HTML_START_RE = re.compile(r"<!doctype\s+html|<html[\s>]", re.IGNORECASE)


def _llm_improve_template(source, template_path):
    """Background: ask LLM to write a better HTML template and save it if valid."""
    prompt = (
        "You are writing templates/index.html for a Flask web app.\n"
        "Output ONLY the complete HTML file starting with <!doctype html>.\n"
        "No explanation, no <think> tags, no markdown fences, no text before the HTML.\n"
        "Requirements:\n"
        "- Vanilla HTML5 with inline CSS and JS fetch() calls matching the Flask routes.\n"
        "- Modern dark theme (dark navy background, indigo/purple accents).\n"
        "- Working CRUD UI: form to add items, list to display them, delete buttons.\n\n"
        f"Flask source:\n```python\n{source[:3000]}\n```\n\n"
        "<!doctype html>"
    )
    try:
        resp = ollama.chat(
            model="qwen2.5-coder:7b",
            messages=[{"role": "user", "content": prompt}],
            options={"num_predict": 2400, "temperature": 0.15},
            keep_alive="5m",
        )
        raw = (resp.get("message", {}).get("content", "") or "").strip()
        # Strip <think>...</think> blocks (deepseek-r1 style)
        raw = _THINK_TAG_RE.sub("", raw).strip()
        # Strip markdown fences
        fence_match = _HTML_FENCE_RE.search(raw)
        if fence_match:
            raw = fence_match.group(1).strip()
        # Find the HTML doctype/root element anywhere in the response
        start = _HTML_START_RE.search(raw)
        if start:
            html_text = raw[start.start():]
            # Prepend doctype if model continued after our "<!doctype html>" primer
            if not html_text.lower().startswith("<!doctype"):
                html_text = "<!doctype html>\n" + html_text
            with open(template_path, "w", encoding="utf-8") as _fh:
                _fh.write(html_text)
            log(f"[RUN] LLM-improved template saved to {template_path}")
            return
        log("[RUN] LLM output contained no HTML — keeping smart fallback template")
    except Exception as exc:
        log(f"[RUN] LLM template improvement failed: {exc}")


def _ensure_index_template_for_flask(project_dir, source):
    result = ensure_generated_templates_for_project(project_dir, source)
    status = result.get("status")
    kind   = result.get("kind", "")
    path   = result.get("path", "")
    if status in {"generated", "upgraded", "copied"}:
        source_path = result.get("source", "")
        if source_path:
            log(f"[RUN] Template {status} ({kind}) at {path} from {source_path}")
        else:
            log(f"[RUN] Template {status} ({kind}) at {path}")
        # Note: previously fired a background LLM template improvement here, but it
        # produced a CRUD template whose fetch() calls didn't match the actual Flask
        # routes (e.g. POST /api/items vs GET /get_weather), causing 405s. We now
        # rely on smart_rescue rewriting the whole app to a single-file embedded
        # HTML version when render_template() is detected — see _run_uses_external_templates.


def _public_runtime_host_for_request(req):
    host = (req.host or "").split(":", 1)[0].strip()
    if host in {"", "0.0.0.0", "::"}:
        return APP_RUNTIME_LOCAL_HEALTH_HOST
    return host


_RENDER_TEMPLATE_CALL_RE = re.compile(r"render_template\s*\(\s*['\"]")
_RENDER_TEMPLATE_STRING_CALL_RE = re.compile(r"render_template_string\s*\(")


def _uses_external_templates(source):
    """True if source calls render_template('foo.html') — the fragile path that
    requires templates/ files whose fetch() calls must match Flask routes exactly.
    render_template_string is fine because the HTML/JS lives in the same file as the routes."""
    text = source or ""
    if not _RENDER_TEMPLATE_CALL_RE.search(text):
        return False
    # If they ALSO use render_template_string, the embedded path is the primary one.
    if _RENDER_TEMPLATE_STRING_CALL_RE.search(text):
        return False
    return True


def _looks_like_web_app_source(source):
    lowered = (source or "").lower()

    if "@app.route" in lowered and "app.run(" in lowered:
        return True
    if "fastapi(" in lowered and ("uvicorn.run" in lowered or "@app.get(" in lowered):
        return True
    if "streamlit" in lowered and "import streamlit" in lowered:
        return True
    if "gradio" in lowered and ".launch(" in lowered:
        return True
    if "dash(" in lowered and ".run_server(" in lowered:
        return True

    return False


# Maps Python import names to their PyPI install names where they differ.
_IMPORT_TO_PYPI = {
    'flask_login': 'flask-login',
    'flask_sqlalchemy': 'flask-sqlalchemy',
    'flask_wtf': 'flask-wtf',
    'flask_migrate': 'flask-migrate',
    'flask_mail': 'flask-mail',
    'flask_bcrypt': 'flask-bcrypt',
    'flask_jwt_extended': 'flask-jwt-extended',
    'flask_restful': 'flask-restful',
    'flask_cors': 'flask-cors',
    'flask_socketio': 'flask-socketio',
    'dotenv': 'python-dotenv',
    'cv2': 'opencv-python',
    'PIL': 'Pillow',
    'sklearn': 'scikit-learn',
    'bs4': 'beautifulsoup4',
    'yaml': 'PyYAML',
    'dateutil': 'python-dateutil',
    'jwt': 'PyJWT',
    'pymongo': 'pymongo',
    'psycopg2': 'psycopg2-binary',
    'sqlalchemy': 'SQLAlchemy',
    'stripe': 'stripe',
    'requests': 'requests',
    'aiohttp': 'aiohttp',
    'pydantic': 'pydantic',
}


def _pip_install_modules(module_names):
    """Pip-install a list of import names, mapping to correct PyPI package names."""
    packages = [_IMPORT_TO_PYPI.get(m, m.replace('_', '-')) for m in module_names]
    if not packages:
        return
    log(f"[RUN] Auto-installing missing packages: {', '.join(packages)}")
    try:
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--quiet'] + packages,
            timeout=120, capture_output=True,
        )
    except Exception as exc:
        log(f"[RUN] pip install failed: {exc}")


def _module_is_available_for_project(module_name, project_dir):
    if not module_name:
        return True

    if module_name in getattr(sys, "stdlib_module_names", set()):
        return True

    module_file = os.path.join(project_dir, f"{module_name}.py")
    module_pkg_init = os.path.join(project_dir, module_name, "__init__.py")
    if os.path.isfile(module_file) or os.path.isfile(module_pkg_init):
        return True

    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _missing_import_modules(source, project_dir):
    pattern = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", flags=re.MULTILINE)
    missing = []
    seen = set()
    for module_name in pattern.findall(source or ""):
        module_name = module_name.strip()
        if not module_name or module_name in seen or module_name == "__future__":
            continue
        seen.add(module_name)
        if not _module_is_available_for_project(module_name, project_dir):
            missing.append(module_name)
    return missing


def _detect_runtime_shape_error(source, project_dir):
    text = source or ""

    missing_modules = _missing_import_modules(text, project_dir)
    if missing_modules:
        modules_text = ", ".join(missing_modules[:6])
        return f"Code imports missing modules: {modules_text}."

    if re.search(r"(^|\n)\s*(from|import)\s+src(\.|\b)", text):
        src_dir = os.path.join(project_dir, "src")
        if not os.path.isdir(src_dir):
            return "Code imports local package 'src' but no src/ folder exists in the project."

    if not _looks_like_web_app_source(text):
        return (
            "main.py does not appear to launch a web app. "
            "Test App expects a web server with a route like '/'."
        )

    return ""


def _escape_html(text):
    value = text or ""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_SMART_RESCUE_SYSTEM = """You generate complete single-file Python Flask web apps.
Rules:
- Output ONLY valid Python code, no markdown, no fences, no <think> tags, no prose.
- ALWAYS start the file with these exact imports (you may add more stdlib imports below):
    import os
    from flask import Flask, render_template_string, request, jsonify
- The file must define `app = Flask(__name__)` and end with `if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5600")))`.
- Define route GET '/' that renders an HTML page using render_template_string.
- Embed all HTML/CSS/JS inline (do NOT use templates/ folder).
- Use only stdlib + flask. Never import packages that aren't flask/stdlib.
- Every name you reference must be imported or defined — never use `os.`, `json.`, `re.`, etc. without importing it first.

INTERACTIVITY (CRITICAL — most generated apps fail here):
- The web UI must let the user actually USE the requested feature in the browser.
- EVERY <button>, <form>, and clickable element you put in the HTML MUST be wired to a working
  Flask route via fetch() or form submit. No dead buttons, no buttons that only show alerts.
- EVERY fetch() URL in the JavaScript MUST exactly match an @app.route path you defined in the
  same file. Same path, same HTTP method (GET/POST/etc.). Mismatched routes cause 404/405 errors.
- For any 'add/save/submit/create/update/delete' action implied by the prompt, you MUST include
  BOTH the UI control AND the matching mutating Flask route (POST/PUT/DELETE).
- Persist state in a module-level Python list/dict so user actions visibly take effect.
- After a mutating action, return the updated state as JSON so the JS can re-render the page.

If the request is ambiguous, build a minimal but functional interpretation.
Handle bad input gracefully — never crash. Never use Python or JS that throws on empty input."""


_STDLIB_AUTO_IMPORTS = ("os", "sys", "json", "re", "time", "math", "random",
                        "datetime", "uuid", "hashlib", "base64", "io", "csv",
                        "sqlite3", "urllib", "collections", "itertools", "functools")


def _ensure_required_imports(source):
    """Auto-prepend missing stdlib `import x` lines when the code references `x.`
    but never imports it. Catches LLMs that use os.environ without `import os`."""
    text = source or ""
    if not text.strip():
        return text

    missing = []
    for mod in _STDLIB_AUTO_IMPORTS:
        used_re = re.compile(rf"(?<![\w.]){re.escape(mod)}\s*\.")
        imported_re = re.compile(
            rf"^\s*(?:import\s+{re.escape(mod)}(?:\s|$|,)|from\s+{re.escape(mod)}\b)",
            re.MULTILINE,
        )
        if used_re.search(text) and not imported_re.search(text):
            missing.append(mod)

    if not missing:
        return text

    log(f"[RUN] Auto-adding missing stdlib imports: {', '.join(missing)}")
    prefix = "\n".join(f"import {m}" for m in missing) + "\n"
    return prefix + text


def _smart_rescue_via_llm(user_request, original_source):
    """Generate a fresh single-file Flask app from the user request via LLM.

    Falls back to the static rescue page if the model can't produce valid code.
    """
    request_hint = clamp_text(user_request, 600).strip() or "build a small web app"
    snippet = clamp_text(original_source or "", 1500).strip()
    user_prompt = (
        "Build a complete single-file Flask web app that fulfills this user request:\n"
        f"\"\"\"{request_hint}\"\"\"\n\n"
        + (
            f"Reference (the previously generated code, possibly broken — extract intent, do not copy verbatim):\n"
            f"```python\n{snippet}\n```\n\n"
            if snippet else ""
        )
        + "Output the full Python file now. Begin with imports."
    )
    try:
        resp = ollama.chat(
            model=MODELS.get("coder", "qwen2.5-coder:7b"),
            messages=[
                {"role": "system", "content": _SMART_RESCUE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            options={"num_predict": 2400, "temperature": 0.2},
            keep_alive="5m",
        )
        raw = (resp.get("message", {}).get("content", "") or "")
        candidate = extract_code(raw)
    except Exception as exc:
        log(f"[RUN] Smart rescue LLM call failed: {exc}")
        return None

    if not candidate.strip():
        return None

    candidate = _ensure_required_imports(candidate)

    if validate_generated_code(candidate, runtime_check=False):
        repair_ok, repaired, _ = try_repair_code(
            code=candidate,
            error_text=validate_generated_code(candidate, runtime_check=False) or "syntax error",
            context_note="Repair this rescue Flask web app so it runs.",
            attempts=2,
            runtime_check=False,
        )
        if repair_ok:
            candidate = repaired
        else:
            return None

    if not _looks_like_web_app_source(candidate):
        return None

    # Final binding check — does every fetch()/form action hit a real route?
    # If not, the UI buttons would silently 404. Try one quick repair pass; if
    # that still fails, return the candidate anyway (better than the static page)
    # but log a warning.
    try:
        from server_pipeline import _find_unrouted_calls  # local import to avoid cycle
        unrouted = _find_unrouted_calls(candidate)
        if unrouted:
            sample = ", ".join(f"{m} {u}" for u, m in unrouted[:3])
            log(f"[RUN] Smart rescue produced unrouted UI calls: {sample}. Trying one repair pass.")
            repair_ok, repaired, _ = try_repair_code(
                code=candidate,
                error_text=(
                    f"The generated app's UI calls these endpoints that have no matching @app.route: {sample}. "
                    "Add the missing routes (with the correct HTTP methods) so every fetch()/form action works."
                ),
                context_note="Wire UI buttons/forms to real Flask routes so nothing 404s.",
                attempts=1,
                runtime_check=False,
            )
            if repair_ok and _looks_like_web_app_source(repaired):
                candidate = _ensure_required_imports(repaired)
    except Exception as exc:
        log(f"[RUN] Smart rescue binding check skipped due to error: {exc}")

    log("[RUN] Smart rescue produced a fresh Flask app from user request.")
    return candidate


def _build_runtime_rescue_app(reason, user_request=""):
    safe_reason = _escape_html(clamp_text(reason, 320))
    safe_request = _escape_html(clamp_text(user_request or "(no request recorded)", 320))
    return f'''"""Auto-generated runtime rescue app."""

import os

from flask import Flask


app = Flask(__name__)


@app.route("/")
def home():
    return """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Recovered App</title>
    <style>
        body {{
            margin: 0;
            font-family: \"Segoe UI\", Tahoma, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
        }}
        .wrap {{
            max-width: 760px;
            margin: 36px auto;
            padding: 22px;
            border: 1px solid #334155;
            border-radius: 12px;
            background: #111827;
        }}
        h1 {{ margin-top: 0; }}
        p {{ line-height: 1.55; color: #cbd5e1; }}
        code {{ color: #67e8f9; }}
        pre {{
            background: #020617;
            color: #cbd5e1;
            border: 1px solid #1e293b;
            padding: 12px;
            border-radius: 8px;
            overflow-x: auto;
            white-space: pre-wrap;
        }}
    </style>
</head>
<body>
    <main class=\"wrap\">
        <h1>Recovered Test App</h1>
        <p>This generated build could not be launched directly, so AI Builder created a runnable rescue app.</p>
        <p>Original request: <code>{safe_request}</code></p>
        <p>Rebuild to get a full custom app. You can also edit <code>main.py</code> in this build folder.</p>
        <pre>{safe_reason}</pre>
    </main>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5600")))
'''


def _read_build_request_text(project_dir):
    meta_path = os.path.join(project_dir, "build_meta.json")
    if not os.path.exists(meta_path):
        return ""

    try:
        payload = json.loads(read_text(meta_path))
    except Exception:
        return ""

    return _collapse_whitespace(payload.get("request", ""))


def _is_calculator_request(request_text):
    lowered = (request_text or "").lower()
    return any(token in lowered for token in ["calculator", "calculate", "math app"])


def _calculator_feature_gap(source):
    text = source or ""
    lowered = text.lower()

    if not _looks_like_web_app_source(source):
        return "Calculator app must run as a web app with route '/'."

    root_routes = list(
        re.finditer(
            r"@app\.route\(\s*['\"]\/['\"]\s*(?:,\s*methods\s*=\s*\[([^\]]*)\])?",
            text,
            flags=re.IGNORECASE,
        )
    )
    if not root_routes:
        return "Calculator app must define route '/' so users can open it in the browser."

    root_allows_get = False
    for match in root_routes:
        methods_text = (match.group(1) or "").lower()
        if not methods_text or "get" in methods_text:
            root_allows_get = True
            break
    if not root_allows_get:
        return "Calculator app route '/' must allow GET so browser open works."

    has_operations = (
        any(op in lowered for op in [" + ", " - ", " * ", " / "])
        or any(word in lowered for word in ["add", "subtract", "multiply", "divide", "calculate"])
    )
    has_inputs = any(
        token in lowered
        for token in [
            "request.form",
            "request.args",
            "request.get_json",
            "type=\"number\"",
            "type='number'",
            "<input",
        ]
    )
    has_output = "result" in lowered or "answer" in lowered
    has_browser_ui = any(
        token in lowered
        for token in [
            "render_template",
            "render_template_string",
            "<!doctype html",
            "<input",
            "/api/calculate",
        ]
    )

    if not has_browser_ui:
        return "Calculator app needs a simple browser UI users can type into."

    if not (has_operations and has_inputs and has_output):
        return (
            "Calculator app is incomplete. It should accept numbers and an operator, "
            "perform + - * /, and show the result."
        )

    return ""


def _build_runtime_calculator_app():
    return '''"""Auto-generated calculator rescue app."""

import os

from flask import Flask, jsonify, render_template_string, request


app = Flask(__name__)

PAGE = """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Simple Calculator</title>
    <style>
        body {
            margin: 0;
            font-family: \"Segoe UI\", Tahoma, sans-serif;
            background: #f1f5f9;
            color: #0f172a;
        }
        .wrap {
            max-width: 520px;
            margin: 38px auto;
            padding: 20px;
            background: #ffffff;
            border: 1px solid #dbeafe;
            border-radius: 12px;
            box-shadow: 0 12px 28px rgba(15, 23, 42, 0.1);
        }
        h1 { margin-top: 0; font-size: 24px; }
        p { color: #334155; }
        .row {
            display: grid;
            grid-template-columns: 1fr 120px 1fr;
            gap: 10px;
            margin-bottom: 12px;
        }
        input, select, button {
            width: 100%;
            padding: 10px;
            border-radius: 8px;
            border: 1px solid #cbd5e1;
            font-size: 16px;
        }
        button {
            background: #0284c7;
            border: none;
            color: #ffffff;
            font-weight: 600;
            cursor: pointer;
        }
        #result {
            margin-top: 14px;
            padding: 10px;
            border-radius: 8px;
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            min-height: 24px;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <main class=\"wrap\">
        <h1>Calculator</h1>
        <p>Type two numbers, pick an operator, and press Calculate.</p>
        <div class=\"row\">
            <input id=\"a\" type=\"number\" step=\"any\" placeholder=\"First number\" />
            <select id=\"op\">
                <option value=\"+\">+</option>
                <option value=\"-\">-</option>
                <option value=\"*\">*</option>
                <option value=\"/\">/</option>
            </select>
            <input id=\"b\" type=\"number\" step=\"any\" placeholder=\"Second number\" />
        </div>
        <button id=\"calc\" type=\"button\">Calculate</button>
        <div id=\"result\">Result will appear here.</div>
    </main>
    <script>
        const resultEl = document.getElementById('result');
        const button = document.getElementById('calc');
        button.addEventListener('click', async () => {
            const a = Number(document.getElementById('a').value);
            const b = Number(document.getElementById('b').value);
            const op = document.getElementById('op').value;

            if (!Number.isFinite(a) || !Number.isFinite(b)) {
                resultEl.textContent = 'Please type valid numbers.';
                return;
            }

            try {
                const res = await fetch('/api/calculate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ a, b, op })
                });
                const data = await res.json();
                if (!res.ok) {
                    resultEl.textContent = data.error || 'Could not calculate.';
                    return;
                }
                resultEl.textContent = 'Result: ' + data.result;
            } catch {
                resultEl.textContent = 'Network error. Try again.';
            }
        });
    </script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    payload = request.get_json(silent=True) or {}
    op = str(payload.get("op", "+")).strip()
    try:
        a = float(payload.get("a", 0))
        b = float(payload.get("b", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Please provide valid numbers."}), 400

    if op == "+":
        result = a + b
    elif op == "-":
        result = a - b
    elif op == "*":
        result = a * b
    elif op == "/":
        if b == 0:
            return jsonify({"error": "You cannot divide by zero."}), 400
        result = a / b
    else:
        return jsonify({"error": "Choose one of +, -, *, /."}), 400

    return jsonify({"result": result})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5600")))
'''


def _infer_app_label(request_text, source):
    lowered_req = (request_text or "").lower()
    lowered_code = (source or "").lower()

    if _is_calculator_request(request_text) or "calculate" in lowered_code:
        return "calculator app"
    if "todo" in lowered_req or "/todos" in lowered_code:
        return "todo app"
    if "chat" in lowered_req:
        return "chat app"
    if "api" in lowered_req or "jsonify" in lowered_code:
        return "web api app"
    return "web app"


def _build_simple_run_summary(app_label, run_mode):
    if run_mode == "calculator_rescue":
        return "I started a working calculator app. Type 2 numbers, pick + - * /, then press Calculate."
    if run_mode == "rescue":
        return (
            "I started a temporary app page because your generated code was incomplete. "
            "Rebuild to get the full app."
        )
    return f"I started your {app_label}."


def _sanitize_requirements(requirements_text):
    valid_lines = []
    dropped_lines = []
    seen = set()

    for raw_line in (requirements_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith(("-r", "--requirement", "--index-url", "--extra-index-url")):
            dropped_lines.append(line)
            continue

        candidate = line.split(" #", 1)[0].strip()
        lowered = candidate.lower()
        if lowered.startswith(("-e ", "--editable", ".", "..", "file:", "git+", "http://", "https://")):
            dropped_lines.append(line)
            continue

        if "\\" in candidate or "/" in candidate:
            dropped_lines.append(line)
            continue

        base_name = re.split(r"[<>=!~\[; ]", candidate, maxsplit=1)[0].strip().lower()
        if not base_name or not _REQUIREMENT_NAME_RE.match(base_name):
            dropped_lines.append(line)
            continue

        if base_name in _IGNORE_REQUIREMENT_NAMES:
            dropped_lines.append(line)
            continue

        if candidate not in seen:
            valid_lines.append(candidate)
            seen.add(candidate)

    return valid_lines, dropped_lines


def _install_requirements_for_project(project_dir, requirements_file, requirements_text):
    valid_reqs, dropped_reqs = _sanitize_requirements(requirements_text)

    if dropped_reqs:
        log(f"[RUN] Dropped invalid requirements: {', '.join(dropped_reqs[:6])}")

    sanitized_text = "\n".join(valid_reqs) + ("\n" if valid_reqs else "")
    try:
        with open(requirements_file, "w", encoding="utf-8") as handle:
            handle.write(sanitized_text)
    except Exception as exc:
        return False, f"Could not rewrite requirements.txt: {exc}", dropped_reqs

    if not valid_reqs:
        return True, "", dropped_reqs

    log("[RUN] Installing dependencies from sanitized requirements.txt...")
    install_proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", requirements_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=project_dir,
    )
    if install_proc.returncode != 0:
        return False, clamp_text(install_proc.stdout or "", 1200), dropped_reqs

    return True, "", dropped_reqs


def _collapse_whitespace(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _iter_related_topic_entries(raw_related_topics):
    for item in raw_related_topics:
        if not isinstance(item, dict):
            continue

        if item.get("Text"):
            yield item

        nested = item.get("Topics")
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, dict) and child.get("Text"):
                    yield child


def _fetch_web_search_context(question):
    query = _collapse_whitespace(question)
    if len(query) < 4:
        return ""

    if query.lower() in {"hi", "hello", "hey", "yo"} or _SMALLTALK_RE.match(query):
        return ""

    url = (
        "https://api.duckduckgo.com/?"
        f"q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
    )

    try:
        request_obj = Request(url, headers={"User-Agent": "AI-Builder NPC Assistant/1.0"})
        with urlopen(request_obj, timeout=WEB_SEARCH_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (URLError, OSError, ValueError) as exc:
        log(f"[NPC] Web lookup unavailable: {exc}")
        return ""

    snippets = []
    for field in ("Answer", "AbstractText", "Definition"):
        candidate = _collapse_whitespace(payload.get(field, ""))
        if candidate and candidate not in snippets:
            snippets.append(candidate)
        if len(snippets) >= WEB_SEARCH_MAX_SNIPPETS:
            break

    if len(snippets) < WEB_SEARCH_MAX_SNIPPETS:
        related_topics = payload.get("RelatedTopics") or []
        for entry in _iter_related_topic_entries(related_topics):
            text = _collapse_whitespace(entry.get("Text", ""))
            if not text:
                continue

            source_url = _collapse_whitespace(entry.get("FirstURL", ""))
            with_source = f"{text} (source: {source_url})" if source_url else text
            if with_source not in snippets:
                snippets.append(with_source)
            if len(snippets) >= WEB_SEARCH_MAX_SNIPPETS:
                break

    if not snippets:
        return ""

    return clamp_text(
        "Internet snippets (can be imperfect):\n"
        + "\n".join(f"- {item}" for item in snippets[:WEB_SEARCH_MAX_SNIPPETS]),
        1300,
    )


def _build_npc_user_prompt(question, app_context="", web_context=""):
    sections = [
        "Style rules:\n"
        "- Speak in first person: use I/me/my.\n"
        "- Talk directly to the player as you.\n"
        "- Never say 'the user' or mention prompt/context blocks.\n"
        "- Keep it to 1-2 short sentences.",
        f"User question: {question}",
    ]

    if app_context:
        sections.append(
            "Known app context (prefer this when it answers the question):\n"
            f"{app_context}"
        )

    if web_context:
        sections.append(
            "Optional web context (use only if helpful and note uncertainty if needed):\n"
            f"{web_context}"
        )

    return "\n\n".join(sections)


def _extract_first_web_fact(web_context):
    for line in (web_context or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("internet snippets"):
            continue
        if stripped.startswith("-"):
            stripped = stripped[1:].strip()
        if " (source:" in stripped:
            stripped = stripped.split(" (source:", 1)[0].strip()
        if stripped:
            return stripped
    return ""


def _looks_like_no_answer(reply):
    lowered = (reply or "").lower()
    return any(marker in lowered for marker in _NO_ANSWER_MARKERS)


def _fallback_first_person_prefix(signature):
    sig = (signature or "").strip().lower()
    if sig.startswith("bug clue"):
        return "I spot this clue: "
    if sig.startswith("code spark"):
        return "I can code this fast: "
    if sig.startswith("blueprint check"):
        return "I see the plan like this: "
    if sig.startswith("test radar"):
        return "I would test this first: "
    if sig.startswith("review note"):
        return "I would review it like this: "
    if sig.startswith("orbit says"):
        return "I can explain it like this: "
    return "I think "


def _enforce_first_person_voice(text, signature=""):
    body = _collapse_whitespace(text)

    body = re.sub(r"\bthe\s+user\b", "you", body, flags=re.IGNORECASE)
    body = re.sub(r"\bthis\s+user\b", "you", body, flags=re.IGNORECASE)
    body = re.sub(r"\bthe\s+person\b", "you", body, flags=re.IGNORECASE)
    body = re.sub(r"\buser\b", "you", body, flags=re.IGNORECASE)

    if body.lower().startswith("here's"):
        body = "Here is" + body[6:]

    has_first_person = re.search(r"\b(i|i'm|i’d|i'll|i’ve|me|my|mine)\b", body, flags=re.IGNORECASE)
    if not has_first_person:
        prefix = _fallback_first_person_prefix(signature)
        lowered = body.lower()
        if lowered.startswith("you "):
            body = f"{prefix}{body[0].lower()}{body[1:]}"
        else:
            body = f"{prefix}{body}"

    return _collapse_whitespace(body)


def _strip_leading_signature(text, signature):
    sig = (signature or "").strip()
    body = (text or "").strip()
    if not sig or not body:
        return body

    pattern = re.compile(rf"^(?:{re.escape(sig)}\s*)+", flags=re.IGNORECASE)
    return pattern.sub("", body).strip()


def _trim_reply_text(text, max_chars):
    candidate = (text or "").strip()
    if len(candidate) <= max_chars:
        return candidate

    clipped = candidate[:max_chars].rstrip()
    last_punc = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
    if last_punc >= max(28, max_chars // 3):
        return clipped[: last_punc + 1].strip()

    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    if clipped and clipped[-1] not in ".!?":
        clipped += "."
    return clipped


def _format_npc_reply(raw_reply, signature=""):
    cleaned = (raw_reply or "").replace("```", " ").replace("`", " ")
    cleaned = cleaned.replace("\r", "\n")

    lines = []
    for line in cleaned.split("\n"):
        compact = line.strip()
        if not compact:
            continue
        compact = re.sub(r"^[-*]\s+", "", compact)
        compact = re.sub(r"^\d+\.\s+", "", compact)
        lines.append(compact)

    compact_reply = _collapse_whitespace(" ".join(lines))
    compact_reply = _strip_leading_signature(compact_reply, signature)
    compact_reply = _enforce_first_person_voice(compact_reply, signature=signature)
    if not compact_reply:
        compact_reply = "I do not know yet."

    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(compact_reply) if part.strip()]
    if not sentences:
        sentences = [compact_reply]

    short_reply = _collapse_whitespace(" ".join(sentences[:NPC_MAX_REPLY_SENTENCES]))
    if len(short_reply) > NPC_MAX_REPLY_CHARS:
        short_reply = _trim_reply_text(short_reply, NPC_MAX_REPLY_CHARS)

    if short_reply and short_reply[-1] not in ".!?":
        short_reply += "."

    signature = (signature or "").strip()
    if signature and not short_reply.lower().startswith(signature.lower()):
        short_reply = f"{signature} {short_reply}"

    if len(short_reply) > NPC_MAX_REPLY_CHARS:
        short_reply = _trim_reply_text(short_reply, NPC_MAX_REPLY_CHARS)

    return short_reply


@atexit.register
def _shutdown_runtime_process_on_exit():
    with _runtime_lock:
        _stop_runtime_locked()


@app.route('/')
@app.route('/office')
@app.route('/office.html')
def index():
    response = make_response(send_file(os.path.join(BASE_DIR, 'office.html')))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/physics.js')
def serve_physics_js():
    return send_file(os.path.join(BASE_DIR, 'physics.js'), mimetype='application/javascript')


@app.route('/minigames.js')
def serve_minigames_js():
    return send_file(os.path.join(BASE_DIR, 'minigames.js'), mimetype='application/javascript')


_ASSET_MIMETYPES = {
    '.glb': 'model/gltf-binary',
    '.gltf': 'model/gltf+json',
    '.bin': 'application/octet-stream',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.txt': 'text/plain; charset=utf-8',
}


@app.route('/assets/<path:filepath>')
def serve_asset_file(filepath):
    """Serve files from the assets/ folder so the Three.js GLTFLoader can fetch
    GLB models, textures, and animations from the same origin as office.html."""
    assets_root = os.path.join(BASE_DIR, 'assets')
    full_path = os.path.normpath(os.path.join(assets_root, filepath))
    # Reject any path that escapes the assets/ folder.
    if not full_path.startswith(os.path.normpath(assets_root) + os.sep):
        return ('Forbidden', 403)
    if not os.path.isfile(full_path):
        return ('Not Found', 404)
    ext = os.path.splitext(full_path)[1].lower()
    mime = _ASSET_MIMETYPES.get(ext, 'application/octet-stream')
    return send_file(full_path, mimetype=mime)


@sock.route('/ws')
def websocket(ws):
    clients.append(ws)
    ws.send(json.dumps({"event": "init", "data": build_state}))
    try:
        while True:
            msg = ws.receive(timeout=30)
            if msg is None:
                break
    except Exception:
        pass
    finally:
        if ws in clients:
            clients.remove(ws)


@app.route('/build', methods=['POST'])
def build():
    data = request.json
    user_request = data.get('request', '')
    if not user_request:
        return jsonify({"error": "No request provided"}), 400

    # Scope filter: keep the pipeline within its sweet spot (single-purpose
    # Flask apps, ≤3 routes, in-memory state). See classify_request_scope().
    verdict, processed_request, note = classify_request_scope(user_request)

    if verdict == "reject":
        log(f"[SCOPE] Rejected request: {note}")
        return jsonify({
            "error": note,
            "scope": "reject",
            "original": user_request,
        }), 400

    reset_build_state_for_new_request()

    if verdict == "narrow":
        log(f"[SCOPE] {note}")
        # Tell the office UI we narrowed the scope so the user knows what we
        # actually committed to building. Fire-and-forget — the pipeline will
        # also broadcast its own build_start moments later.
        broadcast("scope_notice", {
            "kind": "narrow",
            "message": note,
            "original": user_request,
        })

    t = threading.Thread(target=build_pipeline, args=(processed_request,))
    t.daemon = True
    t.start()
    return jsonify({
        "status": "started",
        "scope": verdict,
        "note": note,
    })


@app.route('/refine', methods=['POST'])
def refine():
    """Apply a follow-up prompt to the latest build.

    The user submits either an enhancement ("add a search box") or a fix
    ("the delete button does nothing"). We load the previous project's main.py,
    splice it into a composite prompt, and run it through the normal build
    pipeline so it goes through smoke tests + polish like any fresh build.
    """
    data = request.json or {}
    refinement = (data.get('prompt') or '').strip()
    mode = (data.get('mode') or 'enhance').strip().lower()  # 'fix' | 'enhance'
    if not refinement:
        return jsonify({"error": "No refinement prompt provided"}), 400
    if len(refinement) < 4:
        return jsonify({"error": "Refinement prompt is too short."}), 400

    project_dir = get_latest_project_dir()
    if not (project_dir and os.path.exists(os.path.join(project_dir, "main.py"))):
        return jsonify({"error": "No previous build to refine. Build something first."}), 400

    try:
        existing_code = read_text(os.path.join(project_dir, "main.py"))
    except Exception as exc:
        return jsonify({"error": f"Could not read previous build: {exc}"}), 500

    # Read the original request so the architect/coder still has the user's
    # high-level intent to work from when applying the refinement.
    original_request = _read_build_request_text(project_dir) or "(unknown)"

    intent_label = "FIX" if mode == "fix" else "ENHANCE"
    composite_request = (
        f"[{intent_label} REQUEST — iterating on a previous build, do NOT start from scratch]\n\n"
        f"Original goal: {original_request}\n\n"
        f"User's follow-up: {refinement}\n\n"
        "Apply the follow-up to the existing code below. Preserve everything else "
        "that already works. Output the full updated single-file Flask app.\n\n"
        f"Existing code (latest version):\n```python\n{existing_code}\n```"
    )

    reset_build_state_for_new_request()
    log(f"[REFINE] mode={mode}, prompt={refinement[:80]!r}")
    broadcast("refine_notice", {
        "mode": mode,
        "message": f"Applying {mode}: {refinement[:80]}",
    })

    t = threading.Thread(target=build_pipeline, args=(composite_request,))
    t.daemon = True
    t.start()
    return jsonify({"status": "started", "mode": mode})


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@app.route('/run/stream', methods=['POST'])
def run_app_stream():
    """SSE endpoint: streams launch progress steps so the frontend can show a progress bar."""

    # Capture request-context values before entering the generator (which runs outside context).
    _captured_app_host = _public_runtime_host_for_request(request)

    def generate():
        nonlocal _captured_app_host
        run_mode = "generated"

        # ── Step 1: resolve project dir ──────────────────────────────────
        yield _sse_event({"step": "resolve", "pct": 5, "msg": "Finding latest build..."})
        project_dir = get_latest_project_dir()
        if not (project_dir and os.path.exists(os.path.join(project_dir, "main.py"))):
            legacy_file = get_latest_built_file()
            if not legacy_file:
                yield _sse_event({"step": "error", "pct": 0, "msg": "No built app found. Build something first."})
                return

            yield _sse_event({"step": "resolve", "pct": 10, "msg": "Converting legacy build..."})
            try:
                legacy_source = read_text(legacy_file)
            except Exception as exc:
                yield _sse_event({"step": "error", "pct": 0, "msg": f"Failed to read legacy build: {exc}"})
                return

            compile_error = validate_generated_code(legacy_source, runtime_check=False)
            if compile_error:
                repair_ok, repaired_code, repair_error = try_repair_code(
                    code=legacy_source, error_text=compile_error,
                    context_note="Repair legacy build before converting.",
                    attempts=MAX_AUTO_REPAIR_ATTEMPTS, runtime_check=False,
                )
                if repair_ok:
                    legacy_source = repaired_code
                else:
                    yield _sse_event({"step": "error", "pct": 0, "msg": f"Legacy build invalid: {clamp_text(repair_error or compile_error, 300)}"})
                    return

            bundle = create_project_bundle(
                code=legacy_source, tests="", plan="Legacy conversion",
                review="Converted for run mode.", user_request="legacy-conversion",
            )
            project_dir = bundle["project_dir"]

        request_text = _read_build_request_text(project_dir)
        main_file = os.path.join(project_dir, "main.py")
        requirements_file = os.path.join(project_dir, "requirements.txt")

        # ── Step 2: validate / auto-repair code ──────────────────────────
        yield _sse_event({"step": "validate", "pct": 20, "msg": "Validating code..."})
        try:
            source = read_text(main_file)
        except Exception as exc:
            yield _sse_event({"step": "error", "pct": 0, "msg": f"Failed to read main.py: {exc}"})
            return

        compile_error = validate_generated_code(source, runtime_check=False)
        if compile_error:
            yield _sse_event({"step": "validate", "pct": 25, "msg": "Repairing code..."})
            repair_ok, repaired_code, repair_error = try_repair_code(
                code=source, error_text=compile_error,
                context_note="Repair invalid project main.py before launching.",
                attempts=MAX_AUTO_REPAIR_ATTEMPTS, runtime_check=False,
            )
            if repair_ok:
                saved = save_repaired_project_main(project_dir, repaired_code)
                source = repaired_code
                main_file = saved["main_file"]
            else:
                yield _sse_event({"step": "error", "pct": 0, "msg": f"Code invalid and repair failed: {clamp_text(repair_error or compile_error, 300)}"})
                return

        if _is_calculator_request(request_text):
            calc_gap = _calculator_feature_gap(source)
            if calc_gap:
                yield _sse_event({"step": "validate", "pct": 30, "msg": "Fixing calculator app..."})
                repair_ok, repaired_code, repair_error = try_repair_code(
                    code=source, error_text=calc_gap,
                    context_note="Repair into a working calculator web app.",
                    attempts=MAX_AUTO_REPAIR_ATTEMPTS, runtime_check=False,
                )
                if repair_ok and not _calculator_feature_gap(repaired_code):
                    saved = save_repaired_project_main(project_dir, repaired_code)
                    source = repaired_code
                    main_file = saved["main_file"]
                    run_mode = "calculator_repaired"
                else:
                    rescue_code = _build_runtime_calculator_app()
                    saved = save_repaired_project_main(project_dir, rescue_code)
                    source = rescue_code
                    main_file = saved["main_file"]
                    run_mode = "calculator_rescue"

        # Auto-install any missing imports before shape validation so they don't
        # incorrectly trigger rescue mode.
        pre_missing = _missing_import_modules(source, project_dir)
        if pre_missing:
            yield _sse_event({"step": "deps", "pct": 38, "msg": f"Installing: {', '.join(pre_missing[:5])}..."})
            _pip_install_modules(pre_missing)

        shape_error = _detect_runtime_shape_error(source, project_dir)
        if shape_error:
            yield _sse_event({"step": "validate", "pct": 42, "msg": "Adapting to web app..."})
            repair_ok, repaired_code, repair_error = try_repair_code(
                code=source, error_text=shape_error,
                context_note=(
                    f"Convert into a runnable single-file Flask web app for the user request: "
                    f"\"{clamp_text(request_text, 400)}\". Define route GET '/' that renders an HTML UI "
                    f"using render_template_string, and end with app.run() reading PORT env var."
                ),
                attempts=MAX_AUTO_REPAIR_ATTEMPTS, runtime_check=False,
            )
            if repair_ok and not _detect_runtime_shape_error(repaired_code, project_dir):
                saved = save_repaired_project_main(project_dir, repaired_code)
                source = repaired_code
                main_file = saved["main_file"]
            else:
                yield _sse_event({"step": "validate", "pct": 48, "msg": "Generating fresh web app from prompt..."})
                smart_code = _smart_rescue_via_llm(request_text, source)
                if smart_code and not _detect_runtime_shape_error(smart_code, project_dir):
                    saved = save_repaired_project_main(project_dir, smart_code)
                    source = smart_code
                    main_file = saved["main_file"]
                    run_mode = "smart_rescue"
                else:
                    rescue_code = _build_runtime_rescue_app(repair_error or shape_error, request_text)
                    saved = save_repaired_project_main(project_dir, rescue_code)
                    source = rescue_code
                    main_file = saved["main_file"]
                    run_mode = "rescue"

        # Apps that call render_template('foo.html') need a templates/ file whose
        # fetch() calls match the Flask routes exactly. The static template generator
        # cannot guarantee that alignment for arbitrary prompts, so rewrite as a
        # single-file render_template_string app where the HTML/JS and routes are
        # generated together by the same LLM call.
        if _uses_external_templates(source):
            yield _sse_event({"step": "validate", "pct": 55,
                              "msg": "Inlining UI to match routes..."})
            inlined = _smart_rescue_via_llm(request_text, source)
            if inlined and not _detect_runtime_shape_error(inlined, project_dir):
                saved = save_repaired_project_main(project_dir, inlined)
                source = inlined
                main_file = saved["main_file"]
                if run_mode == "generated":
                    run_mode = "inlined"
                # New source might bring new imports — install them now.
                inline_missing = _missing_import_modules(source, project_dir)
                if inline_missing:
                    _pip_install_modules(inline_missing)

        # Final stdlib-import safety net: catch LLM omissions like using
        # os.environ without `import os`. NameError at startup would otherwise
        # kill the launch ("App did not start in time. NameError: name 'os' ...").
        fixed_source = _ensure_required_imports(source)
        if fixed_source != source:
            saved = save_repaired_project_main(project_dir, fixed_source)
            source = fixed_source
            main_file = saved["main_file"]

        _ensure_index_template_for_flask(project_dir, source)

        # ── Step 3: install dependencies ─────────────────────────────────
        requirements_text = ""
        if os.path.exists(requirements_file):
            try:
                requirements_text = read_text(requirements_file).strip()
            except Exception:
                requirements_text = ""

        if requirements_text:
            yield _sse_event({"step": "deps", "pct": 50, "msg": "Installing dependencies..."})
            install_ok, install_details, dropped_reqs = _install_requirements_for_project(
                project_dir=project_dir,
                requirements_file=requirements_file,
                requirements_text=requirements_text,
            )
            if not install_ok:
                first_line = (install_details or "").split("\n")[0][:200]
                yield _sse_event({"step": "error", "pct": 0, "msg": f"Dependency install failed: {first_line}"})
                return

        # ── Step 4: find port and launch ─────────────────────────────────
        with _runtime_lock:
            existing_proc = _runtime.get("process")
            if (
                _is_process_running(existing_proc)
                and _runtime.get("project_dir") == project_dir
                and _runtime.get("url")
            ):
                yield _sse_event({"step": "ready", "pct": 100,
                                   "msg": f"App already running at {_runtime['url']}",
                                   "url": _runtime["url"]})
                return
            if _is_process_running(existing_proc):
                _stop_runtime_locked()

        yield _sse_event({"step": "starting", "pct": 65, "msg": "Starting server..."})
        port = _find_free_port(APP_RUNTIME_LOCAL_HEALTH_HOST, APP_RUNTIME_PORT_START, APP_RUNTIME_PORT_END)
        if port is None:
            yield _sse_event({"step": "error", "pct": 0, "msg": "No free port available."})
            return

        log_path = os.path.join(project_dir, "runtime.log")
        try:
            proc = _start_runtime_process(
                project_dir=project_dir, main_file=main_file,
                bind_host=APP_RUNTIME_BIND_HOST, port=port, log_path=log_path,
            )
        except Exception as exc:
            yield _sse_event({"step": "error", "pct": 0, "msg": f"Failed to launch: {exc}"})
            return

        # ── Step 5: wait for HTTP port ────────────────────────────────────
        yield _sse_event({"step": "starting", "pct": 80, "msg": "Waiting for app to respond..."})
        if not _wait_for_http_port(APP_RUNTIME_LOCAL_HEALTH_HOST, port, APP_RUNTIME_START_TIMEOUT_SECONDS):
            exit_code = proc.poll()
            if _is_process_running(proc):
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            detail = _read_log_tail(log_path).strip().split("\n")[-1][:200] if log_path else ""
            yield _sse_event({"step": "error", "pct": 0,
                               "msg": f"App did not start in time (exit {exit_code}). {detail}"})
            return

        if not _is_process_running(proc):
            detail = _read_log_tail(log_path).strip().split("\n")[-1][:200] if log_path else ""
            yield _sse_event({"step": "error", "pct": 0, "msg": f"App exited right after startup. {detail}"})
            return

        # ── Done ──────────────────────────────────────────────────────────
        app_host = _captured_app_host
        app_url = f"http://{app_host}:{port}/"
        app_label = _infer_app_label(request_text, source)
        summary = _build_simple_run_summary(app_label, run_mode)

        with _runtime_lock:
            _runtime["process"] = proc
            _runtime["project_dir"] = project_dir
            _runtime["port"] = port
            _runtime["url"] = app_url
            _runtime["log_path"] = log_path
            _runtime["mode"] = run_mode
            _runtime["summary"] = summary
            _runtime["app_label"] = app_label

        log(f"[RUN] App launched at {app_url}")
        yield _sse_event({"step": "ready", "pct": 100, "msg": summary, "url": app_url,
                           "mode": run_mode, "app_label": app_label})

    resp = make_response(generate())
    resp.headers['Content-Type'] = 'text/event-stream'
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/run', methods=['POST'])
def run_app():
    run_mode = "generated"
    project_dir = get_latest_project_dir()
    if not (project_dir and os.path.exists(os.path.join(project_dir, "main.py"))):
        legacy_file = get_latest_built_file()
        if not legacy_file:
            return jsonify({"error": "No built app found. Build something first."}), 404

        try:
            legacy_source = read_text(legacy_file)
        except Exception as e:
            return jsonify({"error": f"Failed to read legacy built app: {str(e)}"}), 500

        compile_error = validate_generated_code(legacy_source, runtime_check=False)
        if compile_error:
            log("[RUN] Legacy build is invalid. Attempting auto-repair before conversion.")
            repair_ok, repaired_code, repair_error = try_repair_code(
                code=legacy_source,
                error_text=compile_error,
                context_note="Repair legacy single-file build before converting to project folder.",
                attempts=MAX_AUTO_REPAIR_ATTEMPTS,
                runtime_check=False,
            )
            if repair_ok:
                legacy_source = repaired_code
            else:
                return jsonify({
                    "error": (
                        "Legacy built app is invalid and could not be auto-repaired. "
                        f"Details: {clamp_text(repair_error or compile_error, 500)}"
                    )
                }), 400

        bundle = create_project_bundle(
            code=legacy_source,
            tests="",
            plan="Legacy fallback conversion",
            review="Converted from legacy single-file build for project run mode.",
            user_request="legacy-conversion",
        )
        project_dir = bundle["project_dir"]
        log(f"[RUN] Converted legacy build to project folder: {project_dir}")

    request_text = _read_build_request_text(project_dir)

    main_file = os.path.join(project_dir, "main.py")
    requirements_file = os.path.join(project_dir, "requirements.txt")

    try:
        source = read_text(main_file)
    except Exception as e:
        return jsonify({"error": f"Failed to read main.py: {str(e)}"}), 500

    compile_error = validate_generated_code(source, runtime_check=False)
    if compile_error:
        log("[RUN] Latest main.py is invalid. Attempting auto-repair before run.")
        repair_ok, repaired_code, repair_error = try_repair_code(
            code=source,
            error_text=compile_error,
            context_note="Repair invalid generated project entrypoint main.py before launching.",
            attempts=MAX_AUTO_REPAIR_ATTEMPTS,
            runtime_check=False,
        )

        if repair_ok:
            saved = save_repaired_project_main(project_dir, repaired_code)
            source = repaired_code
            main_file = saved["main_file"]
            log(f"[RUN] Auto-repair succeeded. Updated main.py and zip at {saved['zip_path']}")
        else:
            return jsonify({
                "error": (
                    "Latest project main.py is not valid Python, and auto-repair failed. "
                    f"Details: {clamp_text(repair_error or compile_error, 500)}"
                )
            }), 400

    if _is_calculator_request(request_text):
        calc_gap = _calculator_feature_gap(source)
        if calc_gap:
            log(f"[RUN] Calculator gap detected: {calc_gap}")
            repair_ok, repaired_code, repair_error = try_repair_code(
                code=source,
                error_text=calc_gap,
                context_note=(
                    "Repair app into a fully working calculator web app. "
                    "Must support + - * / with two numeric inputs and result display."
                ),
                attempts=MAX_AUTO_REPAIR_ATTEMPTS,
                runtime_check=False,
            )

            if repair_ok and not _calculator_feature_gap(repaired_code):
                saved = save_repaired_project_main(project_dir, repaired_code)
                source = repaired_code
                main_file = saved["main_file"]
                run_mode = "calculator_repaired"
                log(f"[RUN] Calculator repair succeeded. Updated main.py and zip at {saved['zip_path']}")
            else:
                rescue_code = _build_runtime_calculator_app()
                saved = save_repaired_project_main(project_dir, rescue_code)
                source = rescue_code
                main_file = saved["main_file"]
                run_mode = "calculator_rescue"
                log(
                    "[RUN] Calculator repair failed. "
                    f"Saved deterministic calculator app at {saved['main_file']}. "
                    f"Reason: {clamp_text(repair_error or calc_gap, 220)}"
                )

    shape_error = _detect_runtime_shape_error(source, project_dir)
    if shape_error:
        log("[RUN] main.py shape check failed. Attempting web-app repair before launch.")
        repair_ok, repaired_code, repair_error = try_repair_code(
            code=source,
            error_text=shape_error,
            context_note=(
                "Repair generated main.py so Test App can run it as a web app. "
                "Requirements: single-file Flask app, route '/', and runnable entrypoint. "
                "Do not import local packages like src.* unless they exist in this project."
            ),
            attempts=MAX_AUTO_REPAIR_ATTEMPTS,
            runtime_check=False,
        )

        if repair_ok:
            repaired_shape_error = _detect_runtime_shape_error(repaired_code, project_dir)
            if repaired_shape_error:
                repair_ok = False
                repair_error = (
                    "Auto-repair returned code that still is not runnable as a web app. "
                    f"{repaired_shape_error}"
                )

        if repair_ok:
            saved = save_repaired_project_main(project_dir, repaired_code)
            source = repaired_code
            main_file = saved["main_file"]
            log(f"[RUN] Web-app repair succeeded. Updated main.py and zip at {saved['zip_path']}")
        else:
            rescue_reason = repair_error or shape_error
            rescue_code = _build_runtime_rescue_app(rescue_reason)
            saved = save_repaired_project_main(project_dir, rescue_code)
            source = rescue_code
            main_file = saved["main_file"]
            run_mode = "rescue"
            log(
                "[RUN] Web-app repair failed. "
                f"Saved runtime rescue app at {saved['main_file']}"
            )

    # Some generated Flask apps call render_template('index.html') but omit the file.
    # Create/copy a template so / can render instead of failing with TemplateNotFound.
    _ensure_index_template_for_flask(project_dir, source)

    requirements_text = ""
    if os.path.exists(requirements_file):
        try:
            requirements_text = read_text(requirements_file).strip()
        except Exception:
            requirements_text = ""

    with _runtime_lock:
        existing_proc = _runtime.get("process")
        if (
            _is_process_running(existing_proc)
            and _runtime.get("project_dir") == project_dir
            and _runtime.get("url")
        ):
            return jsonify(
                {
                    "status": "already_running",
                    "url": _runtime["url"],
                    "port": _runtime["port"],
                    "project_dir": os.path.basename(project_dir),
                    "mode": _runtime.get("mode") or "generated",
                    "summary": _runtime.get("summary") or "I started your app.",
                    "app_label": _runtime.get("app_label") or "web app",
                }
            )

        if _is_process_running(existing_proc):
            _stop_runtime_locked()

    if requirements_text:
        install_ok, install_details, dropped_reqs = _install_requirements_for_project(
            project_dir=project_dir,
            requirements_file=requirements_file,
            requirements_text=requirements_text,
        )
        if not install_ok:
            return jsonify(
                {
                    "error": "Dependency installation failed before launch.",
                    "details": install_details,
                    "dropped_requirements": dropped_reqs,
                }
            ), 500

    port = _find_free_port(APP_RUNTIME_LOCAL_HEALTH_HOST, APP_RUNTIME_PORT_START, APP_RUNTIME_PORT_END)
    if port is None:
        return jsonify(
            {
                "error": (
                    f"No free runtime port found in range {APP_RUNTIME_PORT_START}-{APP_RUNTIME_PORT_END}."
                )
            }
        ), 500

    log_path = os.path.join(project_dir, "runtime.log")
    try:
        proc = _start_runtime_process(
            project_dir=project_dir,
            main_file=main_file,
            bind_host=APP_RUNTIME_BIND_HOST,
            port=port,
            log_path=log_path,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to launch app process: {str(e)}"}), 500

    if not _wait_for_http_port(
        APP_RUNTIME_LOCAL_HEALTH_HOST,
        port,
        APP_RUNTIME_START_TIMEOUT_SECONDS,
    ):
        exit_code = proc.poll()
        if _is_process_running(proc):
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

        return jsonify(
            {
                "error": (
                    "App did not start an HTTP server in time. "
                    "Ensure generated main.py actually serves a web app."
                ),
                "exit_code": exit_code,
                "details": _read_log_tail(log_path),
            }
        ), 500

    # Even if the TCP port opens briefly, ensure the launched process still lives.
    if not _is_process_running(proc):
        return jsonify(
            {
                "error": "App process exited right after startup.",
                "exit_code": proc.poll(),
                "details": _read_log_tail(log_path),
            }
        ), 500

    app_host = _public_runtime_host_for_request(request)
    app_url = f"http://{app_host}:{port}/"
    app_label = _infer_app_label(request_text, source)
    summary = _build_simple_run_summary(app_label, run_mode)

    with _runtime_lock:
        _runtime["process"] = proc
        _runtime["project_dir"] = project_dir
        _runtime["port"] = port
        _runtime["url"] = app_url
        _runtime["log_path"] = log_path
        _runtime["mode"] = run_mode
        _runtime["summary"] = summary
        _runtime["app_label"] = app_label

    log(f"[RUN] App launched at {app_url}")
    return jsonify(
        {
            "status": "running",
            "url": app_url,
            "port": port,
            "project_dir": os.path.basename(project_dir),
            "mode": run_mode,
            "summary": summary,
            "app_label": app_label,
        }
    )


@app.route('/run/status')
def run_status():
    with _runtime_lock:
        proc = _runtime.get("process")
        running = _is_process_running(proc)
        if not running:
            _clear_runtime_state_locked()
            return jsonify({"running": False})

        return jsonify(
            {
                "running": True,
                "url": _runtime.get("url"),
                "port": _runtime.get("port"),
                "project_dir": os.path.basename(_runtime.get("project_dir") or ""),
                "mode": _runtime.get("mode") or "generated",
                "summary": _runtime.get("summary") or "I started your app.",
                "app_label": _runtime.get("app_label") or "web app",
            }
        )


@app.route('/run/stop', methods=['POST'])
def run_stop():
    with _runtime_lock:
        proc = _runtime.get("process")
        if not _is_process_running(proc):
            _clear_runtime_state_locked()
            return jsonify({"status": "already_stopped"})

        _stop_runtime_locked()

    return jsonify({"status": "stopped"})


@app.route('/download')
def download():
    project_dir = get_latest_project_dir()
    if project_dir and os.path.exists(os.path.join(project_dir, "main.py")):
        zip_path = refresh_project_zip(project_dir)
        return send_file(zip_path, as_attachment=True, download_name=os.path.basename(zip_path))

    output_file = get_latest_built_file()
    if not output_file:
        return jsonify({"error": "No built app found."}), 404

    legacy_zip = os.path.join(BUILDS_DIR, "latest_build_legacy.zip")
    with zipfile.ZipFile(legacy_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(output_file, arcname="main.py")
        zf.writestr("requirements.txt", "")
        zf.writestr(
            "README.md",
            "# Legacy Generated App\n\n"
            "Run with:\n"
            "1. `python -m pip install -r requirements.txt`\n"
            "2. `python main.py`\n",
        )

    return send_file(legacy_zip, as_attachment=True, download_name=os.path.basename(legacy_zip))


@app.route('/state')
def state():
    return jsonify(build_state)


def _build_orbit_context():
    """Build a short context summary for Orbit to explain the generated app."""
    output = build_state.get("output", {})
    latest_project = get_latest_project_dir() or "No generated project folder yet."

    plan = clamp_text(output.get("plan", ""), 900) or "No architecture plan available yet."
    review = clamp_text(output.get("review", ""), 700) or "No review summary available yet."
    code_preview = clamp_text(output.get("code", ""), 1000) or "No generated code preview available yet."

    return (
        f"Latest project folder: {latest_project}\n\n"
        f"Plan:\n{plan}\n\n"
        f"Review:\n{review}\n\n"
        f"Code preview:\n{code_preview}"
    )


def _handle_orbit_chat(question):
    """Answer user questions about the built app in plain language."""
    system_prompt = """You are Orbit, a floating orb guide in an AI Office app builder.
Your job is to explain things in tiny, child-friendly language.
Rules:
- Keep answers very short: 1-2 small sentences.
- Use easy words and one concrete idea.
- Speak in first person with I/me/my.
- Talk to the player as you.
- Keep a playful helper personality.
- Start with "Orbit says:".
- Never say "the user" or mention context blocks.
- If uncertain, briefly say "I might be wrong.".
- Do not use markdown tables or code fences.
"""

    web_context = _fetch_web_search_context(question)
    user_prompt = _build_npc_user_prompt(
        question=question,
        app_context=_build_orbit_context(),
        web_context=web_context,
    )

    model = MODELS.get("reviewer", "mistral:7b")
    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    reply = _format_npc_reply(response["message"]["content"], signature="Orbit says:")

    if web_context and _looks_like_no_answer(reply):
        web_fact = _extract_first_web_fact(web_context)
        if web_fact:
            reply = _format_npc_reply(
                f"{web_fact} I might be wrong, so please double-check.",
                signature="Orbit says:",
            )

    return reply


@app.route('/npc_chat/stream', methods=['POST'])
def npc_chat_stream():
    """SSE endpoint: streams NPC reply tokens as they arrive from Ollama."""
    data = request.get_json(force=True)
    npc_id = data.get('npc_id', '').lower()
    question = data.get('question', '').strip()

    if not question:
        return jsonify({'error': 'Empty question'}), 400

    if npc_id == 'guide':
        persona = {
            'model': MODELS.get('reviewer', 'mistral:7b'),
            'system': (
                "You are Orbit, a floating orb guide in an AI Office app builder. "
                "Keep answers very short: 1-2 small sentences. Use easy words. "
                "Speak in first person with I/me/my. Start with 'Orbit says:'. "
                "Never say 'the user'. If uncertain, say 'I might be wrong.'."
            ),
            'signature': 'Orbit says:',
            'name': 'Orbit',
        }
    elif npc_id in NPC_PERSONAS:
        p = NPC_PERSONAS[npc_id]
        persona = {
            'model': p['model'],
            'system': p['system'],
            'signature': p.get('signature', ''),
            'name': p['name'],
        }
    else:
        return jsonify({'error': 'Unknown NPC'}), 400

    # Only run web search for factual questions; skip entirely for smalltalk.
    web_ctx = ""
    if _needs_web_search(question):
        web_context_box = [None]
        def _fetch_web():
            web_context_box[0] = _fetch_web_search_context(question)
        web_thread = threading.Thread(target=_fetch_web, daemon=True)
        web_thread.start()
        web_thread.join(timeout=WEB_SEARCH_TIMEOUT_SECONDS)
        web_ctx = web_context_box[0] or ""

    user_prompt = _build_npc_user_prompt(question, web_context=web_ctx)

    def generate():
        try:
            stream = ollama.chat(
                model=NPC_CHAT_MODEL,
                messages=[
                    {'role': 'system', 'content': persona['system']},
                    {'role': 'user', 'content': user_prompt},
                ],
                options={'num_predict': 120, 'temperature': 0.75},
                keep_alive='10m',
                stream=True,
            )
            for chunk in stream:
                token = chunk.get('message', {}).get('content', '')
                if token:
                    yield f'data: {json.dumps({"token": token, "done": False})}\n\n'

            yield f'data: {json.dumps({"token": "", "done": True})}\n\n'
        except Exception as exc:
            yield f'data: {json.dumps({"error": str(exc), "done": True})}\n\n'

    resp = make_response(generate())
    resp.headers['Content-Type'] = 'text/event-stream'
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/npc_chat', methods=['POST'])
def npc_chat():
    data = request.get_json(force=True)
    npc_id = data.get('npc_id', '').lower()
    question = data.get('question', '').strip()

    if not question:
        return jsonify({'error': 'Empty question'}), 400

    if npc_id == 'guide':
        try:
            reply = _handle_orbit_chat(question)
            return jsonify({'reply': reply, 'name': 'Orbit', 'npc_id': npc_id})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    if npc_id not in NPC_PERSONAS:
        return jsonify({'error': 'Unknown NPC'}), 400

    persona = NPC_PERSONAS[npc_id]
    try:
        web_context = _fetch_web_search_context(question)
        user_prompt = _build_npc_user_prompt(question=question, web_context=web_context)

        response = ollama.chat(
            model=persona['model'],
            messages=[
                {'role': 'system', 'content': persona['system']},
                {'role': 'user', 'content': user_prompt},
            ],
        )
        reply = _format_npc_reply(
            response['message']['content'],
            signature=persona.get('signature', ''),
        )

        if web_context and _looks_like_no_answer(reply):
            web_fact = _extract_first_web_fact(web_context)
            if web_fact:
                reply = _format_npc_reply(
                    f"{web_fact} I might be wrong, so please double-check.",
                    signature=persona.get('signature', ''),
                )

        return jsonify({'reply': reply, 'name': persona['name'], 'npc_id': npc_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
