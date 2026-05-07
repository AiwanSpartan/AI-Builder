"""Multi-agent build pipeline orchestration."""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from server_ai import (
    agent_call,
    build_failure_diagnostics,
    clamp_text,
    extract_code,
    run_code,
    try_repair_code,
)
from server_config import (
    AGENT_TIMEOUT_SECONDS,
    MAX_AUTO_REPAIR_ATTEMPTS,
    MAX_DEBUG_FIX_ATTEMPTS,
    MAX_REBUILD_ATTEMPTS,
)
from server_io import create_project_bundle
from server_state import broadcast, build_state, log, record_event, set_agent
from server_world_events import emit_world_event


def _looks_like_web_app(code):
    lowered = (code or "").lower()
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


_INTERACTIVE_ACTION_VERBS = (
    "add", "create", "make", "save", "update", "edit", "delete", "remove",
    "submit", "send", "post", "search", "find", "filter", "sort", "calculate",
    "compute", "convert", "translate", "generate", "track", "log", "vote",
    "rate", "comment", "reply", "like", "share", "upload", "download",
    "register", "login", "sign up", "sign in",
)


def _is_interactive_request(lowered_request):
    """Heuristic: most user-facing apps are interactive. Default True unless the
    prompt explicitly says it's a static page."""
    if not lowered_request:
        return True
    if any(k in lowered_request for k in ("static", "landing page", "portfolio", "readme")):
        return False
    return True


def _build_functionality_requirements_hint(user_request):
    req = (user_request or "").strip()
    lowered = req.lower()

    hints = [
        "Functional requirements:",
        "- Build a real web app that runs in a browser (not a placeholder page).",
        "- Include route '/' and interactive behavior that matches the request.",
        "- Do not ship a static 'welcome' page unless the request explicitly asks for static content.",
        "",
        "QUALITY BAR (quality strictly outranks brevity — never ship a barebones MVP):",
        "- Build a POLISHED, production-quality app. Assume the user will use this in front of others.",
        "- Modern UI: thoughtful layout, generous spacing, readable typography (system font stack).",
        "- Default to a polished dark theme with a coherent accent color and subtle gradients.",
        "- Hover states, focus rings, smooth transitions, and visible feedback on every action.",
        "- Empty states with a friendly message ('No items yet — add your first below').",
        "- Error states with clear, human-readable messages, never raw stack traces or 'undefined'.",
        "- Loading states for any async operation (skeleton, spinner, or disabled button).",
        "- Implement MORE than the literal minimum: include sensible related features for the domain.",
        "  e.g. a todo app should also support marking complete, editing inline, and a count.",
        "  e.g. a notes app should also support timestamps, search, and persistence within the session.",
        "- Validate user input thoroughly. Never crash on empty strings, huge inputs, or weird unicode.",
        "- Where natural, include keyboard shortcuts (Enter submits, Escape cancels).",
        "- Use semantic HTML and accessible labels (aria-label or visible labels on inputs).",
    ]

    if _is_interactive_request(lowered):
        hints.extend([
            "- EVERY <button>, <form>, and clickable element in the HTML MUST be wired to a working "
            "Flask route OR to inline JavaScript that updates the page. No dead buttons.",
            "- EVERY fetch()/XHR call in the JavaScript MUST correspond to an @app.route in the same "
            "file with a matching HTTP method (GET/POST/etc.). No 404s and no 405 Method Not Allowed.",
            "- For any 'add/save/submit/create/update/delete' action implied by the prompt, include "
            "BOTH the form/button in the HTML AND the matching POST/DELETE route that performs it.",
            "- Persist state in-memory (a module-level list/dict) so the user can see their actions take effect "
            "between page interactions within a single server run.",
            "- After any action, re-render or fetch the updated state so the user sees the change immediately.",
        ])

    matched_verbs = [v for v in _INTERACTIVE_ACTION_VERBS if v in lowered]
    if matched_verbs:
        verbs_text = ", ".join(sorted(set(matched_verbs))[:6])
        hints.append(
            f"- The user explicitly asked to {verbs_text}. Each of these actions MUST have a working "
            f"end-to-end implementation: a UI control to trigger it, a backend route to handle it, "
            f"and visible feedback that it succeeded."
        )

    if "calculator" in lowered:
        hints.extend(
            [
                "- Because this is a calculator request, include working arithmetic (+, -, *, /).",
                "- Accept two user numbers and an operator, then show the computed result.",
                "- Handle divide-by-zero safely with a clear user-facing message.",
            ]
        )

    return "\n".join(hints)


# Architect plan output: lines like "- GET /api/items - returns list of items"
_PLANNED_ROUTE_RE = re.compile(
    r"^\s*[-*]?\s*(?:`)?(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s,;`]*)",
    re.IGNORECASE | re.MULTILINE,
)


# Architect output: a fenced ```json``` block (introduced by ROUTES_SPEC) that
# contains the route contract. We extract it so the coder can implement it
# verbatim and the smoke test can validate the implementation against it.
_SPEC_BLOCK_RE = re.compile(
    r"ROUTES_SPEC\s*```(?:json)?\s*([\s\S]*?)```",
    re.IGNORECASE,
)
_TYPE_SAMPLES = {
    "string": "sample text",
    "str":    "sample text",
    "int":    1,
    "integer": 1,
    "number": 1,
    "float":  1.5,
    "bool":   True,
    "boolean": True,
    "list":   ["a"],
    "array":  ["a"],
    "dict":   {"key": "value"},
    "object": {"key": "value"},
}


def _extract_spec_from_plan(plan_text):
    """Return the architect's parsed JSON spec, or None if missing/malformed.

    Shape:
      {"routes": [{"path", "method", "purpose", "body", "response"}, ...]}
    """
    if not plan_text:
        return None
    match = _SPEC_BLOCK_RE.search(plan_text)
    raw = (match.group(1).strip() if match else "")
    if not raw:
        # Try to find any JSON object that has a top-level "routes" key.
        for fence in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", plan_text):
            candidate = fence.group(1).strip()
            if '"routes"' in candidate:
                raw = candidate
                break
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        log(f"[SPEC] JSON parse failed: {exc}")
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("routes"), list):
        return None
    # Normalize.
    cleaned_routes = []
    for r in parsed["routes"]:
        if not isinstance(r, dict):
            continue
        path = (r.get("path") or "").strip()
        method = (r.get("method") or "").strip().upper()
        if not path or not method:
            continue
        # Preserve tests only if they're a list of {input, expected} dicts.
        raw_tests = r.get("tests")
        cleaned_tests = []
        if isinstance(raw_tests, list):
            for t in raw_tests:
                if isinstance(t, dict) and "expected" in t and isinstance(t.get("expected"), dict):
                    cleaned_tests.append({
                        "input":    t.get("input") if isinstance(t.get("input"), (dict, type(None))) else None,
                        "expected": t["expected"],
                    })
        cleaned_routes.append({
            "path":     path,
            "method":   method,
            "module":   (r.get("module") or "main").strip().lower().replace(" ", "_") or "main",
            "purpose":  (r.get("purpose") or "").strip(),
            "body":     r.get("body") if isinstance(r.get("body"), dict) else None,
            "response": r.get("response") if isinstance(r.get("response"), dict) else None,
            "tests":    cleaned_tests,
        })
    if not cleaned_routes:
        return None
    return {"routes": cleaned_routes}


# Smoke-error type tags. The repair prompt builder uses these to phrase the
# fix request precisely instead of dumping a wall of mixed errors on the model.
ERR_ROUTE_MISSING    = "ROUTE_MISSING"
ERR_METHOD_MISMATCH  = "METHOD_MISMATCH"
ERR_SCHEMA_INVALID   = "SCHEMA_INVALID"
ERR_LOGIC_ERROR      = "LOGIC_ERROR"
ERR_PERSISTENCE_FAIL = "PERSISTENCE_FAIL"
ERR_RUNTIME          = "RUNTIME"


def _tag(code, message):
    """Format a smoke error string with its failure-type tag."""
    return f"[{code}] {message}"


def _classify_smoke_error(tagged_message):
    """Pull the leading [TYPE] tag back off a smoke-error string.
    Returns (tag, body). Falls back to ERR_RUNTIME if the string is unlabeled."""
    m = re.match(r"\[([A-Z_]+)\]\s*(.*)", tagged_message or "")
    if not m:
        return ERR_RUNTIME, (tagged_message or "")
    return m.group(1), m.group(2)


# Per-failure-type guidance the model receives when repairing. Keeping the
# guidance class-specific is the whole point — generic "fix the app" prompts
# are why the repair loop kept guessing.
_REPAIR_GUIDANCE = {
    ERR_ROUTE_MISSING: (
        "ROUTE_MISSING — these routes are declared in the spec but have no "
        "@app.route handler. Add one for each, with the EXACT path and a "
        "matching `methods=[...]` argument. Don't rename anything."
    ),
    ERR_METHOD_MISMATCH: (
        "METHOD_MISMATCH — the route exists but rejects the spec'd HTTP "
        "method. Add the missing method to the existing handler's "
        "`methods=[...]` list (e.g. methods=['GET', 'POST'])."
    ),
    ERR_SCHEMA_INVALID: (
        "SCHEMA_INVALID — the response JSON shape doesn't match the spec. "
        "Add the missing fields and make sure each value is the declared "
        "type. Look at every jsonify(...) and verify the dict matches the "
        "spec's `response` shape exactly."
    ),
    ERR_LOGIC_ERROR: (
        "LOGIC_ERROR — the route returns the wrong VALUE for a known input. "
        "Read the test cases below carefully and fix the implementation so "
        "the actual computation produces the expected result."
    ),
    ERR_PERSISTENCE_FAIL: (
        "PERSISTENCE_FAIL — POSTed data isn't visible in subsequent GETs. "
        "Make sure the storage variable is at MODULE scope (not inside a "
        "function), and that the POST handler appends/updates it before "
        "returning. The GET handler must read the same module-level variable."
    ),
    ERR_RUNTIME: (
        "RUNTIME — the app launches but a request crashes or never responds. "
        "Read the message and fix the underlying Python/Flask issue."
    ),
}


# Pulls "METHOD /path" out of any smoke-error message body so we can attribute
# the failure to a module via the spec.
_ROUTE_IN_MSG_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/\S+?)(?:[\s,;:]|$)",
    re.IGNORECASE,
)


