"""Multi-agent build pipeline orchestration."""

import re

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
from server_state import broadcast, build_state, log, set_agent


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


def _build_functionality_requirements_hint(user_request):
    req = (user_request or "").strip()
    lowered = req.lower()

    hints = [
        "Functional requirements:",
        "- Build a real web app that runs in a browser (not a placeholder page).",
        "- Include route '/' and interactive behavior that matches the request.",
        "- Do not ship a static 'welcome' page unless the request explicitly asks for static content.",
    ]

    if "calculator" in lowered:
        hints.extend(
            [
                "- Because this is a calculator request, include working arithmetic (+, -, *, /).",
                "- Accept two user numbers and an operator, then show the computed result.",
                "- Handle divide-by-zero safely with a clear user-facing message.",
            ]
        )

    return "\n".join(hints)


def _detect_request_feature_gap(user_request, code):
    lowered_req = (user_request or "").lower()
    lowered_code = (code or "").lower()

    if not _looks_like_web_app(code):
        return (
            "Generated code does not look like a runnable web app. "
            "It must expose route '/' and start a web server."
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


def build_pipeline(user_request):
    """Run architect->coder->debugger->tester->reviewer pipeline."""
    build_state["status"] = "building"
    broadcast("build_start", {"request": user_request})
    build_succeeded = False

    try:
        set_agent("architect", "working", "Studying the requirements...", 20)
        functional_hint = _build_functionality_requirements_hint(user_request)
        plan = agent_call(
            "architect",
            """You are a senior software architect. Given a user request, produce a detailed build plan.
Your output MUST include:
1. A clear project folder/file structure
2. Each file's purpose in one line
3. A numbered list of implementation steps in order
4. Any external libraries needed
5. Edge cases or constraints to watch out for
Be specific. Think step by step.""",
            f"Build this application: {user_request}\n\n{functional_hint}",
        )
        build_state["output"]["plan"] = plan
        set_agent("architect", "done", "Blueprint complete! 📋", 100)
        broadcast("output_update", {"type": "plan", "content": plan})

        code = ""
        last_error = ""
        debug_success = False

        for rebuild_idx in range(MAX_REBUILD_ATTEMPTS + 1):
            attempt_label = rebuild_idx + 1
            total_attempts = MAX_REBUILD_ATTEMPTS + 1

            if rebuild_idx == 0:
                set_agent("coder", "working", "Writing code...", 10)
                coder_prompt = (
                    f"Implement this plan fully:\n\n{plan}\n\n"
                    f"Original user request:\n{user_request}\n\n"
                    f"{functional_hint}"
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
                )

            raw_code = agent_call(
                "coder",
                """You are an expert Python developer. Write complete, production-ready code.
Rules:
- Output ONLY raw Python code. No markdown, no backticks, no explanations.
- Never include natural-language sentences like "Your code is fine" or "Here is the code".
- Never output HTML, CSS, JavaScript, JSON, or non-Python content.
- Build a real app entrypoint for `main.py`, not a placeholder snippet.
- Build a browser-runnable web app by default (Flask/FastAPI/Streamlit/Gradio/Dash).
- Include meaningful runtime behavior and requested UI/actions, not just variable assignments.
- Ensure users can run the app with `python main.py`.
- Every function must have a docstring.
- Handle all edge cases.
- Add inline comments for non-obvious logic.
- The code must be runnable as-is.""",
                coder_prompt,
            )
            code = extract_code(raw_code)
            if not code.strip():
                raise RuntimeError("Coder returned empty code output.")

            build_state["output"]["code"] = code
            set_agent("coder", "done", "Code written! 💻", 100)
            broadcast("output_update", {"type": "code", "content": code})

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

            set_agent(
                "debugger",
                "error",
                f"Could not produce runnable code after {total_attempts} rebuild rounds.",
                0,
            )
            raise RuntimeError(
                "Generated app is not runnable after rebuild retries. "
                "Review panel contains identified mistakes."
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

        bundle = create_project_bundle(code, tests, plan, review, user_request)
        build_state["output"]["code"] = bundle["code_preview"]
        broadcast("output_update", {"type": "code", "content": bundle["code_preview"]})

        build_succeeded = True

        log(f"[SAVED] project folder: {bundle['project_dir']}")
        log(f"[SAVED] entrypoint: {bundle['entrypoint']}")
        log(f"[SAVED] downloadable zip: {bundle['zip_path']}")
        log(f"[SAVED] latest zip pointer: {bundle['latest_zip_path']}")
        print(f"[DEBUG] Built app folder saved to: {bundle['project_dir']}")

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
        broadcast("build_complete", {"success": build_succeeded})
