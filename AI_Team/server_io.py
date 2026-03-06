"""File and project bundle helpers for generated builds."""

import ast
import datetime
import html
import json
import os
import re
import sys
import zipfile

from server_config import (
    BUILDS_DIR,
    LATEST_OUTPUT_FILE,
    LATEST_PROJECT_FILE,
    LATEST_ZIP_FILE,
)


_FLASK_ROUTE_RE = re.compile(r"@app\.route\(\s*['\"]([^'\"]+)['\"]")
_RENDER_INDEX_RE = re.compile(r"render_template\(\s*['\"]index\.html['\"]\s*\)")

_LEGACY_FALLBACK_MARKERS = (
    "generated app is running",
    "fallback page",
)

_TODO_INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Todo App</title>
    <style>
        body {
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            background: linear-gradient(120deg, #ecfeff, #f8fafc);
            color: #0f172a;
        }
        .wrap {
            max-width: 720px;
            margin: 32px auto;
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 14px;
            padding: 20px;
            box-shadow: 0 10px 22px rgba(15, 23, 42, 0.08);
        }
        h1 { margin: 0 0 6px 0; }
        p { margin: 0 0 16px 0; color: #334155; }
        .row {
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 10px;
            margin-bottom: 12px;
        }
        input {
            border: 1px solid #cbd5e1;
            border-radius: 10px;
            padding: 10px;
            font-size: 15px;
        }
        button {
            border: 0;
            border-radius: 10px;
            padding: 10px 12px;
            background: #0f766e;
            color: #ffffff;
            font-weight: 600;
            cursor: pointer;
        }
        button.secondary { background: #334155; }
        ul { list-style: none; margin: 0; padding: 0; }
        li {
            display: grid;
            grid-template-columns: 1fr auto auto;
            gap: 8px;
            align-items: center;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 10px;
            margin-bottom: 8px;
            background: #f8fafc;
        }
        .done { text-decoration: line-through; color: #64748b; }
        .status { margin-top: 12px; min-height: 20px; color: #0f766e; }
    </style>
</head>
<body>
    <main class="wrap">
        <h1>Tiny Todo App</h1>
        <p>Add a task, mark it done, or delete it.</p>

        <div class="row">
            <input id="taskInput" type="text" placeholder="Type a task" />
            <button id="addBtn" type="button">Add</button>
        </div>

        <ul id="todoList"></ul>
        <div id="status" class="status"></div>
    </main>

    <script>
        const taskInput = document.getElementById('taskInput');
        const addBtn = document.getElementById('addBtn');
        const todoList = document.getElementById('todoList');
        const statusEl = document.getElementById('status');

        function setStatus(text, isError = false) {
            statusEl.textContent = text;
            statusEl.style.color = isError ? '#b91c1c' : '#0f766e';
        }

        async function requestJson(path, options = {}) {
            const res = await fetch(path, options);
            const text = await res.text();
            let data = {};
            try {
                data = text ? JSON.parse(text) : {};
            } catch {
                data = { raw: text };
            }

            if (!res.ok) {
                throw new Error(data.error || data.raw || ('Request failed: ' + res.status));
            }

            return data;
        }

        function renderTodos(data) {
            todoList.innerHTML = '';
            const entries = Object.entries(data || {});

            if (!entries.length) {
                const empty = document.createElement('li');
                empty.textContent = 'No tasks yet. Add your first one!';
                todoList.appendChild(empty);
                return;
            }

            for (const [id, item] of entries) {
                const li = document.createElement('li');
                const label = document.createElement('span');
                const taskText = (item && item.task) ? String(item.task) : ('Task #' + id);
                const done = Boolean(item && item.completed);
                label.textContent = taskText;
                if (done) {
                    label.classList.add('done');
                }

                const toggleBtn = document.createElement('button');
                toggleBtn.type = 'button';
                toggleBtn.className = 'secondary';
                toggleBtn.textContent = done ? 'Undo' : 'Done';
                toggleBtn.addEventListener('click', async () => {
                    try {
                        await requestJson('/todos/' + id, {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ completed: !done }),
                        });
                        await loadTodos();
                    } catch (err) {
                        setStatus(err.message, true);
                    }
                });

                const delBtn = document.createElement('button');
                delBtn.type = 'button';
                delBtn.textContent = 'Delete';
                delBtn.addEventListener('click', async () => {
                    try {
                        await requestJson('/todos/' + id, { method: 'DELETE' });
                        await loadTodos();
                    } catch (err) {
                        setStatus(err.message, true);
                    }
                });

                li.appendChild(label);
                li.appendChild(toggleBtn);
                li.appendChild(delBtn);
                todoList.appendChild(li);
            }
        }

        async function loadTodos() {
            try {
                const data = await requestJson('/todos');
                renderTodos(data);
                setStatus('Loaded tasks.');
            } catch (err) {
                setStatus(err.message, true);
            }
        }

        async function addTodo() {
            const task = taskInput.value.trim();
            if (!task) {
                setStatus('Type a task first.', true);
                return;
            }

            try {
                await requestJson('/todos', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task, completed: false }),
                });
                taskInput.value = '';
                await loadTodos();
            } catch (err) {
                setStatus(err.message, true);
            }
        }

        addBtn.addEventListener('click', addTodo);
        taskInput.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter') {
                addTodo();
            }
        });

        loadTodos();
    </script>
