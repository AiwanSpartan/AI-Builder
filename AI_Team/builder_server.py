"""
AI Office Builder - Backend
Runs the multi-agent pipeline and broadcasts live status via WebSocket
"""
import asyncio
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import ollama
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
from flask_sock import Sock

# ── Set base folder and builds folder ──────────────────────────
BASE_DIR = r"C:\Users\aiwan\Documents\AI-Builder"
BUILDS_DIR = os.path.join(BASE_DIR, "Builds")
os.makedirs(BUILDS_DIR, exist_ok=True)
LATEST_OUTPUT_FILE = os.path.join(BUILDS_DIR, "built_app.py")

app = Flask(__name__)
CORS(app)
sock = Sock(app)

# Different models per role
MODELS = {
    "architect": "deepseek-r1:7b",
    "coder":     "qwen2.5-coder:7b",
    "debugger":  "deepseek-coder:6.7b",
    "tester":    "qwen2.5-coder:7b",
    "reviewer":  "mistral:7b",
}

# Global state
clients = []
build_state = {
    "status": "idle",
    "agents": {
        "architect": {"state": "idle", "message": "Waiting for a task...", "progress": 0},
        "coder":     {"state": "idle", "message": "Waiting for a task...", "progress": 0},
        "debugger":  {"state": "idle", "message": "Waiting for a task...", "progress": 0},
        "tester":    {"state": "idle", "message": "Waiting for a task...", "progress": 0},
        "reviewer":  {"state": "idle", "message": "Waiting for a task...", "progress": 0},
    },
    "output": {"plan": "", "code": "", "tests": "", "review": ""},
    "log": []
}

def broadcast(event, data):
    """Send event to all connected websocket clients."""
    message = json.dumps({"event": event, "data": data})
    dead = []
    for ws in clients:
        try:
            ws.send(message)
        except:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)

def set_agent(name, state, message, progress=None):
    build_state["agents"][name]["state"] = state
    build_state["agents"][name]["message"] = message
    if progress is not None:
        build_state["agents"][name]["progress"] = progress
    broadcast("agent_update", {"name": name, **build_state["agents"][name]})
    log(f"[{name.upper()}] {message}")

def log(msg):
    entry = {"time": time.strftime("%H:%M:%S"), "msg": msg}
    build_state["log"].append(entry)
    broadcast("log", entry)


def get_latest_built_file():
    """Return the latest runnable app file from the Builds folder."""
    # Always prefer the stable latest target used by the runner.
    if os.path.exists(LATEST_OUTPUT_FILE):
        return LATEST_OUTPUT_FILE

    # Fallback to newest archived build if stable target is missing.
    candidates = []
    try:
        for filename in os.listdir(BUILDS_DIR):
            if filename.startswith("built_app_") and filename.endswith(".py"):
                candidates.append(os.path.join(BUILDS_DIR, filename))
    except FileNotFoundError:
        return None

    if candidates:
        return max(candidates, key=os.path.getmtime)

    return None

def agent_call(name, system_prompt, user_prompt):
    try:
        model = MODELS.get(name, "qwen2.5-coder:7b")
        set_agent(name, "working", f"Thinking... [{model}]", 10)
        response = ollama.chat(model=model, messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt}
        ])
        result = response['message']['content']
        set_agent(name, "done", "Task complete ✓", 100)
        return result
    except Exception as e:
        set_agent(name, "error", f"Error: {str(e)}", 0)
        raise

def is_compilable_python(source):
    """Return True if source is syntactically valid Python code."""
    try:
        compile(source, "<generated_app>", "exec")
        return True
    except Exception:
        return False


def cleanup_candidate(candidate):
    """Drop leading model chatter lines and return a best-effort code candidate."""
    candidate = candidate.strip()
    if not candidate:
        return ""
    if is_compilable_python(candidate):
        return candidate

    lines = candidate.splitlines()
    max_drop = min(25, len(lines) - 1)
    for start in range(1, max_drop + 1):
        sliced = "\n".join(lines[start:]).strip()
        if sliced and is_compilable_python(sliced):
            return sliced

    return candidate


