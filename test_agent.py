"""Generate runtime tests (scenarios) from Architect schema.

Functions:
 - generate_runtime_scenarios(schema): returns a list of scenario dicts suitable for `run_scenario`.

Simple heuristic:
 - For POST endpoints that accept JSON and have a sibling GET, generate POST then GET scenario.
 - For GET endpoints returning lists, generate a simple GET check.
"""
from typing import List, Dict, Any


def generate_runtime_scenarios(schema: List[Dict[str, Any]], base_path: str = '') -> List[Dict[str, Any]]:
    scenarios = []
    # map paths to route info
    path_map = {r['path']: r for r in schema}

    for r in schema:
        if r.get('method') == 'POST' and r.get('accepts_json'):
            # attempt to find a GET for the same resource (e.g., POST /items and GET /items)
            path = r['path']
            get_route = path_map.get(path)
            if get_route and get_route.get('method', 'GET') == 'GET':
                # build a scenario: POST then GET, expect JSON with required keys
                req_keys = r.get('required_keys', [])
                if not req_keys:
                    post_sample = {'name': 'auto_name'}
                else:
                    post_sample = {k: f"auto_{k}" for k in req_keys}
                scenario = [
                    {'type': 'request', 'method': 'POST', 'path': path, 'json': post_sample},
                    {'type': 'request', 'method': 'GET', 'path': path, 'expect_json': True, 'required_keys': get_route.get('required_keys', [])}
                ]
                scenarios.append(scenario)
        elif r.get('method') == 'GET':
            # simple GET check scenario
            scenario = [
                {'type': 'request', 'method': 'GET', 'path': r['path'], 'expect_json': (r.get('response_type') == 'json'), 'required_keys': r.get('required_keys', [])}
            ]
            scenarios.append(scenario)

        elif r.get('method') in ('PUT', 'DELETE'):
            # attempt to construct a sequence: POST -> PUT -> GET or POST -> DELETE -> GET
            # find a POST for the same base resource
            path = r['path']
            # heuristic: strip trailing /{id} or /<id>
            base = path
            base = base.replace('{id}', '').replace('<id>', '')
            if base.endswith('/'):
                base = base[:-1]
            # try to find POST on base
            post_path = None
            for pth, info in path_map.items():
                if pth.rstrip('/') == base and info.get('method') == 'POST':
                    post_path = pth
                    break
            if post_path:
                post_info = path_map[post_path]
                post_req = post_info.get('required_keys', [])
                if not post_req:
                    post_sample = {'name': 'auto_name'}
                else:
                    post_sample = {k: f"auto_{k}" for k in post_req}
                if r.get('method') == 'PUT':
                    scenario = [
                        {'type': 'request', 'method': 'POST', 'path': post_path, 'json': post_sample},
                        {'type': 'request', 'method': 'PUT', 'path': path, 'json': post_sample},
                        {'type': 'request', 'method': 'GET', 'path': base, 'expect_json': True, 'required_keys': post_info.get('required_keys', [])}
                    ]
                else:
                    # DELETE: ensure item is removed
                    scenario = [
                        {'type': 'request', 'method': 'POST', 'path': post_path, 'json': post_sample},
                        {'type': 'request', 'method': 'DELETE', 'path': path},
                        {'type': 'request', 'method': 'GET', 'path': base, 'expect_json': True, 'required_keys': post_info.get('required_keys', [])}
                    ]
                scenarios.append(scenario)

    return scenarios


if __name__ == '__main__':
    print('test_agent: run generate_runtime_scenarios(schema) programmatically')