</body>
</html>
"""

_GENERIC_INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Generated App</title>
    <style>
        body {
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            background: linear-gradient(130deg, #f0f9ff, #f8fafc);
            color: #0f172a;
        }
        .wrap {
            max-width: 780px;
            margin: 32px auto;
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 14px;
            padding: 20px;
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.08);
        }
        h1 { margin-top: 0; }
        p { color: #334155; }
        ul { padding-left: 20px; }
        li { margin-bottom: 8px; }
        code {
            background: #f1f5f9;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 2px 6px;
        }
    </style>
</head>
<body>
    <main class="wrap">
        <h1>Your App Is Running</h1>
        <p>This template was auto-generated because your code asked for <code>index.html</code> but no file was provided.</p>
        <p>Detected Flask routes:</p>
        <ul>
__ROUTE_ITEMS__
        </ul>
        <p>You can customize this page in <code>templates/index.html</code>.</p>
    </main>
</body>
</html>
"""

_BASIC_INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Generated App</title>
</head>
<body>
    <h1>Generated App Is Running</h1>
    <p>This page was created automatically because <code>templates/index.html</code> was missing.</p>
</body>
</html>
"""


def write_text(path, content):
    """Write text content to disk with UTF-8 encoding."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def read_text(path):
    """Read UTF-8 text from disk."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def safe_project_slug(raw):
    """Create a filesystem-safe project folder suffix."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", (raw or "app").strip()).strip("_").lower()
    return cleaned[:40] or "app"


def get_latest_project_dir():
    """Return the latest generated project directory if available."""
    if os.path.exists(LATEST_PROJECT_FILE):
        try:
            path = read_text(LATEST_PROJECT_FILE).strip()
            if path and os.path.isdir(path) and os.path.exists(os.path.join(path, "main.py")):
                return path
        except Exception:
            pass

    candidates = []
    try:
        for name in os.listdir(BUILDS_DIR):
            full = os.path.join(BUILDS_DIR, name)
            if os.path.isdir(full) and name.startswith("build_") and os.path.exists(os.path.join(full, "main.py")):
                candidates.append(full)
    except FileNotFoundError:
        return None

    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def get_latest_built_file():
    """Return the latest runnable app file from the Builds folder."""
    project_dir = get_latest_project_dir()
    if project_dir:
        main_file = os.path.join(project_dir, "main.py")
        if os.path.exists(main_file):
            return main_file

    if os.path.exists(LATEST_OUTPUT_FILE):
        return LATEST_OUTPUT_FILE

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


