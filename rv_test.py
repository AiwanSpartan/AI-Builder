"""Test harness that runs `response_validator` checks against the sample app.

Run the sample app in one terminal:
    python sample_app.py

Then run this in another terminal:
    python rv_test.py
"""
from response_validator import parse_architect, validate_route, run_scenario, compute_score
import time


architect_text = """
- GET /weather - returns JSON { "temp": number, "condition": string }
- POST /items - accepts JSON { "name": string } and returns success
- GET /items - returns JSON { "id": number, "name": string }
"""


def main():
    base = 'http://localhost:8000'
    schema = parse_architect(architect_text)
    print('Parsed schema:', schema)

    # Validate routes
    routes_total = len(schema)
    routes_passed = 0
    for r in schema:
        print('Validating', r['method'], r['path'])
        res = validate_route(r, base)
        print(' ->', res)
        if res.get('ok'):
            routes_passed += 1

    # Run scenario: add item then check persistence
    scenario = [
        {'type': 'request', 'method': 'POST', 'path': '/items', 'json': {'name': 'test-item'}},
        {'type': 'request', 'method': 'GET', 'path': '/items', 'expect_json': True, 'required_keys': ['id', 'name']}
    ]
    print('Running scenario...')
    scenario_res = run_scenario(scenario, base)
    print('Scenario result:', scenario_res)

    scenarios_total = 1
    scenarios_passed = 1 if scenario_res.get('ok') else 0

    # response validity counts (quick heuristic)
    response_valid_count = routes_passed
    response_total = routes_total

    summary = {
        'routes_passed': routes_passed,
        'routes_total': routes_total,
        'scenarios_passed': scenarios_passed,
        'scenarios_total': scenarios_total,
        'response_valid_count': response_valid_count,
        'response_total': response_total,
    }

    score = compute_score(summary)
    print('\nSummary:', summary)
    print('Confidence score:', score)


if __name__ == '__main__':
    main()