def _extract_route_from_message(message):
    m = _ROUTE_IN_MSG_RE.search(message or "")
    if not m:
        return None, None
    return m.group(1).upper(), m.group(2).rstrip(".,;:)")


def _build_targeted_repair_prompt(smoke_errors, spec):
    """Group smoke errors by MODULE and failure-type, then emit a scoped repair
    prompt that names which modules to touch and which to leave alone.

    Without scoping, repairs ripple — fixing posts can quietly break auth
    because the model rewrites the whole file. The marker-aware coder output
    plus this prompt let us tell the model: "only modify MODULE: posts."
    """
    # grouped = {module_name: {error_tag: [body, ...]}}
    grouped = {}
    untagged_module_errors = []  # errors we couldn't attribute to a module

    for err in smoke_errors:
        tag, body = _classify_smoke_error(err)
        method, path = _extract_route_from_message(body)
        if method and path:
            module = _module_for_route(spec, method, path)
        else:
            module = None
        if module:
            grouped.setdefault(module, {}).setdefault(tag, []).append(body)
        else:
            untagged_module_errors.append((tag, body))

    affected_modules = sorted(grouped.keys())

    sections = ["The app launched but failed runtime checks. Fix every issue below."]

    if affected_modules:
        sections.append("")
        sections.append(
            f"AFFECTED MODULES: {', '.join(affected_modules)}\n"
            "ONLY modify code inside the `# ===== MODULE: <name> =====` blocks "
            "for those modules. Every OTHER module is working correctly — keep "
            "its code byte-for-byte identical, including its markers, helpers, "
            "and module-local state."
        )

    # Stable per-class ordering inside each module.
    order = [
        ERR_ROUTE_MISSING,
        ERR_METHOD_MISMATCH,
        ERR_SCHEMA_INVALID,
        ERR_PERSISTENCE_FAIL,
        ERR_LOGIC_ERROR,
        ERR_RUNTIME,
    ]

    for module in affected_modules:
        sections.append("")
        sections.append(f"=== Failures in MODULE: {module} ===")
        errs_by_tag = grouped[module]
        for tag in order:
            items = errs_by_tag.get(tag) or []
            if not items:
                continue
            sections.append(_REPAIR_GUIDANCE.get(tag, ""))
            for it in items[:5]:
                sections.append(f"  - {it}")

    if untagged_module_errors:
        sections.append("")
        sections.append("=== Other failures (could not attribute to a module) ===")
        for tag, it in untagged_module_errors[:6]:
            sections.append(f"  [{tag}] {it}")

    if spec and spec.get("routes"):
        spec_summary = ", ".join(
            f"{r['method']} {r['path']} ({r.get('module','main')})"
            for r in spec["routes"]
        )
        sections.append("")
        sections.append(f"Spec contract (for reference): {spec_summary}")

    sections.append("")
    sections.append(
        "Output the FULL fixed Python file. PRESERVE every `# ===== MODULE: ... =====` "
        "and `# ===== END: ... =====` marker exactly. Do not delete, rename, or "
        "merge module blocks."
    )
    return "\n".join(sections)


