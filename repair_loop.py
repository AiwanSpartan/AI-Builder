"""Minimal repair loop CLI.

Runs the validator and scenario checks, collects debug prompts for failures,
saves prompts to `repair_prompts/` and exits with non-zero code when confidence
is below threshold.

Usage:
    python repair_loop.py
"""
import os
import time
from response_validator import (
    parse_architect,
    validate_route,
    run_scenario,
    compute_score,
    generate_debug_prompt,
)
from test_agent import generate_runtime_scenarios


architect_text = """
- GET /weather - returns JSON { "temp": number, "condition": string }
- POST /items - accepts JSON { "name": string } and returns success
- GET /items - returns JSON { "id": number, "name": string }
"""


def run(base='http://localhost:8000', threshold=80):
    # Prefer an `architect.txt` file if present, else fall back to built-in text
    arch_file = os.path.join(os.path.dirname(__file__), 'architect.txt')
    try:
        if os.path.exists(arch_file):
            from architect import load_architect
            schema = load_architect(arch_file)
        else:
            schema = parse_architect(architect_text)
    except Exception:
        schema = parse_architect(architect_text)

    routes_total = len(schema)
    routes_passed = 0
    prompts = []

    for r in schema:
        res = validate_route(r, base)
        if res.get('ok'):
            routes_passed += 1
        else:
            dbg = res.get('debug') or generate_debug_prompt(r, res)
            prompts.append({'route': f"{r['method']} {r['path']}", 'prompt': dbg})

    # generate runtime scenarios from schema
    runtime_scenarios = generate_runtime_scenarios(schema)
    scenarios_total = len(runtime_scenarios)
    scenarios_passed = 0
    for idx, sc in enumerate(runtime_scenarios, start=1):
        sc_res = run_scenario(sc, base)
        if sc_res.get('ok'):
            scenarios_passed += 1
        else:
            # collect debug prompts for failing steps
            for step in sc_res.get('steps', []):
                if not step.get('ok'):
                    dbg = step.get('debug') or generate_debug_prompt({'method': 'SCENARIO', 'path': step.get('path', ''), 'response_type': 'json', 'required_keys': step.get('required_keys', [])}, step)
                    prompts.append({'route': f"SCENARIO#{idx}", 'prompt': dbg})

    # Persistence checks: for each POST/GET pair, verify data persists across GETs
    from response_validator import check_persistence
    for r in schema:
        if r.get('method') == 'POST' and r.get('accepts_json'):
            # attempt matching GET route
            get_path = r['path']
            post_path = r['path']
            req_keys = r.get('required_keys', [])
            if not req_keys:
                # heuristic default when architect didn't specify fields
                sample = {'name': 'persist_name'}
            else:
                sample = {k: f"persist_{k}" for k in req_keys}
            pers = check_persistence(base, post_path, get_path, sample, match_key=(req_keys or ['name'])[0])
            if not pers.get('ok'):
                prompts.append({'route': f'PERSIST {post_path}', 'prompt': generate_debug_prompt(r, pers)})

    summary = {
        'routes_passed': routes_passed,
        'routes_total': routes_total,
        'scenarios_passed': scenarios_passed,
        'scenarios_total': scenarios_total,
        'response_valid_count': routes_passed,
        'response_total': routes_total,
    }

    score = compute_score(summary)

    print('Summary:', summary)
    print('Confidence score:', score)

    if score < threshold:
        # persist prompts for repair agent to consume
        outdir = os.path.join(os.path.dirname(__file__), 'repair_prompts')
        os.makedirs(outdir, exist_ok=True)
        ts = int(time.time())
        fname = os.path.join(outdir, f'prompt_{ts}.txt')
        with open(fname, 'w', encoding='utf-8') as f:
            for p in prompts:
                f.write(f"--- {p['route']} ---\n")
                f.write(p['prompt'])
                f.write('\n\n')

        print(f'Failure: saved {len(prompts)} repair prompt(s) to {fname}')
        return 2

    print('All checks passed; no repair needed.')
    return 0


if __name__ == '__main__':
    import sys
    base = 'http://localhost:8000'
    if len(sys.argv) > 1:
        base = sys.argv[1]
    raise SystemExit(run(base=base))
