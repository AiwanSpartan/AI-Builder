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

from server_ai import clamp_text, try_repair_code, validate_generated_code
from server_config import BASE_DIR, BUILDS_DIR, MAX_AUTO_REPAIR_ATTEMPTS, MODELS
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
from server_pipeline import build_pipeline
from server_state import (
    app,
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
WEB_SEARCH_TIMEOUT_SECONDS = 4
WEB_SEARCH_MAX_SNIPPETS = 4
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_SMALLTALK_RE = re.compile(
    r"^(?:\s*)(hi|hello|hey|hey there|yo|sup|what'?s up|hii+|heyy+)(?:[\s!,.?]*)$",
    flags=re.IGNORECASE,
)
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


def _ensure_index_template_for_flask(project_dir, source):
    result = ensure_generated_templates_for_project(project_dir, source)
    status = result.get("status")
    if status in {"generated", "upgraded", "copied"}:
        kind = result.get("kind", "")
        path = result.get("path", "")
        source_path = result.get("source", "")
        if source_path:
            log(f"[RUN] Template {status} ({kind}) at {path} from {source_path}")
        else:
            log(f"[RUN] Template {status} ({kind}) at {path}")


def _public_runtime_host_for_request(req):
    host = (req.host or "").split(":", 1)[0].strip()
    if host in {"", "0.0.0.0", "::"}:
        return APP_RUNTIME_LOCAL_HEALTH_HOST
    return host


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


def _build_runtime_rescue_app(reason):
    safe_reason = _escape_html(clamp_text(reason, 320))
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

    reset_build_state_for_new_request()

    t = threading.Thread(target=build_pipeline, args=(user_request,))
    t.daemon = True
    t.start()
    return jsonify({"status": "started"})


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