def _matches_type(value, type_hint):
    """Return True iff value structurally matches the declared spec type.

    Accepts type_hint as either a primitive string ("int", "string", ...) or
    a structured shape (dict for nested objects, list for arrays where the
    item type is the first element)."""
    if type_hint is None:
        return value is None

    # Structured shapes recurse.
    if isinstance(type_hint, dict):
        if not isinstance(value, dict):
            return False
        for k, sub_hint in type_hint.items():
            if k == "_":
                continue
            if k not in value:
                return False
            if not _matches_type(value[k], sub_hint):
                return False
        return True

    if isinstance(type_hint, list):
        if not isinstance(value, list):
            return False
        if not type_hint:
            return True  # untyped list — any contents OK
        item_hint = type_hint[0]
        return all(_matches_type(item, item_hint) for item in value)

    # Primitive type names.
    t = str(type_hint).strip().lower()
    if t in ("string", "str"):
        return isinstance(value, str)
    if t in ("int", "integer"):
        return isinstance(value, int) and not isinstance(value, bool)
    if t in ("float", "number"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t in ("bool", "boolean"):
        return isinstance(value, bool)
    if t in ("list", "array"):
        return isinstance(value, list)
    if t in ("dict", "object"):
        return isinstance(value, dict)
    if t in ("null", "none"):
        return value is None
    if t == "any":
        return True
    # Unknown type name — be lenient rather than blocking the build.
    return True


def _validate_response_shape(value, schema, path_prefix=""):
    """Walk a response value against its declared schema and collect every
    field-level mismatch as a list of human-readable strings.

    Returns a list of strings (empty if everything matches)."""
    errors = []
    if schema is None:
        return errors
    # HTML-typed responses skip JSON validation entirely.
    if isinstance(schema, dict) and schema.get("_") == "html":
        return errors

    if isinstance(schema, dict):
        if not isinstance(value, dict):
            errors.append(f"expected object at '{path_prefix or '<root>'}', got {type(value).__name__}")
            return errors
        for field, sub_hint in schema.items():
            if field == "_":
                continue
            child_path = f"{path_prefix}.{field}" if path_prefix else field
            if field not in value:
                errors.append(f"missing field '{child_path}'")
                continue
            errors.extend(_validate_response_shape(value[field], sub_hint, child_path))
        return errors

    if isinstance(schema, list):
        if not isinstance(value, list):
            errors.append(f"expected array at '{path_prefix or '<root>'}', got {type(value).__name__}")
            return errors
        if not schema:
            return errors
        item_hint = schema[0]
        for i, item in enumerate(value):
            errors.extend(_validate_response_shape(item, item_hint, f"{path_prefix}[{i}]"))
        return errors

    # Primitive — flat type check.
    if not _matches_type(value, schema):
        errors.append(
            f"field '{path_prefix or '<root>'}' expected {schema}, got "
            f"{type(value).__name__} ({repr(value)[:40]})"
        )
    return errors


def _expected_subset_matches(actual, expected, path_prefix=""):
    """Like _validate_response_shape but compares concrete VALUES, not types.
    Used to verify logic-assertion test cases. Returns list of mismatch strings.

    Only requires keys in `expected` to be present in `actual` with equal value.
    Extra keys in actual are fine (forward compatibility)."""
    errors = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            errors.append(f"expected object at '{path_prefix or '<root>'}', got {type(actual).__name__}")
            return errors
        for k, sub_expected in expected.items():
            child = f"{path_prefix}.{k}" if path_prefix else k
            if k not in actual:
                errors.append(f"missing '{child}' in response")
                continue
            errors.extend(_expected_subset_matches(actual[k], sub_expected, child))
        return errors
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            errors.append(f"'{path_prefix or '<root>'}' expected list of len {len(expected) if isinstance(expected, list) else '?'}, got {actual!r}"[:200])
            return errors
        for i, e in enumerate(expected):
            errors.extend(_expected_subset_matches(actual[i], e, f"{path_prefix}[{i}]"))
        return errors
    if actual != expected:
        errors.append(f"'{path_prefix or '<root>'}' expected {expected!r}, got {actual!r}")
    return errors


def _build_sample_body(body_schema):
    """Synthesize a JSON body that matches a {field: type} schema. Used to
    smoke-test routes with realistic-shaped data instead of bare `{}`."""
    if not isinstance(body_schema, dict):
        return {}
    sample = {}
    for field, type_hint in body_schema.items():
        if isinstance(type_hint, dict):
            sample[field] = _build_sample_body(type_hint)
        elif isinstance(type_hint, list):
            sample[field] = ["a"]
        else:
            t = str(type_hint).lower().strip()
            sample[field] = _TYPE_SAMPLES.get(t, f"sample_{field}")
    return sample


def _spec_render_for_prompt(spec):
    """Render the spec as a compact JSON snippet suitable for injecting into
    a coder prompt. Returns "" if the spec is empty."""
    if not spec or not spec.get("routes"):
        return ""
    return json.dumps(spec, indent=2)


def _format_route_key(method, path):
    return f"{method.upper()} {path}"


def _module_for_route(spec, method, path):
    """Find which module a (METHOD, PATH) belongs to according to the spec.
    Falls back to inferring from the URL prefix (e.g. /auth/login → 'auth')
    when the spec doesn't have an explicit `module` field."""
    method = (method or "").upper()
    if spec and spec.get("routes"):
        for r in spec["routes"]:
            if r.get("method", "").upper() == method and r.get("path") == path:
                return (r.get("module") or "main").strip().lower() or "main"
    # Fallback: first path segment.
    segs = [s for s in (path or "").split("/") if s]
    return (segs[0].lower() if segs else "main")


# Coder-emitted module markers in the generated source. Lets us attribute a
# specific block of code to a module so repairs can be scoped to that block.
_MODULE_MARKER_RE = re.compile(
    r"#\s*=+\s*MODULE\s*:\s*([A-Za-z0-9_-]+)\s*=+\s*$",
    re.MULTILINE,
)


def _split_code_by_module(code):
    """Parse `# ===== MODULE: name =====` markers and return a dict mapping
    module name → its block of code. Anything before the first marker is
    stored under the synthetic key '__preamble__'.

    If no markers are found, returns {'__preamble__': code} so callers don't
    have to special-case unmodularised output."""
    text = code or ""
    markers = list(_MODULE_MARKER_RE.finditer(text))
    if not markers:
        return {"__preamble__": text}

    blocks = {}
    preamble = text[: markers[0].start()].rstrip()
    if preamble:
        blocks["__preamble__"] = preamble

    for i, m in enumerate(markers):
        name = m.group(1).lower()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        body = text[m.end():end].strip("\n")
        # Append (don't overwrite) so two markers with the same name merge.
        if name in blocks:
            blocks[name] = blocks[name] + "\n\n" + body
        else:
            blocks[name] = body
    return blocks


def _routes_in_code(code):
    """Return [(method, path)] from @app.route declarations in the code.
    Expands routes with multiple methods into one entry each."""
    found = []
    for m in _ROUTE_DECL_RE.finditer(code or ""):
        path = m.group(1)
        methods_text = (m.group(2) or "").upper()
        if methods_text:
            methods = {x.strip().strip("'\"") for x in methods_text.split(",") if x.strip()}
        else:
            methods = {"GET"}
        for meth in methods:
            if meth:
                found.append((meth, path))
    return found


def _validate_routes_against_spec(code, spec):
    """Compare implemented routes vs spec. Returns:
      ([], [])                  — perfect match
      (missing, extra)          — lists of formatted "METHOD /path" strings

    `missing` = in spec but not in code (a 404 waiting to happen)
    `extra`   = in code but not in spec (probably fine, but worth flagging)
    """
    if not spec or not spec.get("routes"):
        return [], []
    spec_keys = {(r["method"].upper(), r["path"]) for r in spec["routes"]}
    code_keys = set(_routes_in_code(code))
    missing = sorted(spec_keys - code_keys)
    extra   = sorted(code_keys - spec_keys)
    return (
        [_format_route_key(m, p) for m, p in missing],
        [_format_route_key(m, p) for m, p in extra],
    )


def _extract_routes_from_plan(plan_text):
    """Parse (METHOD, PATH) tuples out of the architect's plan.

    The architect is prompted to produce a routes section like:
        - GET /api/items - returns the list
        - POST /api/items - creates one
    This function pulls those out so the smoke test can verify each one
    actually responds when the backend is running."""
    routes = []
    seen = set()
    for m in _PLANNED_ROUTE_RE.finditer(plan_text or ""):
        method = m.group(1).upper()
        path = m.group(2).rstrip('.,;:').rstrip(')')
        # Strip trailing markdown like '`'
        path = path.rstrip('`')
        if not path.startswith('/'):
            continue
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        routes.append(key)
    return routes


def _find_smoke_port():
    """Bind to port 0 to let the OS pick a free port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _wait_for_smoke_port(host, port, timeout_seconds):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            try:
                s.connect((host, port))
                return True
            except OSError:
                pass
        time.sleep(0.2)
    return False


def _smoke_test_code(code, expected_routes=None, timeout_seconds=10, spec=None):
    """Subprocess-launch the generated code on a free port and verify '/' loads
    plus every declared route responds.

    When `spec` is provided (the architect's JSON contract), upgrades the smoke
    test in three ways:
      1. POST/PUT/PATCH bodies are synthesized from each route's `body` schema
         instead of bare `{}`, so routes that 400-on-missing-fields don't get
         falsely flagged.
      2. Status codes get stricter checks: 5xx is always a failure, 4xx other
         than 404/405 is allowed (route exists, just needs different data).
      3. Persistence is verified: if the spec has a POST and a GET on the same
         base path, the POST is sent and then the GET response is inspected to
         confirm the new resource shows up — catches "data didn't persist".

    Returns (ok: bool, errors: list[str])."""
    text = (code or "").strip()
    if not text:
        return False, ["Empty code."]

    tmpdir = tempfile.mkdtemp(prefix="aibuilder_smoke_")
    main_file = os.path.join(tmpdir, "main.py")
    log_path = os.path.join(tmpdir, "smoke.log")
    with open(main_file, "w", encoding="utf-8") as f:
        f.write(text)

    port = _find_smoke_port()
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["AIBUILDER_APP_PORT"] = str(port)
    env["AIBUILDER_APP_HOST"] = "127.0.0.1"

    proc = None
    errors = []
    try:
        with open(log_path, "w", encoding="utf-8") as logfh:
            proc = subprocess.Popen(
                [sys.executable, "-u", main_file],
                cwd=tmpdir,
                stdout=logfh,
                stderr=subprocess.STDOUT,
                env=env,
            )

        if not _wait_for_smoke_port("127.0.0.1", port, timeout_seconds):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as logfh:
                    tail = logfh.read()[-900:]
            except Exception:
                tail = ""
            errors.append(
                f"Server failed to start within {timeout_seconds}s. Log tail:\n{tail}"
            )
            return False, errors

        # Always probe '/' so we catch syntax-clean apps that crash on first request.
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/", method="GET"),
                timeout=5,
            ).read()
        except urllib.error.HTTPError as e:
            # 4xx is bad here — '/' should serve a page or JSON
            errors.append(_tag(ERR_RUNTIME, f"GET / returned HTTP {e.code}"))
        except Exception as e:
            errors.append(_tag(ERR_RUNTIME, f"GET / failed: {e}"))

        # Hard runtime gate: every generated app must expose GET /health → 200.
        # This is a stable target that doesn't depend on the user's domain
        # routes, so it cleanly distinguishes "server actually booted" from
        # "server crashed on first request".
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET"),
                timeout=5,
            ) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                errors.append(_tag(
                    ERR_ROUTE_MISSING,
                    "GET /health returned 404 — required boot health route is missing",
                ))
            elif 500 <= e.code < 600:
                errors.append(_tag(
                    ERR_RUNTIME,
                    f"GET /health returned HTTP {e.code} (server error during boot check)",
                ))
            # 4xx other than 404: route exists, treat as booted.
        except Exception as e:
            errors.append(_tag(ERR_RUNTIME, f"GET /health failed: {e}"))

        # Build a quick lookup so we can find a route's schema/tests by key.
        spec_by_route = {}
        if spec and spec.get("routes"):
            for r in spec["routes"]:
                spec_by_route[(r["method"].upper(), r["path"])] = r

        # Build a body lookup keyed by (METHOD, PATH) so the loop below can
        # send realistic data for routes that have body schemas.
        body_by_route = {}
        if spec and spec.get("routes"):
            for r in spec["routes"]:
                if r.get("body"):
                    key = (r["method"].upper(), r["path"])
                    body_by_route[key] = _build_sample_body(r["body"])

        # Don't probe parameterized paths like /api/items/<int:item_id> — we'd
        # need a real ID. The persistence pass below handles those when needed.
        def _is_concrete(p):
            return ("<" not in p) and ("{" not in p)

        for method, path in (expected_routes or []):
            if not _is_concrete(path):
                continue
            url = f"http://127.0.0.1:{port}{path}"
            route_meta = spec_by_route.get((method, path))
            try:
                if method in ("POST", "PUT", "PATCH"):
                    body_obj = body_by_route.get((method, path), {})
                    req = urllib.request.Request(
                        url, method=method,
                        data=json.dumps(body_obj).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                else:
                    req = urllib.request.Request(url, method=method)
                resp = urllib.request.urlopen(req, timeout=5)
                raw_body = resp.read()
                # Deep response-shape validation for spec'd JSON routes.
                # Skip HTML-typed responses, body-less DELETE, and routes with
                # no declared response (we only validate what the spec promised).
                if route_meta and route_meta.get("response") and route_meta["response"].get("_") != "html":
                    try:
                        parsed_body = json.loads(raw_body.decode("utf-8"))
                    except Exception:
                        errors.append(_tag(
                            ERR_SCHEMA_INVALID,
                            f"{method} {path} response is not valid JSON (spec declared a JSON shape)",
                        ))
                    else:
                        shape_issues = _validate_response_shape(parsed_body, route_meta["response"])
                        for issue in shape_issues[:3]:  # keep noise down
                            errors.append(_tag(
                                ERR_SCHEMA_INVALID,
                                f"{method} {path} response: {issue}",
                            ))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    errors.append(_tag(ERR_ROUTE_MISSING, f"{method} {path} returned HTTP 404"))
                elif e.code == 405:
                    errors.append(_tag(ERR_METHOD_MISMATCH, f"{method} {path} returned HTTP 405 (route exists but rejects {method})"))
                elif 500 <= e.code < 600:
                    errors.append(_tag(ERR_RUNTIME, f"{method} {path} returned HTTP {e.code} (server error)"))
                # 4xx other than 404/405: route exists, just rejecting the
                # placeholder body — fine.
            except Exception as e:
                errors.append(_tag(ERR_RUNTIME, f"{method} {path} failed: {e}"))

        # Logic assertions — for each route's `tests`, send the input and
        # verify the response contains the expected fields with equal values.
        # This catches "structurally correct, logically wrong" output (e.g.
        # /add returning {result: 10} for 2+3).
        if spec and spec.get("routes"):
            for r in spec["routes"]:
                tests = r.get("tests") or []
                if not tests:
                    continue
                if not _is_concrete(r["path"]):
                    continue
                test_url = f"http://127.0.0.1:{port}{r['path']}"
                method = r["method"].upper()
                for idx, t in enumerate(tests):
                    try:
                        if method in ("POST", "PUT", "PATCH"):
                            payload = t.get("input") or {}
                            req = urllib.request.Request(
                                test_url, method=method,
                                data=json.dumps(payload).encode("utf-8"),
                                headers={"Content-Type": "application/json"},
                            )
                        else:
                            req = urllib.request.Request(test_url, method=method)
                        resp = urllib.request.urlopen(req, timeout=5)
                        raw = resp.read()
                        try:
                            parsed = json.loads(raw.decode("utf-8"))
                        except Exception:
                            errors.append(_tag(
                                ERR_LOGIC_ERROR,
                                f"{method} {r['path']} test #{idx + 1} returned non-JSON when expected {t['expected']}",
                            ))
                            continue
                        diffs = _expected_subset_matches(parsed, t["expected"])
                        for d in diffs[:2]:
                            input_repr = json.dumps(t.get("input") or {}, separators=(",", ":"))
                            errors.append(_tag(
                                ERR_LOGIC_ERROR,
                                f"{method} {r['path']} with input {input_repr}: {d}",
                            ))
                    except urllib.error.HTTPError as e:
                        # Test failed because the route 4xx'd on its own example.
                        errors.append(_tag(
                            ERR_LOGIC_ERROR,
                            f"{method} {r['path']} test #{idx + 1} returned HTTP {e.code} (expected 2xx with {t['expected']})",
                        ))
                    except Exception as e:
                        errors.append(_tag(
                            ERR_RUNTIME,
                            f"{method} {r['path']} test #{idx + 1} crashed: {e}",
                        ))

        # Persistence pass: when the spec declares both a POST and a GET on
        # the same base path, send the POST then re-fetch the GET and verify
        # something changed. Catches "data didn't persist" silent bugs.
        if spec and spec.get("routes"):
            paths_with_post = {r["path"] for r in spec["routes"] if r["method"].upper() == "POST"}
            paths_with_get  = {r["path"] for r in spec["routes"] if r["method"].upper() == "GET"}
            crud_paths = paths_with_post & paths_with_get
            for p in crud_paths:
                if not _is_concrete(p):
                    continue
                url = f"http://127.0.0.1:{port}{p}"
                # Read state BEFORE the POST.
                before = b""
                try:
                    before = urllib.request.urlopen(
                        urllib.request.Request(url, method="GET"), timeout=5,
                    ).read()
                except Exception:
                    continue  # GET already broken; main loop captured it.
                # POST a realistic body to create a record.
                try:
                    post_body = body_by_route.get(("POST", p), {"name": "smoke_test_marker"})
                    urllib.request.urlopen(
                        urllib.request.Request(
                            url, method="POST",
                            data=json.dumps(post_body).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                        ),
                        timeout=5,
                    ).read()
                except urllib.error.HTTPError as e:
                    if e.code in (404, 405) or 500 <= e.code < 600:
                        # Already reported above.
                        continue
                    # 4xx — body validation rejected our sample. Skip persistence
                    # check but don't flag, since we can't synthesize valid data
                    # for arbitrary schemas.
                    continue
                except Exception:
                    continue
                # Read state AFTER the POST.
                try:
                    after = urllib.request.urlopen(
                        urllib.request.Request(url, method="GET"), timeout=5,
                    ).read()
                except Exception:
                    continue
                if before == after:
                    errors.append(_tag(
                        ERR_PERSISTENCE_FAIL,
                        f"POST {p} did not change subsequent GET {p}. "
                        "Data is not being stored across requests.",
                    ))

        return (len(errors) == 0, errors)

    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, Exception):
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
        try:
            for fn in os.listdir(tmpdir):
                try:
                    os.remove(os.path.join(tmpdir, fn))
                except Exception:
                    pass
            os.rmdir(tmpdir)
        except Exception:
            pass


_FETCH_CALL_RE = re.compile(
    r"""fetch\(\s*['"`]([^'"`]+)['"`]\s*(?:,\s*\{[^}]*method\s*:\s*['"]([A-Za-z]+)['"][^}]*\})?""",
    re.IGNORECASE | re.DOTALL,
)
_ROUTE_DECL_RE = re.compile(
    r"""@app\.route\(\s*['"]([^'"]+)['"](?:\s*,\s*methods\s*=\s*\[([^\]]+)\])?""",
    re.IGNORECASE,
)
# Match any <form ...> opening tag — we'll parse attrs separately so attribute
# order (action before method or vice versa) doesn't matter.
_HTML_FORM_TAG_RE = re.compile(r"<form\b([^>]*)>", re.IGNORECASE)
_FORM_ACTION_ATTR_RE = re.compile(r"""\baction\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
_FORM_METHOD_ATTR_RE = re.compile(r"""\bmethod\s*=\s*['"]([A-Za-z]+)['"]""", re.IGNORECASE)


def _strip_query(path):
    return (path or "").split("?", 1)[0].split("#", 1)[0]


def _route_path_matches(declared, called):
    """True if a declared @app.route path covers the path the JS/form calls.

    Handles trivial Flask path converters: declared '/items/<int:id>' covers
    '/items/123' and dynamic '${...}' template paths."""
    declared = _strip_query(declared)
    called = _strip_query(called)
    if not declared or not called:
        return False
    if declared == called:
        return True
    # Convert declared converters to a regex that accepts any segment.
    pattern = re.sub(r"<[^>]+>", r"[^/]+", declared)
    # Treat ${...} JS interpolation in the called URL as a wildcard segment too.
    called_pattern = re.sub(r"\$\{[^}]+\}", r"[^/]+", called)
    try:
        return bool(re.fullmatch(pattern, called_pattern)) or bool(re.fullmatch(called_pattern, declared))
    except re.error:
        return False


def _find_unrouted_calls(code):
    """Return a list of frontend calls (URL, METHOD) that have no matching @app.route."""
    routes = []
    for m in _ROUTE_DECL_RE.finditer(code or ""):
        path = m.group(1)
        methods_text = (m.group(2) or "").upper()
        methods = {x.strip().strip("'\"") for x in methods_text.split(",")} if methods_text else {"GET"}
        methods = {m for m in methods if m}
        routes.append((path, methods))

    if not routes:
        return []

    def _has_route(url, method):
        method = (method or "GET").upper()
        url_path = _strip_query(url)
        for r_path, r_methods in routes:
            if _route_path_matches(r_path, url_path) and method in r_methods:
                return True
        return False

    unrouted = []
    seen = set()
    for m in _FETCH_CALL_RE.finditer(code or ""):
        url = m.group(1)
        method = (m.group(2) or "GET").upper()
        if url.startswith(("http://", "https://", "//")):
            continue  # external URL, not our backend
        key = (url, method)
        if key in seen:
            continue
        seen.add(key)
        if not _has_route(url, method):
            unrouted.append(key)

    for tag_match in _HTML_FORM_TAG_RE.finditer(code or ""):
        attrs = tag_match.group(1)
        action_match = _FORM_ACTION_ATTR_RE.search(attrs)
        if not action_match:
            continue
        url = action_match.group(1)
        method_match = _FORM_METHOD_ATTR_RE.search(attrs)
        method = (method_match.group(1) if method_match else "GET").upper()
        if url.startswith(("http://", "https://", "//")):
            continue
        key = (url, method)
        if key in seen:
            continue
        seen.add(key)
        if not _has_route(url, method):
            unrouted.append(key)

    return unrouted


def _detect_request_feature_gap(user_request, code):
    lowered_req = (user_request or "").lower()
    lowered_code = (code or "").lower()

    if not _looks_like_web_app(code):
        return (
            "Generated code does not look like a runnable web app. "
            "It must expose route '/' and start a web server."
        )

    # Catch the classic "buttons that don't do anything" failure mode: the HTML
    # makes fetch()/form calls to URLs the Flask code never registered, which
    # produces 404/405 at runtime even though the app technically launches.
    unrouted = _find_unrouted_calls(code)
    if unrouted:
        sample = ", ".join(f"{m} {u}" for u, m in unrouted[:4])
        return (
            "UI/route mismatch: the HTML or JS calls endpoints that no @app.route "
            f"handles ({sample}). Add the missing routes or fix the URLs so every "
            "fetch()/form action hits a real handler with the right HTTP method."
        )

    # If the user asked to do something interactive but the app has no inputs/forms
    # and no non-GET routes, the UI almost certainly can't perform the action.
    if _is_interactive_request(lowered_req):
        action_verbs_in_prompt = [v for v in _INTERACTIVE_ACTION_VERBS if v in lowered_req]
        if action_verbs_in_prompt:
            has_input_ui = any(
                t in lowered_code for t in ("<input", "<textarea", "<select", "<form", "onclick", "addeventlistener")
            )
            has_mutating_route = bool(re.search(
                r"methods\s*=\s*\[[^\]]*['\"](?:POST|PUT|PATCH|DELETE)['\"]",
                lowered_code,
            ))
            if not has_input_ui or not has_mutating_route:
                return (
                    "Interactive feature gap: the user asked to "
                    f"{', '.join(sorted(set(action_verbs_in_prompt))[:4])}, but the app has no "
                    "working input UI or no POST/PUT/DELETE route to perform those actions. "
                    "Add a form/button in the HTML AND a matching mutating route."
                )

    if "calculator" in lowered_req:
        has_arithmetic = (
            any(op in lowered_code for op in [" + ", " - ", " * ", " / "])
            or any(word in lowered_code for word in ["add", "subtract", "multiply", "divide"])
        )
        has_inputs = any(
            token in lowered_code
            for token in [
                "request.form",
                "request.args",
                "request.get_json",
                "type=\"number\"",
                "type='number'",
                "<input",
            ]
        )
        has_result = "result" in lowered_code or "calculate" in lowered_code

        if not (has_arithmetic and has_inputs and has_result):
            return (
                "Calculator feature gap: app does not clearly implement full calculator behavior "
                "(inputs, operations, and visible result output)."
            )

    if re.search(r"welcome\s+to", lowered_code) and lowered_code.count("@app.route") <= 1:
        return (
            "Feature gap: app appears to be mostly a welcome page with limited interaction. "
            "Implement the requested functionality fully."
        )

    return ""


# ── Request scope filter ─────────────────────────────────────────────────────
# This pipeline reliably produces single-purpose Flask apps with ≤3 routes,
# in-memory state, and minimal frontend. Anything beyond that scope tends to
# fail or ship a barebones stub. We classify the user's request before kicking
# off the build so we can either:
#   - REJECT (and suggest a doable alternative) for things we genuinely can't do
#   - NARROW (auto-add scope constraints) for over-broad requests
#   - OK    for requests already in our sweet spot

# (regex, what-it-is, suggested-alternative)
_HARD_REJECT_PATTERNS = [
    (
        r"\b(real[- ]?time|websocket|live\s+(chat|messaging|stream|video)|webrtc)\b",
        "real-time / websocket / WebRTC apps",
        "an HTTP endpoint that returns the current state on each request",
    ),
    (
        r"\b(stripe|paypal|payment|checkout|shopping\s*cart|e-?commerce|billing)\b",
        "payment or e-commerce flows",
        "a product catalog page that just lists items (no checkout)",
    ),
    (
        r"\b(oauth|sso|sign\s*in\s+with\s+(google|github|facebook|apple)|magic\s+link|single\s+sign[- ]on)\b",
        "OAuth / third-party login",
        "a simple form-based prototype with hardcoded demo credentials",
    ),
    (
        r"\b(train(ing)?\s+(a|the|my)\s+model|neural\s+network|tensorflow|pytorch|deep\s+learning|fine[- ]?tune)\b",
        "ML training pipelines",
        "an app that uses simple heuristics or wraps a pre-trained API",
    ),
    (
        r"\b(deploy(ment|ed)?|kubernetes|aws\s+(lambda|ec2|s3)|gcp|azure|heroku|production[- ]ready)\b",
        "deployment / cloud infrastructure",
        "a runnable local Flask app first — deploy separately later",
    ),
    (
        r"\b(postgres(ql)?|mongodb|mongo\s+atlas|mysql|sql\s*server|redis|cassandra|elastic\s*search)\b",
        "external database systems",
        "in-memory state in a Python list/dict",
    ),
    (
        r"\b(full\s*[- ]?stack\s+(app|application|platform)|complete\s+(social|e-?commerce|saas)|multi[- ]?user\s+(chat|forum)|social\s+(network|media)\s+(app|platform|site))\b",
        "full multi-feature platforms",
        "a single-purpose tool that does ONE thing well (e.g. just the feed view, no posting)",
    ),
    (
        r"\b(microservices?|message\s+queue|kafka|rabbitmq|distributed\s+system)\b",
        "distributed / microservice architectures",
        "a single Flask process with synchronous functions",
    ),
]

# (regex, scope-constraint to add to the prompt)
_NARROW_PATTERNS = [
    (
        r"\b(dashboard|admin\s+(panel|console|interface)|control\s+panel)\b",
        "Dashboards: build a SINGLE page that shows the data. No navigation, no settings, no user management.",
    ),
    (
        r"\b(authentication|auth(?!or)|login\s+(system|page|flow)|user\s+(accounts|registration)|signup)\b",
        "Auth: skip it entirely. Hardcode a single demo user or use no auth at all.",
    ),
    (
        r"\b(notifications?|email\s+(sending|delivery)|sms|push\s+notifications?)\b",
        "Outbound notifications: skip them. Show a simulated 'Notification sent' message in the UI instead.",
    ),
    (
        r"\b((file|image|video|photo)\s+upload|drag[- ]and[- ]drop\s+upload)\b",
        "File uploads: accept a URL or pasted text instead of an actual binary upload.",
    ),
    (
        r"\b(3d\s+graphics|three\.?js|webgl|particle\s+system|physics\s+engine)\b",
        "3D / heavy graphics: skip. Use a flat 2D layout with CSS only.",
    ),
    (
        r"\b(charts?|graphs?|visualizations?|analytics?\s+dashboard)\b",
        "Charts: render simple HTML/CSS bars rather than pulling in chart.js or d3.",
    ),
    (
        r"\b(multi[- ]?(page|step\s+form|tab|view)|router|navigation\s+menu)\b",
        "Multi-page / multi-step: collapse to a SINGLE page. Show everything inline or stacked.",
    ),
]

_FEATURE_CONJUNCTION_RE = re.compile(r"\b(?:plus|and|with|including|that\s+also|as\s+well\s+as)\b", re.IGNORECASE)


def classify_request_scope(user_request):
    """Decide whether a user request is in-scope for the build pipeline.

    Returns a 3-tuple:
      ("ok",     processed_request, "")            — proceed as-is
      ("narrow", augmented_request, human_message) — build a constrained version
      ("reject", "",               human_message)  — refuse with a suggestion
    """
    text = (user_request or "").strip()
    if not text:
        return ("reject", "", "Empty request. Please describe what you want to build.")
    if len(text) < 6:
        return ("reject", "", "Request too short to interpret. Add a sentence describing what the app should do.")

    lowered = text.lower()

    # Hard rejects — things we genuinely can't produce reliably.
    for pattern, what, suggestion in _HARD_REJECT_PATTERNS:
        if re.search(pattern, lowered):
            return (
                "reject", "",
                f"That request involves {what}, which this builder can't reliably produce. "
                f"Try {suggestion} instead, then resubmit.",
            )

    # Narrowing — add scope constraints to over-broad requests.
    extra_constraints = []
    for pattern, hint in _NARROW_PATTERNS:
        if re.search(pattern, lowered):
            extra_constraints.append(hint)

    # Feature-count heuristic: prompts joining many features with and/plus/with
    # tend to fail. Force the LLM to pick the single most central feature.
    feature_joins = len(_FEATURE_CONJUNCTION_RE.findall(lowered))
    if feature_joins >= 3:
        extra_constraints.append(
            "Multi-feature scope: the request mentions several features joined together. "
            "Implement ONLY the single most central one. Skip the rest unless trivial to add inline."
        )

    # Length heuristic: very long prompts usually pile on requirements.
    if len(text) > 320:
        extra_constraints.append(
            "Long request: ignore stylistic, brand, or aspirational requirements. "
            "Build the smallest functional core that fulfils the literal ask."
        )

    if extra_constraints:
        augmented = (
            f"{text}\n\n"
            "--- SCOPE CONSTRAINTS (system-applied to keep this build reliable) ---\n"
            + "\n".join(f"- {c}" for c in extra_constraints)
        )
        # First two constraints make a readable user-facing summary.
        note = "Auto-narrowed scope: " + "; ".join(c.split(":")[0] for c in extra_constraints[:2])
        return ("narrow", augmented, note)

    return ("ok", text, "")


# Empirical typical-duration estimates per agent stage on local Ollama, in seconds.
# Used to give the UI a realistic ETA at build_start. Quality > speed: these
# include time for the polish pass at the end of the pipeline.
_TYPICAL_AGENT_DURATIONS = {
    "architect": 75,
    "coder":     120,
    "debugger":  60,
    "tester":    45,
    "reviewer":  45,
    "polish":    75,
}
_TYPICAL_BUILD_SECONDS = sum(_TYPICAL_AGENT_DURATIONS.values())  # ~7 minutes


def _generate_recommendations(user_request, code):
    """Ask the reviewer model for 3 specific, actionable next-step prompts the
    user could submit to improve the app they just got. Returns a list of short
    one-line strings (max 3). Failures degrade to an empty list."""
    if not code or not code.strip():
        return []
    snippet = clamp_text(code, 3200)
    request_text = clamp_text(user_request, 400)
    prompt = (
        f"The user just got this Flask app built for their request:\n"
        f"\"\"\"{request_text}\"\"\"\n\n"
        f"Current code:\n```python\n{snippet}\n```\n\n"
        "Suggest exactly 3 SPECIFIC, ACTIONABLE follow-up prompts the user could "
        "submit to improve THIS app. Each must be:\n"
        "- one short imperative sentence (under 70 characters)\n"
        "- specific to features actually in this app (not generic advice)\n"
        "- something a small follow-up build could realistically add\n\n"
        "Output exactly 3 lines, no numbering, no bullet points, no quotes, no extra text."
    )
    try:
        raw = agent_call(
            "reviewer",
            "You generate concrete, buildable follow-up prompts. Output only short imperative lines.",
            prompt,
            timeout_seconds=60,
        )
    except Exception as exc:
        log(f"[RECS] Recommendation generation failed: {exc}")
        return []
    lines = [l.strip(" -*•\t").strip() for l in (raw or "").splitlines()]
    # Drop empty, drop ones that are obviously markdown noise, cap length.
    cleaned = []
    for l in lines:
        if not l or len(l) > 110:
            continue
        if l.lower().startswith(("here are", "suggestion", "follow-up", "options:")):
            continue
        # Strip a leading numeric "1." / "2)" if the model added one.
        l = re.sub(r"^\s*\d+[\.\)]\s+", "", l)
        if not l:
            continue
        cleaned.append(l)
        if len(cleaned) >= 3:
            break
    return cleaned


_DECOMPOSE_BLOCK_RE = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```",
    re.IGNORECASE,
)


def decompose_request(user_request):
    """Decide whether the request is one cohesive app or a composite of modules.

    Returns a dict shaped like:
        {"complexity": "simple"|"composite", "modules": [{"name", "purpose"}]}

    On any failure (LLM error, malformed JSON), falls back to a single 'main'
    module so the build still proceeds. The decomposer keeps the existing
    pipeline intact — it only enriches what gets handed to the architect."""
    fallback = {"complexity": "simple",
                "modules": [{"name": "main", "purpose": (user_request or "").strip()}]}
    text = (user_request or "").strip()
    if not text or len(text) < 10:
        return fallback

    system_prompt = (
        "You decompose user app requests into independent backend modules.\n"
        "Output ONLY a single fenced JSON block (no prose), shaped exactly like:\n"
        '```json\n{\n  "complexity": "simple" | "composite",\n'
        '  "modules": [\n    {"name": "auth", "purpose": "user login and signup"},\n'
        '    {"name": "posts", "purpose": "create and list posts"}\n  ]\n}\n```\n'
        "\nRules:\n"
        "- 'simple' apps (one cohesive purpose, ≤5 routes) get ONE module named 'main'.\n"
        "- 'composite' apps get 2 to 4 modules. Never more than 4. Each module is a\n"
        "  small group of routes around one responsibility.\n"
        "- Module names: lowercase snake_case, single word when possible.\n"
        "- Do NOT over-decompose. A todo app is ONE module, not three.\n"
        "- Decomposition is only for prompts that genuinely span multiple distinct\n"
        "  responsibilities (auth + content, dashboard + admin, etc.).\n"
    )
    user_prompt = f"Decompose this user request:\n\"\"\"{clamp_text(text, 600)}\"\"\""

    try:
        raw = agent_call(
            "architect",
            system_prompt,
            user_prompt,
            timeout_seconds=60,
        )
    except Exception as exc:
        log(f"[DECOMPOSE] LLM call failed: {exc}")
        return fallback

    raw = (raw or "").strip()
    parsed = None

    # Try a fenced ```json``` block first.
    match = _DECOMPOSE_BLOCK_RE.search(raw)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
        except Exception:
            parsed = None

    # Fallback: scan for the first balanced {...} that mentions "complexity"
    # or "modules" — handles models that emit raw JSON with surrounding prose.
    if parsed is None:
        depth = 0
        start = -1
        for i, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start != -1:
                    candidate = raw[start:i + 1]
                    if '"modules"' in candidate or '"complexity"' in candidate:
                        try:
                            parsed = json.loads(candidate)
                            break
                        except Exception:
                            pass
                    start = -1

    if parsed is None:
        # Last-ditch: try the whole string verbatim.
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            log(f"[DECOMPOSE] JSON parse failed: {exc}")
            return fallback

    if not isinstance(parsed, dict):
        return fallback
    modules_raw = parsed.get("modules") or []
    if not isinstance(modules_raw, list) or not modules_raw:
        return fallback

    cleaned = []
    seen_names = set()
    for m in modules_raw:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip().lower().replace(" ", "_")
        purpose = (m.get("purpose") or "").strip()
        if not name or not purpose:
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        cleaned.append({"name": name, "purpose": purpose})
        if len(cleaned) >= 4:
            break

    if not cleaned:
        return fallback
    if len(cleaned) == 1:
        return {"complexity": "simple", "modules": cleaned}
    return {"complexity": "composite", "modules": cleaned}


def _format_modules_for_architect(user_request, modules):
    """Build the text the architect sees when the request was decomposed."""
    bullet_lines = "\n".join(f"  - {m['name']}: {m['purpose']}" for m in modules)
    return (
        f"{user_request}\n\n"
        f"--- DECOMPOSED MODULES (system-generated) ---\n"
        f"This request spans multiple modules. Build a SINGLE Flask app whose\n"
        f"routes cover every module below. Group routes per module under matching\n"
        f"path prefixes (e.g. /auth/login, /posts/, /comments/) so the resulting\n"
        f"app is internally organized:\n\n{bullet_lines}\n\n"
        f"Keep it to one main.py file (no separate module files). The ROUTES_SPEC\n"
        f"must list every route from every module."
    )


def build_pipeline(user_request):
    """Run decomposer->architect->coder->debugger->tester->reviewer->polish pipeline."""
    build_state["status"] = "building"
    started_at = time.time()
    # Stash on build_state so reconnects via the `init` event can resume the
    # ETA countdown from the right elapsed time, even if they missed `build_start`.
    build_state["started_at"] = started_at
    build_state["eta_seconds"] = _TYPICAL_BUILD_SECONDS
    # Reset any stale post-build artifacts from a previous run.
    build_state.pop("recommendations", None)
    build_state.pop("first_error", None)
    build_state.pop("modules", None)
    broadcast("build_start", {
        "request": user_request,
        "eta_seconds": _TYPICAL_BUILD_SECONDS,
        "started_at": started_at,
    })
    build_succeeded = False
    first_smoke_error = ""

    try:
        # Phase 0 — Decompose. Decide whether this is one cohesive app or a
        # composite of modules. If composite, the architect will see an enriched
        # prompt that lists every module; the rest of the pipeline (spec, smoke,
        # repair) is unchanged.
        set_agent("architect", "working", "Decomposing the request...", 8)
        try:
            broadcast("world_event", emit_world_event("ARCHITECT_START", {"message": "Decomposing the request"}))
        except Exception:
            # Non-fatal: world events best-effort
            pass
        decomposition = decompose_request(user_request)
        modules = decomposition.get("modules", [])
        is_composite = decomposition.get("complexity") == "composite" and len(modules) > 1
        build_state["modules"] = modules
        broadcast("decomposition", {
            "complexity": decomposition.get("complexity", "simple"),
            "modules": modules,
        })
        if is_composite:
            module_names = ", ".join(m["name"] for m in modules)
            log(f"[DECOMPOSE] Composite build with {len(modules)} modules: {module_names}")
            architect_request = _format_modules_for_architect(user_request, modules)
        else:
            log("[DECOMPOSE] Single-module build")
            architect_request = user_request

        set_agent("architect", "working", "Studying the requirements...", 20)
        functional_hint = _build_functionality_requirements_hint(user_request)
        plan = agent_call(
            "architect",
            """You are a senior software architect. Produce a build plan in this EXACT format.

The very FIRST thing in your output must be a fenced JSON block called ROUTES_SPEC.
This is a hard contract that downstream agents implement verbatim — no improvisation.

ROUTES_SPEC
```json
{
  "routes": [
    {
      "path": "/",
      "method": "GET",
      "module": "main",
      "purpose": "serves the main HTML page",
      "body": null,
      "response": {"_": "html"}
    },
    {
      "path": "/health",
      "method": "GET",
      "module": "main",
      "purpose": "boot health check used by the runtime gate",
      "body": null,
      "response": {"status": "string"}
    },
    {
      "path": "/api/items",
      "method": "GET",
      "module": "items",
      "purpose": "list items",
      "body": null,
      "response": {"items": "list"}
    },
    {
      "path": "/api/items",
      "method": "POST",
      "module": "items",
      "purpose": "create one item",
      "body": {"name": "string"},
      "response": {"id": "int", "name": "string"}
    },
    {
      "path": "/api/add",
      "method": "POST",
      "module": "calc",
      "purpose": "add two numbers",
      "body": {"a": "int", "b": "int"},
      "response": {"result": "int"},
      "tests": [
        {"input": {"a": 2, "b": 3},  "expected": {"result": 5}},
        {"input": {"a": -4, "b": 4}, "expected": {"result": 0}},
        {"input": {"a": 0, "b": 0},  "expected": {"result": 0}}
      ]
    }
  ]
}
```

Rules for ROUTES_SPEC:
- ALWAYS valid JSON between the fenced ```json``` markers.
- Every route the backend exposes is listed. No more, no fewer.
- "method" is one of: GET, POST, PUT, PATCH, DELETE.
- "module" — REQUIRED — the module name this route belongs to. For composite
  builds use the names from the decomposition list. For simple builds use "main".
  This lets the system attribute failures to a module so repairs don't ripple
  across unrelated code.
- "body" is null for GET/DELETE, otherwise a {field: type} object — types are
  one of: "string", "int", "float", "bool", "list", "dict".
- "response" is a {field: type} object describing the JSON shape on 2xx, or
  {"_": "html"} for routes that return rendered HTML.
- "tests" — for any route with DETERMINISTIC, INPUT-DEPENDENT output
  (calculations, transforms, lookups), include AT LEAST 3 test cases that VARY
  THE INPUTS and produce DIFFERENT EXPECTED OUTPUTS. Cover at least one edge
  case: zero, negative number, empty string, boundary, or unusual unicode.
  Format: {"input": <body or null>, "expected": <subset of response fields>}.
  WHY 3 minimum: a single case lets the model hardcode `if a==2 and b==3: return 5`.
  Two cases lets it hardcode an if/else. Three forces a real implementation.
  Skip `tests` ONLY if output is genuinely non-deterministic (timestamps, random IDs,
  list ordering).
- Keep paths short and consistent. CRUD endpoints share a base path.
- ALWAYS include a GET `/health` route that returns `{"status": "ok"}` with HTTP
  200. The runtime gate uses this to detect a clean boot. Missing or crashing
  /health = automatic build failure.

After the JSON block, include a short prose plan with these sections:

DATA MODEL — what's stored in memory (Python list/dict shape).
FILE STRUCTURE — single-file Flask app: main.py with embedded HTML via render_template_string.
IMPLEMENTATION STEPS — numbered, in build order.
EDGE CASES — inputs to validate against.

Keep the prose tight. The ROUTES_SPEC JSON block is what other agents follow.""",
            f"Build this application: {architect_request}\n\n{functional_hint}",
        )
        build_state["output"]["plan"] = plan
        set_agent("architect", "done", "Blueprint complete! 📋", 100)
        broadcast("output_update", {"type": "plan", "content": plan})
        record_event("Bob finished planning the app structure")

        # Parse the structured spec; fall back to regex extraction if the model
        # failed to produce valid JSON.
        spec = _extract_spec_from_plan(plan)
        if spec and spec.get("routes"):
            planned_routes = [(r.get("method", "").upper(), r.get("path", ""))
                              for r in spec["routes"] if r.get("path")]
            log(f"[ARCHITECT] Spec contract: {len(spec['routes'])} routes")
        else:
            planned_routes = _extract_routes_from_plan(plan)
            log(f"[ARCHITECT] Falling back to regex routes: {planned_routes if planned_routes else 'none parsed'}")

        code = ""
        last_error = ""
        debug_success = False

        for rebuild_idx in range(MAX_REBUILD_ATTEMPTS + 1):
            attempt_label = rebuild_idx + 1
            total_attempts = MAX_REBUILD_ATTEMPTS + 1

            spec_block = _spec_render_for_prompt(spec)
            spec_section = (
                "\n\n=== ROUTES_SPEC (HARD CONTRACT — implement EXACTLY, no extra "
                "routes, no missing routes, no method changes) ===\n"
                f"```json\n{spec_block}\n```\n"
                "Implementation rules:\n"
                "- Every route in this spec MUST exist as an @app.route with the "
                "  matching path AND `methods=[...]` declaration.\n"
                "- Do not add routes outside this spec. Do not rename paths.\n"
                "- Response shapes must match the `response` field of each route.\n"
                "- For POST/PUT/PATCH, parse JSON via `request.get_json(silent=True) or {}` "
                "  and validate the fields listed in `body`.\n"
                "\n"
                "MODULE STRUCTURE (REQUIRED — do not skip):\n"
                "- Group code by the `module` field on each route. ALL handlers for a\n"
                "  given module must sit between two marker comments:\n"
                "      # ===== MODULE: <name> =====\n"
                "      ... handlers + helpers + module-local state for that module ...\n"
                "      # ===== END: <name> =====\n"
                "- Module-local state goes INSIDE that module's block, with a name\n"
                "  prefixed by the module: `auth_users = {}`, `posts_list = []`. Never\n"
                "  share a single global mutable across modules.\n"
                "- A module may include a `# ===== MODULE: shared =====` block above the\n"
                "  others for imports / app setup / common helpers.\n"
                "- These markers are how the system attributes failures to a module and\n"
                "  scopes repairs. Skipping them causes regressions when bugs are fixed."
            ) if spec_block else ""

            if rebuild_idx == 0:
                set_agent("coder", "working", "Writing code...", 10)
                try:
                    broadcast("world_event", emit_world_event("CODE_START", {"message": "Coder starting implementation"}))
                except Exception:
                    pass
                coder_prompt = (
                    f"Implement this plan fully:\n\n{plan}\n\n"
                    f"Original user request:\n{user_request}\n\n"
                    f"{functional_hint}"
                    f"{spec_section}"
                )
            else:
                set_agent(
                    "coder",
                    "working",
                    f"Rebuilding from failures ({attempt_label}/{total_attempts})...",
                    10,
                )
                log(f"[REBUILD] Starting rebuild attempt {attempt_label}/{total_attempts}")
                coder_prompt = (
                    "Previous build attempt failed. Rebuild from scratch and return only valid runnable Python.\n\n"
                    "Strict requirements:\n"
                    "- No prose, no markdown, no HTML/JS, only Python code.\n"
                    "- Preserve requested functionality.\n"
                    "- Ensure syntax is valid and runtime-safe.\n\n"
                    f"{functional_hint}\n\n"
                    f"Original plan:\n{plan}\n\n"
                    f"Previous failing code:\n{clamp_text(code, 2200)}\n\n"
                    f"Last error details:\n{clamp_text(last_error, 900)}"
                    f"{spec_section}"
                )

            raw_code = agent_call(
                "coder",
                """You are a senior full-stack engineer shipping a polished product, not a prototype.
Quality bar: prefer comprehensiveness and polish over brevity. Take the time to do it right.

Output rules:
- Output ONLY raw Python code. No markdown, no backticks, no explanations.
- Never include natural-language sentences like "Your code is fine" or "Here is the code".
- Never output HTML, CSS, JavaScript, JSON, or non-Python content as standalone output —
  embed all HTML/CSS/JS as Python strings inside render_template_string calls.
- Build a real app entrypoint for `main.py`, not a placeholder snippet.

What to build:
- Default to a single-file Flask app using render_template_string (no external templates/).
- Embed a polished, responsive HTML UI with inline CSS and JS in the same file.
- The UI must look professional: dark theme, accent color, hover states, smooth transitions,
  empty/error/loading states, tasteful typography. Not a 1996-style form.
- Implement the full feature set the prompt implies — not the literal minimum.
  A todo app needs add/edit/complete/delete + count + persistence. A notes app needs
  CRUD + timestamps + search. A calculator needs all four ops + history + clear.
- Every fetch() / form action MUST hit a real @app.route in the same file with the
  matching HTTP method. Mismatches cause 404/405 — verify before output.
- Persist state in module-level Python data structures (list/dict) so the user sees
  their actions take effect within a single server run.

Engineering rules:
- Ensure users can run the app with `python main.py` after `pip install flask`.
- Every function must have a docstring explaining what it does.
- Handle all edge cases: empty input, invalid types, division by zero, missing fields, huge strings.
- Add inline comments for non-obvious logic only (don't restate what the code says).
- The code must be runnable as-is. End with: if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5600")))""",
                coder_prompt,
            )
            code = extract_code(raw_code)
            if not code.strip():
                raise RuntimeError("Coder returned empty code output.")

            build_state["output"]["code"] = code
            set_agent("coder", "done", "Code written! 💻", 100)
            broadcast("output_update", {"type": "code", "content": code})
            record_event("Nia finished building the features")
            try:
                modules_list = build_state.get("modules") or [{"name": "main"}]
                for m in modules_list:
                    name = m.get("name") if isinstance(m, dict) else str(m)
                    broadcast("world_event", emit_world_event("MODULE_COMPLETE", {"module": name, "message": "Module code generated"}))
            except Exception:
                pass

            debug_success = False
            for attempt in range(MAX_DEBUG_FIX_ATTEMPTS):
                error = run_code(code)
                if not error:
                    set_agent("debugger", "done", "No bugs found! All clear 🟢", 100)
                    debug_success = True
                    break

                last_error = error
                set_agent(
                    "debugger",
                    "working",
                    f"Fixing bug (attempt {attempt + 1}/{MAX_DEBUG_FIX_ATTEMPTS}): {error[:60]}...",
                    int(((attempt + 1) / MAX_DEBUG_FIX_ATTEMPTS) * 100),
                )
                raw_code = agent_call(
                    "debugger",
                    """You are an expert Python debugger.
Rules:
- Output ONLY the corrected Python code. No markdown, no backticks.
- Never include explanations or prose.
- Never output HTML, CSS, JavaScript, JSON, or non-Python content.
- Keep the code as a complete runnable `main.py` entrypoint.
- Fix the root cause, not just the symptom.
- Do not remove features.""",
                    f"Fix this code:\n\n{code}\n\nError:\n{error}",
                )
                code = extract_code(raw_code)
                if not code.strip():
                    raise RuntimeError("Debugger returned empty code output.")

                build_state["output"]["code"] = code
                broadcast("output_update", {"type": "code", "content": code})

            # ── Runtime checkpoint: actually launch the code and hit each route ──
            # Static compile-check can't catch route mismatches (the 405 problem),
            # NameErrors that only fire at request time, or apps that crash on the
            # first request. Subprocess-run the code on a free port and HTTP-probe.
            if debug_success:
                # Static spec check first: catch missing/wrong-method routes BEFORE
                # we burn time launching a subprocess. If the architect declared a
                # POST /api/items but the code only has GET /api/items, we know
                # we'll get a 405 — fix it now with a targeted prompt.
                if spec:
                    missing, extra = _validate_routes_against_spec(code, spec)
                    if missing:
                        set_agent("debugger", "working",
                                  f"Adding missing routes: {missing[0]}", 95)
                        log(f"[SPEC] Code missing routes from spec: {missing}")
                        spec_repair_text = (
                            "The implementation is missing routes from the spec contract.\n"
                            f"Missing: {', '.join(missing)}\n"
                            + (f"Extra (in code but not in spec, OK to keep): {', '.join(extra)}\n" if extra else "")
                            + "\nAdd the missing @app.route handlers with the EXACT path and "
                            "methods=[...] declaration from the spec. Don't remove existing routes."
                        )
                        repair_ok, repaired_code, repair_error = try_repair_code(
                            code=code,
                            error_text=spec_repair_text,
                            context_note=(
                                "Implement every route in the architect's spec contract. "
                                f"Spec routes: {[(r['method'], r['path']) for r in spec['routes']]}"
                            ),
                            attempts=2,
                            runtime_check=False,
                        )
                        if repair_ok:
                            still_missing, _ = _validate_routes_against_spec(repaired_code, spec)
                            if not still_missing:
                                code = repaired_code
                                build_state["output"]["code"] = code
                                broadcast("output_update", {"type": "code", "content": code})
                                log("[SPEC] Repair filled in missing routes.")
                            else:
                                log(f"[SPEC] Repair still missing: {still_missing}")

                set_agent("debugger", "working", "Smoke-testing routes...", 96)
                smoke_ok, smoke_errors = _smoke_test_code(
                    code, expected_routes=planned_routes, spec=spec,
                )
                if not smoke_ok and smoke_errors:
                    smoke_summary = "; ".join(smoke_errors[:3])
                    # Remember the first smoke failure so the UI can pre-fill
                    # a "Fix this" follow-up prompt for the user.
                    if not first_smoke_error:
                        first_smoke_error = smoke_errors[0][:240]
                    log(f"[SMOKE] Backend smoke test failed: {smoke_summary}")
                    record_event("Rex spotted something to fix and started repairing")
                    set_agent(
                        "debugger", "working",
                        f"Fixing wiring: {smoke_errors[0][:60]}", 97,
                    )
                    try:
                        # Attribute the first smoke error to a module when possible
                        err_tag, err_body = _classify_smoke_error(smoke_errors[0])
                        meth, path = _extract_route_from_message(smoke_errors[0])
                        module_name = _module_for_route(spec, meth, path) if meth and path and spec else (build_state.get("modules") or [{"name": "main"}])[0].get("name")
                        broadcast("world_event", emit_world_event("DEBUG_ERROR", {
                            "module": module_name,
                            "error_type": err_tag,
                            "message": smoke_errors[0],
                        }))
                    except Exception:
                        pass
                    # Build a TYPE-AWARE repair prompt. Group errors by tag so
                    # we can give the model focused guidance per failure class
                    # instead of dumping everything as one mixed list.
                    repair_text = _build_targeted_repair_prompt(smoke_errors, spec)
                    repair_ok, repaired_code, repair_error = try_repair_code(
                        code=code,
                        error_text=repair_text,
                        context_note=(
                            f"Wire the Flask app so all routes work end-to-end. "
                            f"Architect declared routes: {planned_routes or '(none parsed)'}"
                        ),
                        attempts=2,
                        runtime_check=False,
                    )
                    if repair_ok:
                        # Re-smoke the repaired code; only adopt if it now passes.
                        retry_ok, retry_errors = _smoke_test_code(
                            repaired_code, expected_routes=planned_routes, spec=spec,
                        )
                        if retry_ok:
                            code = repaired_code
                            build_state["output"]["code"] = code
                            broadcast("output_update", {"type": "code", "content": code})
                            # Repair worked — clear the captured first error so
                            # the post-build UI doesn't pre-fill a fix prompt
                            # for an issue that's already gone.
                            first_smoke_error = ""
                            log("[SMOKE] Repair fixed the wiring — smoke test passing.")
                            record_event("Rex fixed the issue — checks are passing again")
                            set_agent("debugger", "done", "Routes verified ✓", 100)
                        else:
                            # Smoke still fails after a targeted repair attempt.
                            # Treat this as a real build failure: feed it back
                            # into the rebuild loop instead of pretending the
                            # debugger said "all clear" on a broken app.
                            unresolved = "; ".join(retry_errors[:3])
                            log(
                                "[SMOKE] Repair did not fix smoke after retry. "
                                f"Forcing rebuild. Remaining: {unresolved}"
                            )
                            last_error = (
                                f"Runtime checks still failing after repair attempt:\n{unresolved}"
                            )
                            debug_success = False
                            set_agent(
                                "debugger", "error",
                                f"Runtime checks failing: {retry_errors[0][:60]}",
                                0,
                            )
                    else:
                        # Repair LLM call itself failed — same outcome: smoke
                        # never recovered, so this iteration didn't actually
                        # produce a runnable app.
                        unresolved = "; ".join(smoke_errors[:3])
                        log(f"[SMOKE] Repair attempt failed: {clamp_text(repair_error, 200)}")
                        last_error = (
                            f"Runtime checks failing and repair could not run:\n{unresolved}"
                        )
                        debug_success = False
                        set_agent(
                            "debugger", "error",
                            f"Runtime checks failing: {smoke_errors[0][:60]}",
                            0,
                        )
                else:
                    set_agent("debugger", "done", "Routes verified ✓", 100)
                    record_event("Rex verified the buttons all work")

            if debug_success:
                feature_gap = _detect_request_feature_gap(user_request, code)
                if feature_gap:
                    last_error = feature_gap
                    debug_success = False
                    set_agent(
                        "debugger",
                        "working",
                        "Improving feature completeness...",
                        94,
                    )
                    log(f"[FEATURE] {feature_gap}")

            if debug_success:
                break

            set_agent(
                "debugger",
                "working",
                f"Trying deep auto-repair ({attempt_label}/{total_attempts})...",
                95,
            )
            log("[REPAIR] Debugger exhausted quick fixes. Starting deep auto-repair pass.")
            repair_ok, repaired_code, repair_error = try_repair_code(
                code=code,
                error_text=last_error,
                context_note=(
                    "Build pipeline deep repair after debugger exhaustion. "
                    f"User request: {user_request}"
                ),
                attempts=MAX_AUTO_REPAIR_ATTEMPTS,
                runtime_check=True,
            )

            if repair_ok:
                feature_gap = _detect_request_feature_gap(user_request, repaired_code)
                if feature_gap:
                    repair_ok = False
                    repair_error = feature_gap
                    last_error = feature_gap
                    log(f"[FEATURE] Deep repair output still incomplete: {feature_gap}")

            if repair_ok:
                code = repaired_code
                build_state["output"]["code"] = code
                broadcast("output_update", {"type": "code", "content": code})
                set_agent("debugger", "done", "Auto-repair recovered runnable code ✓", 100)
                debug_success = True
                log("[REPAIR] Deep auto-repair succeeded. Continuing pipeline.")
                break

            if repair_error:
                last_error = repair_error
                log(f"[REPAIR] Deep auto-repair failed: {clamp_text(repair_error, 300)}")

            diagnostics = build_failure_diagnostics(code, last_error)
            build_state["output"]["review"] = diagnostics
            broadcast("output_update", {"type": "review", "content": diagnostics})

            if rebuild_idx < MAX_REBUILD_ATTEMPTS:
                log("[REBUILD] Debugger exhausted attempts. Retrying full rebuild with failure context.")
                continue

            # Final failure path: build truly didn't pass runtime checks.
            # Demote the ARCHITECT and CODER from DONE → ERROR so the UI doesn't
            # claim success on agents whose output didn't actually run. The
            # plan was structurally correct but the code couldn't be made to
            # work, so both agents share responsibility for the outcome.
            for role in ("architect", "coder"):
                role_state = (build_state.get("agents", {}).get(role) or {}).get("state")
                if role_state == "done":
                    set_agent(
                        role,
                        "error",
                        "Build did not pass runtime checks.",
                        0,
                    )

            # Build a concrete failure summary from the last error captured by
            # the smoke test or the debugger so the user sees WHAT broke, not
            # just "not runnable". Pull the highest-priority smoke error if
            # available, otherwise use the last debugger error.
            failure_detail = clamp_text(last_error or "no specific error captured", 220)
            set_agent(
                "debugger",
                "error",
                f"Build failed runtime checks: {failure_detail[:80]}",
                0,
            )
            raise RuntimeError(
                "Build did not pass runtime checks after "
                f"{total_attempts} attempt(s). Last issue: {failure_detail}"
            )

        tests = ""
        try:
            set_agent("tester", "working", "Writing test cases...", 20)
            raw_tests = agent_call(
                "tester",
                """You are a senior QA engineer.
Rules:
- Write focused pytest unit tests for key functions.
- Include happy path, edge case, and one failure case per major function.
- Keep output concise and runnable.
- Output ONLY raw Python test code. No markdown.""",
                f"Write comprehensive tests:\n\n{code}",
                timeout_seconds=AGENT_TIMEOUT_SECONDS.get("tester"),
            )
            tests = extract_code(raw_tests)
            if not tests.strip():
                raise RuntimeError("Tester returned empty output.")
            build_state["output"]["tests"] = tests
            set_agent("tester", "done", "All tests written! 🧪", 100)
            broadcast("output_update", {"type": "tests", "content": tests})
            record_event("Zoe wrote the tests for the new app")
        except Exception as test_exc:
            test_msg = clamp_text(str(test_exc), 220)
            tests = (
                "# Auto-generated fallback tests (tester timed out or failed).\n"
                f"# Reason: {test_msg}\n\n"
                "def test_placeholder_passes():\n"
                "    assert True\n"
            )
            build_state["output"]["tests"] = tests
            broadcast("output_update", {"type": "tests", "content": tests})
            set_agent("tester", "error", f"Test generation skipped: {test_msg}", 0)
            log(f"[TESTER] Skipped due to timeout/error: {test_msg}")

        review = ""
        try:
            set_agent("reviewer", "working", "Reviewing code quality...", 30)
            review = agent_call(
                "reviewer",
                """You are a senior code reviewer. Give a concise report covering:
1. Code quality (1-10)
2. Potential bugs or risks
3. Security issues (if any)
4. Performance concerns
5. One key suggestion""",
                f"Review this code:\n\n{code}",
                timeout_seconds=AGENT_TIMEOUT_SECONDS.get("reviewer"),
            )
            build_state["output"]["review"] = review
            set_agent("reviewer", "done", "Review complete! 🔍", 100)
            broadcast("output_update", {"type": "review", "content": review})
            record_event("Max finished a quality review of the work")
        except Exception as review_exc:
            review_msg = clamp_text(str(review_exc), 260)
            review = (
                "Automatic review was skipped due to timeout/error.\n"
                f"Reason: {review_msg}\n"
                "Suggestion: run the app and inspect logs/output manually."
            )
            build_state["output"]["review"] = review
            broadcast("output_update", {"type": "review", "content": review})
            set_agent("reviewer", "error", f"Review skipped: {review_msg}", 0)
            log(f"[REVIEWER] Skipped due to timeout/error: {review_msg}")

        # ── Polish pass: apply review feedback to upgrade the code ──────────
        # Quality over speed: take the reviewer's findings and have the coder
        # actually IMPROVE the code rather than ship the first runnable version.
        # Skipped if the reviewer was the empty-fallback (no real feedback).
        review_text = (review or "").strip()
        review_is_useful = review_text and "Automatic review was skipped" not in review_text
        if review_is_useful:
            try:
                set_agent("coder", "working", "Polishing based on review...", 40)
                polish_prompt = (
                    "A senior reviewer just inspected your code. Apply EVERY actionable "
                    "suggestion that improves quality, polish, or feature completeness. "
                    "Do NOT regress functionality. Do NOT introduce new dependencies. "
                    "If the review highlights bugs, fix them. If it flags missing features "
                    "the original prompt implied, add them.\n\n"
                    f"Original user request:\n{user_request}\n\n"
                    f"Current code:\n{clamp_text(code, 4500)}\n\n"
                    f"Reviewer feedback:\n{clamp_text(review_text, 1500)}\n\n"
                    "Output the FULL improved Python file. No prose, no markdown."
                )
                raw_polished = agent_call(
                    "coder",
                    """You are a senior engineer doing a final polish pass before shipping.
Goal: take the existing working code and the reviewer's feedback, and produce a
HIGHER-QUALITY version. Apply every actionable suggestion. Add missing polish
(empty states, error states, hover/focus styles, keyboard shortcuts, input validation).
Never regress features. Output ONLY the full improved Python file.
Constraints: stdlib + flask only, single-file, render_template_string for HTML,
ends with app.run(host=\"0.0.0.0\", port=int(os.environ.get(\"PORT\", \"5600\"))).""",
                    polish_prompt,
                    timeout_seconds=AGENT_TIMEOUT_SECONDS.get("coder"),
                )
                polished_code = extract_code(raw_polished)
                # Only adopt the polished version if it's syntactically valid AND
                # still looks like a web app — otherwise stick with the original.
                if (polished_code.strip()
                        and not run_code(polished_code)
                        and _looks_like_web_app(polished_code)):
                    code = polished_code
                    build_state["output"]["code"] = code
                    broadcast("output_update", {"type": "code", "content": code})
                    set_agent("coder", "done", "Polish complete ✨", 100)
                    log("[POLISH] Applied review feedback — code upgraded.")
                else:
                    set_agent("coder", "done", "Code written! 💻", 100)
                    log("[POLISH] Polished output invalid — keeping reviewed version.")
            except Exception as polish_exc:
                polish_msg = clamp_text(str(polish_exc), 200)
                set_agent("coder", "done", "Code written! 💻", 100)
                log(f"[POLISH] Skipped due to error: {polish_msg}")

        bundle = create_project_bundle(code, tests, plan, review, user_request)
        build_state["output"]["code"] = bundle["code_preview"]
        broadcast("output_update", {"type": "code", "content": bundle["code_preview"]})

        build_succeeded = True
        record_event("The app is finished and ready to test")

        log(f"[SAVED] project folder: {bundle['project_dir']}")
        log(f"[SAVED] entrypoint: {bundle['entrypoint']}")
        log(f"[SAVED] downloadable zip: {bundle['zip_path']}")
        log(f"[SAVED] latest zip pointer: {bundle['latest_zip_path']}")
        print(f"[DEBUG] Built app folder saved to: {bundle['project_dir']}")

        # Generate per-app follow-up suggestions and stash them on build_state
        # so they survive a page refresh. Best-effort — empty list if it fails.
        try:
            recommendations = _generate_recommendations(user_request, code)
        except Exception as rec_exc:
            log(f"[RECS] Failed: {rec_exc}")
            recommendations = []
        build_state["recommendations"] = recommendations
        build_state["first_error"] = first_smoke_error
        log(f"[RECS] {len(recommendations)} suggestions generated")

    except Exception as e:
        msg = clamp_text(str(e), 220)
        active_agents = [
            name for name, info in build_state["agents"].items() if info.get("state") == "working"
        ]
        if active_agents:
            for name in active_agents:
                set_agent(name, "error", f"Pipeline failed: {msg}", 0)
        else:
            set_agent("debugger", "error", f"Pipeline failed: {msg}", 0)

        log(f"[ERROR] Build pipeline failed: {str(e)}")
        build_state["status"] = "failed"
        broadcast("build_error", {"error": str(e)})
    finally:
        build_state["status"] = "idle"
        broadcast("build_complete", {
            "success": build_succeeded,
            "recommendations": build_state.get("recommendations", []),
            "first_error": build_state.get("first_error", ""),
            "user_request": user_request,
            "modules": build_state.get("modules", []),
        })
        try:
            broadcast("world_event", emit_world_event("BUILD_COMPLETE", {
                "success": build_succeeded,
                "recommendations": build_state.get("recommendations", []),
                "first_error": build_state.get("first_error", ""),
                "user_request": user_request,
                "modules": build_state.get("modules", []),
            }))
        except Exception:
            pass