def extract_code(raw):
    """Extract Python code from model output, including fenced responses."""
    if not raw:
        return ""

    candidates = []

    # Prefer fenced code blocks when available.
    fenced_blocks = re.findall(r"```(?:python)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    candidates.extend(block.strip() for block in fenced_blocks if block.strip())

    # Fallback to full raw output.
    candidates.append(raw.strip())

    best_effort = ""
    for candidate in candidates:
        cleaned = cleanup_candidate(candidate)
        if not cleaned:
            continue
        if is_compilable_python(cleaned):
            return cleaned
        if not best_effort:
            best_effort = cleaned

    return best_effort

def run_code(code):
    if not code or not code.strip():
        return "Generated code was empty."

    try:
        compiled = compile(code, "<generated_app>", "exec")
    except Exception:
        return traceback.format_exc()

    try:
        exec(compiled, {})
        return None
    except Exception:
        return traceback.format_exc()

def build_pipeline(user_request):
    build_state["status"] = "building"
    broadcast("build_start", {"request": user_request})
    build_succeeded = False

    try:
        # ── Architect ──────────────────────────────────────────
        set_agent("architect", "working", "Studying the requirements...", 20)
        plan = agent_call("architect",
            """You are a senior software architect. Given a user request, produce a detailed build plan.
Your output MUST include:
1. A clear project folder/file structure
2. Each file's purpose in one line
3. A numbered list of implementation steps in order
4. Any external libraries needed
5. Edge cases or constraints to watch out for
Be specific. Think step by step.""",
            f"Build this application: {user_request}"
        )
        build_state["output"]["plan"] = plan
        set_agent("architect", "done", "Blueprint complete! 📋", 100)
        broadcast("output_update", {"type": "plan", "content": plan})

        # ── Coder ──────────────────────────────────────────────
        set_agent("coder", "working", "Writing code...", 10)
        raw_code = agent_call("coder",
            """You are an expert Python developer. Write complete, production-ready code.
Rules:
- Output ONLY raw Python code. No markdown, no backticks, no explanations.
- Never include natural-language sentences like "Your code is fine" or "Here is the code".
- Every function must have a docstring.
- Handle all edge cases.
- Add inline comments for non-obvious logic.
- The code must be runnable as-is.""",
            f"Implement this plan fully:\n\n{plan}"
        )
        code = extract_code(raw_code)
        if not code.strip():
            raise RuntimeError("Coder returned empty code output.")

        build_state["output"]["code"] = code
        set_agent("coder", "done", "Code written! 💻", 100)
        broadcast("output_update", {"type": "code", "content": code})

        # ── Debugger ───────────────────────────────────────────
        last_error = ""
        debug_success = False
        for attempt in range(5):
            error = run_code(code)
            if not error:
                set_agent("debugger", "done", "No bugs found! All clear 🟢", 100)
                debug_success = True
                break

            last_error = error
            set_agent("debugger", "working", f"Fixing bug (attempt {attempt+1}/5): {error[:60]}...", (attempt+1)*20)
            raw_code = agent_call("debugger",
                """You are an expert Python debugger.
Rules:
- Output ONLY the corrected Python code. No markdown, no backticks.
- Never include explanations or prose.
- Fix the root cause, not just the symptom.
- Do not remove features.""",
                f"Fix this code:\n\n{code}\n\nError:\n{error}"
            )
            code = extract_code(raw_code)
            if not code.strip():
                raise RuntimeError("Debugger returned empty code output.")

            build_state["output"]["code"] = code
            broadcast("output_update", {"type": "code", "content": code})

        if not debug_success:
            set_agent("debugger", "error", "Could not produce runnable code after 5 attempts.", 0)
            raise RuntimeError(
                "Generated app is not runnable after debugging. "
                f"Last error: {last_error[:350]}"
            )

        # ── Tester ─────────────────────────────────────────────
        set_agent("tester", "working", "Writing test cases...", 20)
        raw_tests = agent_call("tester",
            """You are a senior QA engineer.
Rules:
- Write pytest unit tests for every function.
- Include: happy path, edge cases, and failure tests.
- Use descriptive test names.
- Output ONLY raw Python test code. No markdown.""",
            f"Write comprehensive tests:\n\n{code}"
        )
        tests = extract_code(raw_tests)
        build_state["output"]["tests"] = tests
        set_agent("tester", "done", "All tests written! 🧪", 100)
        broadcast("output_update", {"type": "tests", "content": tests})

        # ── Reviewer ───────────────────────────────────────────
        set_agent("reviewer", "working", "Reviewing code quality...", 30)
        review = agent_call("reviewer",
            """You are a senior code reviewer. Give a concise report covering:
1. Code quality (1-10)
2. Potential bugs or risks
3. Security issues (if any)
4. Performance concerns
5. One key suggestion""",
            f"Review this code:\n\n{code}"
        )
        build_state["output"]["review"] = review
        set_agent("reviewer", "done", "Review complete! 🔍", 100)
        broadcast("output_update", {"type": "review", "content": review})

        # ── Save to disk ───────────────────────────────────────
        # Ensure the Builds folder exists
        os.makedirs(BUILDS_DIR, exist_ok=True)

        # Optional: make a timestamped filename for each build
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"built_app_{timestamp}.py"
        timestamped_output_file = os.path.join(BUILDS_DIR, output_filename)

        # Save an archive copy for this specific build.
        with open(timestamped_output_file, "w", encoding="utf-8") as f:
            f.write(code)

        # Keep a stable latest file so /run always has a predictable target.
        with open(LATEST_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(code)

        build_succeeded = True

        # Log the exact location
        log(f"[SAVED] build archived at {timestamped_output_file}")
        log(f"[SAVED] latest runnable app updated at {LATEST_OUTPUT_FILE}")
        print(f"[DEBUG] Built app saved to: {timestamped_output_file}")  # For console verification

    except Exception as e:
        set_agent("architect", "error", f"Pipeline failed: {str(e)}", 0)
        log(f"[ERROR] Build pipeline failed: {str(e)}")
        build_state["status"] = "failed"
        broadcast("build_error", {"error": str(e)})
    finally:
        build_state["status"] = "idle"
        broadcast("build_complete", {"success": build_succeeded})

@app.route('/')
@app.route('/office')
@app.route('/office.html')
def index():
    return send_file(os.path.join(BASE_DIR, 'office.html'))

@sock.route('/ws')
def websocket(ws):
    clients.append(ws)
    ws.send(json.dumps({"event": "init", "data": build_state}))
    try:
        while True:
            msg = ws.receive(timeout=30)
            if msg is None:
                break
    except:
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
    for agent in build_state["agents"]:
        build_state["agents"][agent] = {"state": "idle", "message": "Waiting...", "progress": 0}
    build_state["log"] = []
    build_state["output"] = {"plan": "", "code": "", "tests": "", "review": ""}
    t = threading.Thread(target=build_pipeline, args=(user_request,))
    t.daemon = True
    t.start()
    return jsonify({"status": "started"})

@app.route('/run', methods=['POST'])
def run_app():
    output_file = get_latest_built_file()
    if not output_file:
        return jsonify({"error": "No built app found. Build something first."}), 404

    # Fail fast with a clear message if a stale/invalid build file exists.
    try:
        with open(output_file, "r", encoding="utf-8") as f:
            source = f.read()
        compile(source, output_file, "exec")
    except Exception as e:
        return jsonify({
            "error": (
                "Latest built app is not valid Python. "
                f"Please rebuild the app. Details: {str(e)}"
            )
        }), 400

    def generate():
        try:
            proc = subprocess.Popen(
                [sys.executable, output_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=BUILDS_DIR  # <-- use BUILDS_DIR instead
            )
            yield f"data: Running {os.path.basename(output_file)}...\n\n"
            for line in iter(proc.stdout.readline, ''):
                yield f"data: {line.rstrip()}\n\n"
            proc.wait()
            yield f"data: \n\ndata: --- Process exited with code {proc.returncode} ---\n\n"
        except Exception as e:
            yield f"data: ERROR: {str(e)}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/download')
def download():
    output_file = get_latest_built_file()
    if not output_file:
        return jsonify({"error": "No built app found."}), 404
    return send_file(output_file, as_attachment=True, download_name=os.path.basename(output_file))

@app.route('/state')
def state():
    return jsonify(build_state)

if __name__ == '__main__':
    print("🏢 AI Office Server starting on http://localhost:5000")
    app.run(debug=False, port=5000, threaded=True)