def ensure_main_has_entrypoint(code):
    """Append a minimal fallback entrypoint if no __main__ block exists."""
    if "if __name__ == \"__main__\":" in code or "if __name__ == '__main__':" in code:
        return code

    return (
        code.rstrip()
        + "\n\n"
        + "if __name__ == \"__main__\":\n"
        + "    print(\"Generated app loaded. Add a main entrypoint for interactive behavior.\")\n"
    )


def infer_requirements_from_code(code):
    """Infer third-party dependencies from import statements."""
    module_to_package = {
        "bs4": "beautifulsoup4",
        "cv2": "opencv-python",
        "PIL": "Pillow",
        "yaml": "PyYAML",
        "sklearn": "scikit-learn",
    }
    stdlib_modules = set(getattr(sys, "stdlib_module_names", set()))
    ignored = {"app", "tests", "main", "__future__"}

    try:
        tree = ast.parse(code)
    except Exception:
        return []

    discovered = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                discovered.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            discovered.add(node.module.split(".")[0])

    packages = []
    for module in sorted(discovered):
        if module in ignored or module in stdlib_modules:
            continue
        packages.append(module_to_package.get(module, module))
    return packages


def build_project_preview(files):
    """Return a readable multi-file preview for the UI code pane."""
    chunks = []
    for rel_path in sorted(files):
        chunks.append(f"# FILE: {rel_path}\n{files[rel_path].rstrip()}\n")
    return "\n".join(chunks).strip()


def extract_flask_routes(source):
    """Extract distinct Flask route paths from source code."""
    routes = []
    for match in _FLASK_ROUTE_RE.finditer(source or ""):
        route = (match.group(1) or "").strip()
        if route and route not in routes:
            routes.append(route)
    return routes


def generate_index_template_from_code(source):
    """Generate a best-effort index.html for Flask apps that need templates."""
    if not _RENDER_INDEX_RE.search(source or ""):
        return "", ""

    routes = extract_flask_routes(source)
    has_todo_routes = any(route.startswith("/todos") for route in routes)
    if has_todo_routes:
        return _TODO_INDEX_TEMPLATE, "todo"

    route_items = "\n".join(
        f"            <li><code>{html.escape(route, quote=True)}</code></li>"
        for route in (routes or ["/"])
    )
    return _GENERIC_INDEX_TEMPLATE.replace("__ROUTE_ITEMS__", route_items), "generic"


def _looks_like_legacy_fallback_template(content):
    lowered = (content or "").lower()
    return all(marker in lowered for marker in _LEGACY_FALLBACK_MARKERS)


def ensure_generated_templates_for_project(project_dir, source):
    """Ensure Flask template files exist when code references render_template('index.html')."""
    if not _RENDER_INDEX_RE.search(source or ""):
        return {"status": "not_needed", "path": "", "kind": ""}

    templates_dir = os.path.join(project_dir, "templates")
    target_template = os.path.join(templates_dir, "index.html")
    generated_template, generated_kind = generate_index_template_from_code(source)

    if os.path.exists(target_template):
        existing = ""
        try:
            existing = read_text(target_template)
        except Exception:
            existing = ""

        if generated_template and _looks_like_legacy_fallback_template(existing):
            write_text(target_template, generated_template)
            return {
                "status": "upgraded",
                "path": target_template,
                "kind": generated_kind,
            }

        return {"status": "exists", "path": target_template, "kind": "existing"}

    candidate_sources = [
        os.path.join(project_dir, "app", "templates", "index.html"),
        os.path.join(project_dir, "src", "templates", "index.html"),
    ]

    for candidate in candidate_sources:
        if os.path.exists(candidate):
            write_text(target_template, read_text(candidate))
            return {
                "status": "copied",
                "path": target_template,
                "kind": "copied",
                "source": candidate,
            }

    if generated_template:
        write_text(target_template, generated_template)
        return {
            "status": "generated",
            "path": target_template,
            "kind": generated_kind,
        }

    write_text(target_template, _BASIC_INDEX_TEMPLATE)
    return {"status": "generated", "path": target_template, "kind": "basic"}


