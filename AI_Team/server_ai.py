"""Model calls, code extraction, validation, and auto-repair helpers."""

import queue
import re
import threading
import traceback

import ollama

from server_config import (
    AGENT_TIMEOUT_SECONDS,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    MAX_AUTO_REPAIR_ATTEMPTS,
    MODELS,
    REPAIR_CALL_TIMEOUT_SECONDS,
)
from server_state import set_agent


def ollama_chat_with_timeout(model, messages, timeout_seconds):
    """Call ollama.chat with a hard timeout to avoid pipeline hangs."""
    result_q = queue.Queue(maxsize=1)
    error_q = queue.Queue(maxsize=1)

    def _worker():
        try:
            # keep_alive='15m' keeps each model resident in GPU memory long enough
            # to survive the round-trip through other models in the pipeline,
            # so re-used models (e.g. qwen2.5-coder:7b for both coder+tester)
            # don't get evicted and forced to cold-load mid-build.
            response = ollama.chat(model=model, messages=messages, keep_alive='15m')
            result_q.put(response)
        except Exception as exc:
            error_q.put(exc)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(timeout_seconds)

    if worker.is_alive():
        return None, TimeoutError(
            f"Model call timed out after {timeout_seconds}s for model '{model}'."
        )

    if not error_q.empty():
        return None, error_q.get()

    if result_q.empty():
        return None, RuntimeError("Model returned no response.")

    return result_q.get(), None


def agent_call(name, system_prompt, user_prompt, timeout_seconds=None):
    """Run one agent step and update websocket-visible state."""
    try:
        model = MODELS.get(name, "qwen2.5-coder:7b")
        if timeout_seconds is None:
            timeout_seconds = AGENT_TIMEOUT_SECONDS.get(name, DEFAULT_AGENT_TIMEOUT_SECONDS)

        set_agent(name, "working", f"Thinking... [{model}]", 10)
        response, call_error = ollama_chat_with_timeout(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout_seconds=timeout_seconds,
        )

        if call_error:
            raise call_error

        result = response["message"]["content"]
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

    fenced_blocks = re.findall(r"```(?:python)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    candidates.extend(block.strip() for block in fenced_blocks if block.strip())

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
    """Validate generated code safely using syntax compilation only."""
    if not code or not code.strip():
        return "Generated code was empty."

    try:
        compile(code, "<generated_app>", "exec")
    except Exception:
        return traceback.format_exc()

    return None


def clamp_text(text, max_chars=1200):
    """Trim long text to keep logs/prompts manageable."""
    if text is None:
        return ""
    clean = str(text).strip()
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars] + "\n... (truncated)"


def build_failure_diagnostics(code, error_text):
    """Create a readable failure report from Python tracebacks."""
    safe_error = clamp_text(error_text, 1600)
    lines = [
        "Build failed after automated debug attempts.",
        "",
        "Most recent Python error:",
        safe_error or "No error text captured.",
        "",
    ]

    if "SyntaxError" in safe_error:
        lines.append("Likely issue type: SyntaxError (code is not valid Python syntax).")
        match = re.search(r'File "<generated_app>", line (\d+)', safe_error)
        if match and code:
            line_no = int(match.group(1))
            code_lines = code.splitlines()
            if 1 <= line_no <= len(code_lines):
                snippet = code_lines[line_no - 1]
                lines.append(f"Problem line {line_no}: {snippet}")
        lines.append("Suggested fix: ensure the output is pure Python code without prose/HTML/markdown text.")
    elif "NameError" in safe_error:
        lines.append("Likely issue type: NameError (variable/function used before definition).")
        lines.append("Suggested fix: define symbols before use and verify spelling consistency.")
    elif "ModuleNotFoundError" in safe_error:
        lines.append("Likely issue type: Missing dependency.")
        lines.append("Suggested fix: avoid unavailable libraries or include install requirements.")
    elif "IndentationError" in safe_error:
        lines.append("Likely issue type: Bad indentation.")
        lines.append("Suggested fix: use consistent 4-space indentation in blocks.")
    else:
        lines.append("Likely issue type: Runtime exception.")
        lines.append("Suggested fix: check stack trace above and harden edge-case handling.")

    return "\n".join(lines)


def validate_generated_code(code, runtime_check=True):
    """Return None when code validates, else a traceback/error string."""
    if runtime_check:
        return run_code(code)

    if not code or not code.strip():
        return "Generated code was empty."

    try:
        compile(code, "<generated_app>", "exec")
        return None
    except Exception:
        return traceback.format_exc()


def try_repair_code(code, error_text, context_note="", attempts=MAX_AUTO_REPAIR_ATTEMPTS, runtime_check=True):
    """Try to repair invalid generated code using the debugger model."""
    candidate = (code or "").strip()
    last_error = clamp_text(error_text, 1600) or "Unknown error"
    model = MODELS.get("debugger", "qwen2.5-coder:7b")

    system_prompt = """You are an expert Python repair assistant.
Rules:
- Output ONLY valid Python code.
- No markdown, no backticks, no explanations.
- Never output HTML, CSS, JavaScript, JSON, or prose.
- Preserve requested functionality while fixing syntax/runtime problems.
- If required, simplify implementation, but keep it runnable."""

    for attempt in range(1, attempts + 1):
        user_prompt = (
            f"Repair attempt {attempt}/{attempts}.\n"
            f"Context: {clamp_text(context_note, 600)}\n\n"
            f"Current failing code:\n{clamp_text(candidate, 3000)}\n\n"
            f"Error details:\n{clamp_text(last_error, 1500)}"
        )

        response, call_error = ollama_chat_with_timeout(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout_seconds=REPAIR_CALL_TIMEOUT_SECONDS,
        )
        if call_error:
            last_error = f"Repair model call failed: {str(call_error)}"
            continue

        raw = response["message"]["content"]
        candidate = extract_code(raw)
        if not candidate.strip():
            last_error = "Repair model returned empty output."
            continue

        validation_error = validate_generated_code(candidate, runtime_check=runtime_check)
        if not validation_error:
            return True, candidate, ""

        last_error = validation_error

    return False, candidate, last_error