def create_project_bundle(code, tests, plan, review, user_request):
    """Create a multi-file app folder, zip it, and refresh latest pointers."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = safe_project_slug(user_request)
    project_dir_name = f"build_{timestamp}_{slug}"
    project_dir = os.path.join(BUILDS_DIR, project_dir_name)
    os.makedirs(project_dir, exist_ok=True)

    main_code = ensure_main_has_entrypoint(code)
    reqs = infer_requirements_from_code(main_code)

    tests_code = tests if (tests or "").strip() else "def test_placeholder():\n    assert True\n"
    files = {
        "main.py": main_code,
        "requirements.txt": "\n".join(reqs) + ("\n" if reqs else ""),
        "README.md": (
            "# Generated App\n\n"
            "## Run\n"
            "1. Install dependencies: `python -m pip install -r requirements.txt`\n"
            "2. Start the app: `python main.py`\n\n"
            "This project was generated by AI Office Builder.\n"
        ),
        "app/__init__.py": "\"\"\"Generated application package.\"\"\"\n",
        "app/config.py": (
            "import os\n\n"
            "ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        ),
        "tests/test_generated.py": tests_code,
        "build_meta.json": json.dumps(
            {
                "created_at": timestamp,
                "request": user_request,
                "plan": plan,
                "review": review,
                "entrypoint": "main.py",
            },
            indent=2,
        )
        + "\n",
    }

    template_content, _ = generate_index_template_from_code(main_code)
    if template_content:
        files["templates/index.html"] = template_content

    for rel_path, content in files.items():
        write_text(os.path.join(project_dir, rel_path), content)

    write_text(LATEST_OUTPUT_FILE, main_code)
    legacy_archive = os.path.join(BUILDS_DIR, f"built_app_{timestamp}.py")
    write_text(legacy_archive, main_code)

    write_text(LATEST_PROJECT_FILE, project_dir)

    zip_name = f"{project_dir_name}.zip"
    zip_path = os.path.join(BUILDS_DIR, zip_name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in files:
            abs_path = os.path.join(project_dir, rel_path)
            zf.write(abs_path, arcname=rel_path)

    with zipfile.ZipFile(LATEST_ZIP_FILE, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in files:
            abs_path = os.path.join(project_dir, rel_path)
            zf.write(abs_path, arcname=rel_path)

    return {
        "project_dir": project_dir,
        "entrypoint": os.path.join(project_dir, "main.py"),
        "requirements": os.path.join(project_dir, "requirements.txt"),
        "zip_path": zip_path,
        "latest_zip_path": LATEST_ZIP_FILE,
        "legacy_file": legacy_archive,
        "code_preview": build_project_preview(files),
    }


def refresh_project_zip(project_dir):
    """Recreate project zip files for a given project folder."""
    project_name = os.path.basename(project_dir.rstrip("\\/"))
    zip_path = os.path.join(BUILDS_DIR, f"{project_name}.zip")

    def _write_zip(target_zip):
        with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(project_dir):
                for filename in files:
                    abs_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(abs_path, project_dir)
                    if "__pycache__" in rel_path:
                        continue
                    zf.write(abs_path, arcname=rel_path)

    _write_zip(zip_path)
    _write_zip(LATEST_ZIP_FILE)
    return zip_path


def save_repaired_project_main(project_dir, repaired_code):
    """Persist repaired main.py and refresh legacy/latest artifacts."""
    main_file = os.path.join(project_dir, "main.py")
    write_text(main_file, repaired_code)
    write_text(LATEST_OUTPUT_FILE, repaired_code)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    legacy_archive = os.path.join(BUILDS_DIR, f"built_app_{timestamp}.py")
    write_text(legacy_archive, repaired_code)

    zip_path = refresh_project_zip(project_dir)
    return {
        "main_file": main_file,
        "legacy_file": legacy_archive,
        "zip_path": zip_path,
    }
