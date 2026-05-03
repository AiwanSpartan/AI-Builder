"""Response validation and scenario testing utilities.

Usage (example):
    from response_validator import parse_architect, validate_route, run_scenario, compute_score

    schema = parse_architect(text)
    result = validate_route(schema[0], "http://localhost:8000")

This module is intentionally small and dependency-light: it uses `requests`.
"""
import re
import requests
from typing import List, Dict, Any, Optional


def parse_architect(text: str) -> List[Dict[str, Any]]:
    """Parse a simple Architect route expectations format into structured schema.

    Example line formats this parser supports (basic, forgiving):
      - GET /weather - returns JSON { "temp": number, "condition": string }
      - POST /items - accepts JSON { "name": string } and returns success

    Returns a list of dicts with keys: method, path, response_type, required_keys, accepts_json
    """
    routes = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith(('-', '*')):
            continue
        # remove leading marker
        line = line.lstrip('-* ').strip()
        m = re.match(r"([A-Z]+)\s+([^\s]+)\s*-\s*(.*)", line)
        if not m:
            continue
        method, path, desc = m.groups()
        response_type = 'text'
        accepts_json = False
        required_keys: List[str] = []
        optional_keys: List[str] = []
        field_types: Dict[str, str] = {}
        expected_status: Optional[Any] = None

        if re.search(r"\bJSON\b", desc, re.IGNORECASE):
            response_type = 'json'
        if re.search(r"accepts JSON", desc, re.IGNORECASE):
            accepts_json = True

        # detect explicit expected status codes (e.g., returns 201, status:201, returns 2xx)
        mstat = re.search(r"(?:returns|status)[:\s]*([0-9]{3}|[0-9]xx)", desc, re.IGNORECASE)
        if mstat:
            expected_status = mstat.group(1)

        # try to find a {...} block and extract keys
        brace = re.search(r"\{([^}]*)\}", desc)
        if brace:
            body = brace.group(1)
            # try to capture key: type pairs like "name": string or name: string
            pairs = re.findall(r'"?([A-Za-z0-9_]+)"?\s*:\s*([A-Za-z0-9_\[\]]+)', body)
            if pairs:
                for k, t in pairs:
                    if not k:
                        continue
                    if re.search(r'optional', body, re.IGNORECASE) and re.search(rf"\b{k}\b", body) and re.search(rf"optional\s+{k}", body, re.IGNORECASE):
                        optional_keys.append(k)
                    else:
                        required_keys.append(k)
                    field_types[k] = t.lower()
            else:
                # fallback: find quoted keys or bare words, possibly with ? marker
                keys = re.findall(r'"([^\"]+)"|\b(\w+)\b|(\w+\?)', body)
                # keys is list of tuples from alternation; flatten
                for a, b, c in keys:
                    k = a or b or (c[:-1] if c and c.endswith('?') else c)
                    if not k:
                        continue
                    kl = k.lower()
                    if kl in ('number', 'string', 'int', 'float', 'bool'):
                        continue
                    if c and c.endswith('?'):
                        optional_keys.append(k)
                    else:
                        if re.search(rf"optional\s+{re.escape(k)}", body, re.IGNORECASE):
                            optional_keys.append(k)
                        else:
                            required_keys.append(k)

        routes.append({
            'method': method,
            'path': path,
            'response_type': response_type,
            'required_keys': required_keys,
            'optional_keys': optional_keys,
            'field_types': field_types,
            'accepts_json': accepts_json,
            'expected_status': expected_status,
            'raw_desc': desc,
        })
    return routes


def _make_url(base: str, path: str) -> str:
    if base.endswith('/') and path.startswith('/'):
        return base[:-1] + path
    if not base.endswith('/') and not path.startswith('/'):
        return base + '/' + path
    return base + path


def _has_key_in_obj(obj: Any, key: str) -> bool:
    """Check if `key` exists in obj. Supports dot-paths and recursive search."""
    if obj is None:
        return False
    if '.' in key:
        parts = key.split('.')
        cur = obj
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return False
        return True

    # direct key
    if isinstance(obj, dict) and key in obj:
        return True

    # recursive search for nested dicts/lists
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, (dict, list)) and _has_key_in_obj(v, key):
                return True
    elif isinstance(obj, list):
        for it in obj:
            if isinstance(it, (dict, list)) and _has_key_in_obj(it, key):
                return True
    return False


def json_has_keys(data: Any, keys: List[str]) -> (bool, List[str]):
    """Return (ok, missing_keys) for presence of keys in JSON-like data.

    - If data is a dict: check keys (supports nested paths)
    - If data is a list: accept if any element (dict) contains the keys
    - Otherwise: return False, all keys
    """
    if isinstance(data, dict):
        missing = [k for k in keys if not _has_key_in_obj(data, k)]
        return (len(missing) == 0, missing)

    if isinstance(data, list):
        # if list of dicts, accept if any item has all keys
        for item in data:
            if isinstance(item, dict):
                ok, missing = json_has_keys(item, keys)
                if ok:
                    return True, []
        # fallback: check first element for diagnostic
        if data and isinstance(data[0], dict):
            missing = [k for k in keys if not _has_key_in_obj(data[0], k)]
            return (len(missing) == 0, missing)
        return False, keys

    return False, keys


def _type_matches(value: Any, expected: str) -> bool:
    if expected is None:
        return True
    exp = expected.lower()
    if exp in ('string', 'str'):
        return isinstance(value, str)
    if exp in ('number', 'float'):
        return isinstance(value, (int, float))
    if exp in ('int', 'integer'):
        return isinstance(value, int) and not isinstance(value, bool)
    if exp in ('bool', 'boolean'):
        return isinstance(value, bool)
    if exp in ('array', 'list'):
        return isinstance(value, list)
    if exp in ('object', 'dict'):
        return isinstance(value, dict)
    # allow loose matching for unknowns
    return True


def json_validate_types(data: Any, field_types: Dict[str, str]) -> (bool, Dict[str, Any]):
    """Validate that keys in data match the expected `field_types` mapping.

    Returns (ok, details) where details contains mismatches.
    """
    mismatches = {}
    if not field_types:
        return True, {}

    # If data is a list, check first dict element that contains keys
    if isinstance(data, list):
        target = None
        for it in data:
            if isinstance(it, dict):
                target = it
                break
        if target is None:
            return False, {'reason': 'Expected list of objects for type checking'}
        data = target

    if not isinstance(data, dict):
        return False, {'reason': 'Expected JSON object for type checking'}

    for k, expected in field_types.items():
        # Support nested keys (dot-path)
        if '.' in k:
            parts = k.split('.')
            cur = data
            ok_found = True
            for p in parts:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    ok_found = False
                    break
            if not ok_found:
                mismatches[k] = {'reason': 'missing'}
                continue
            if not _type_matches(cur, expected):
                mismatches[k] = {'expected': expected, 'actual_type': type(cur).__name__}
        else:
            if k not in data:
                mismatches[k] = {'reason': 'missing'}
                continue
            if not _type_matches(data[k], expected):
                mismatches[k] = {'expected': expected, 'actual_type': type(data[k]).__name__}

    return (len(mismatches) == 0, mismatches)


# HTTP policy: configurable rules for what counts as failure
HTTP_POLICY = {
    'allow_400_for_input': False,  # stricter default: do not allow 400 for input endpoints
}


def set_http_policy(**kw):
    """Update HTTP policy. Example: set_http_policy(allow_400_for_input=False)"""
    HTTP_POLICY.update(kw)


def check_persistence(base_url: str, post_path: str, get_path: str, post_json: Dict[str, Any], match_key: str = 'name', restart_command: Optional[str] = None, timeout: float = 5.0) -> Dict[str, Any]:
    """Check that a POST persists data visible to GET.

    Steps:
      - POST to `post_path` with `post_json`
      - GET `get_path` and confirm an item with `match_key` == post_json[match_key]
      - Optionally run `restart_command` (shell) and repeat GET to ensure persistence across restart

    Returns a dict with keys: ok(bool), reason(str), details(...)
    """
    url_post = _make_url(base_url, post_path)
    url_get = _make_url(base_url, get_path)
    try:
        rpost = requests.post(url_post, json=post_json, timeout=timeout)
    except requests.RequestException as e:
        return {'ok': False, 'reason': f'POST failed: {e}'}

    if rpost.status_code >= 400:
        return {'ok': False, 'reason': f'POST returned bad status {rpost.status_code}', 'status': rpost.status_code, 'body': rpost.text}

    expected_value = post_json.get(match_key)
    try:
        rget = requests.get(url_get, timeout=timeout)
    except requests.RequestException as e:
        return {'ok': False, 'reason': f'GET failed: {e}'}

    if rget.status_code >= 400:
        return {'ok': False, 'reason': f'GET returned bad status {rget.status_code}', 'status': rget.status_code, 'body': rget.text}

    try:
        data = rget.json()
    except Exception:
        return {'ok': False, 'reason': 'GET response not JSON', 'body': rget.text}

    # check presence in list or top-level
    found = False
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get(match_key) == expected_value:
                found = True
                break
    elif isinstance(data, dict):
        if data.get(match_key) == expected_value:
            found = True

    if not found:
        return {'ok': False, 'reason': f'Persist check failed: value {expected_value!r} not present in GET response', 'json': data}

    # optional restart check
    if restart_command:
        import subprocess
        try:
            subprocess.run(restart_command, shell=True, check=True)
        except Exception as e:
            return {'ok': False, 'reason': f'Restart command failed: {e}'}

        # re-check GET
        try:
            rget2 = requests.get(url_get, timeout=timeout)
        except requests.RequestException as e:
            return {'ok': False, 'reason': f'GET after restart failed: {e}'}
        try:
            data2 = rget2.json()
        except Exception:
            return {'ok': False, 'reason': 'GET after restart not JSON', 'body': rget2.text}

        found2 = False
        if isinstance(data2, list):
            for item in data2:
                if isinstance(item, dict) and item.get(match_key) == expected_value:
                    found2 = True
                    break
        elif isinstance(data2, dict):
            if data2.get(match_key) == expected_value:
                found2 = True

        if not found2:
            return {'ok': False, 'reason': f'Persist check after restart failed: value {expected_value!r} not present', 'json_after_restart': data2}

    return {'ok': True, 'status_post': rpost.status_code, 'status_get': rget.status_code}


def generate_debug_prompt(expected: Dict[str, Any], actual: Dict[str, Any]) -> str:
    """Generate a clear, repair-oriented debug prompt for the Debugger/Repair agent.

    `expected` should contain at least `method`, `path`, `response_type`, `required_keys`.
    `actual` is the validator result (status, body/json, reason).
    """
    method = expected.get('method', 'GET')
    path = expected.get('path', '/')
    exp_type = expected.get('response_type', 'json')
    req_keys = expected.get('required_keys', [])

    lines = []
    lines.append(f"The route {method} {path} exists but failed validation.")
    lines.append("")
    lines.append("Expected:")
    if exp_type == 'json':
        lines.append(f"- JSON response with keys: {req_keys}")
    else:
        lines.append(f"- {exp_type} response")

    lines.append("")
    lines.append("Actual:")
    status = actual.get('status')
    if status is not None:
        lines.append(f"- HTTP status: {status}")
    reason = actual.get('reason')
    if reason:
        lines.append(f"- Validation reason: {reason}")

    if 'json' in actual:
        lines.append(f"- Returned JSON (truncated): {str(actual.get('json'))[:1000]}")
    elif 'body' in actual:
        lines.append(f"- Returned body (truncated): {str(actual.get('body'))[:1000]}")

    lines.append("")
    lines.append("Suggested fixes:")
    if exp_type == 'json':
        lines.append(f"- Ensure the endpoint returns a JSON object or list containing the required keys: {req_keys}.")
        lines.append("- If the actual JSON wraps the payload (e.g., { \"item\": {...} }), either return the payload at top-level or adjust the Architect expectation to reference the wrapper (e.g., item.name).")
        lines.append("- Verify status codes: return 2xx on success and appropriate 4xx/5xx on client/server errors.")
    else:
        lines.append("- Return the expected content type and body.")

    lines.append("")
    lines.append("Please modify the implementation to match the expected response format and re-run the validator.")
    return "\n".join(lines)


def validate_route(expected: Dict[str, Any], base_url: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Validate a single route against the expectation.

    Returns a dict: { 'ok': bool, 'reason': str, 'status': int, 'details': ... }
    """
    method = expected.get('method', 'GET').upper()
    path = expected.get('path', '/')
    url = _make_url(base_url, path)

    sample_json = None
    if expected.get('accepts_json'):
        sample_json = {k: f"test_{k}" for k in expected.get('required_keys', [])}

    try:
        if method == 'GET':
            res = requests.get(url, timeout=timeout)
        elif method == 'POST':
            res = requests.post(url, json=sample_json, timeout=timeout)
        elif method == 'PUT':
            res = requests.put(url, json=sample_json, timeout=timeout)
        elif method == 'DELETE':
            res = requests.delete(url, timeout=timeout)
        else:
            return {'ok': False, 'reason': f'Unsupported method {method}'}
    except requests.RequestException as e:
        return {'ok': False, 'reason': f'Connection error: {e}'}

    # Strict HTTP validation: treat as failure unless policy allows it
    if res.status_code >= 400:
        allowed = False
        if HTTP_POLICY.get('allow_400_for_input') and res.status_code == 400 and expected.get('accepts_json'):
            allowed = True
        if not allowed:
            out = {'ok': False, 'reason': f'Bad status {res.status_code}', 'status': res.status_code, 'body': res.text}
            out['debug'] = generate_debug_prompt(expected, out)
            return out

    if expected.get('response_type') == 'json':
        try:
            data = res.json()
        except Exception:
            return {'ok': False, 'reason': 'Response not JSON', 'status': res.status_code, 'body': res.text}

        required = expected.get('required_keys', [])
        ok, missing = json_has_keys(data, required)
        if ok:
            # perform optional type validation if architect provided field types
            field_types = expected.get('field_types', {})
            if field_types:
                t_ok, details = json_validate_types(data, field_types)
                if not t_ok:
                    out = {'ok': False, 'reason': 'Type mismatch', 'status': res.status_code, 'json': data, 'type_mismatches': details}
                    out['debug'] = generate_debug_prompt(expected, out)
                    return out
            return {'ok': True, 'status': res.status_code, 'json': data}

        # try to detect wrapper key whose value contains required keys
        wrapper = None
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    w_ok, _ = json_has_keys(v, required)
                    if w_ok:
                        wrapper = k
                        break
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict):
                            w_ok, _ = json_has_keys(it, required)
                            if w_ok:
                                wrapper = k
                                break
                    if wrapper:
                        break

        reason = f"Missing keys: {missing}."
        if wrapper:
            reason += f" Keys found under wrapper '{wrapper}'."

        out = {'ok': False, 'reason': reason, 'status': res.status_code, 'json': data}
        out['debug'] = generate_debug_prompt(expected, out)
        return out

    # For text responses, just succeed if 2xx
    return {'ok': True, 'status': res.status_code, 'body': res.text}


def run_scenario(steps: List[Dict[str, Any]], base_url: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Run a list of sequential steps (scenario) against `base_url`.

    Each step is a dict, e.g.:
      { 'type': 'request', 'method': 'POST', 'path': '/items', 'json': {'name': 'A'} }
      { 'type': 'request', 'method': 'GET', 'path': '/items', 'expect_json': True, 'required_keys': ['name'] }

    Returns summary: { 'ok': bool, 'steps': [ ... ] }
    """
    results = []
    session = requests.Session()
    for i, step in enumerate(steps, start=1):
        t = step.get('type', 'request')
        if t != 'request':
            results.append({'ok': False, 'reason': f'Unknown step type {t}'})
            return {'ok': False, 'steps': results}

        method = step.get('method', 'GET').upper()
        path = step.get('path', '/')
        url = _make_url(base_url, path)
        try:
            if method == 'GET':
                res = session.get(url, timeout=timeout)
            elif method == 'POST':
                res = session.post(url, json=step.get('json'), timeout=timeout)
            elif method == 'PUT':
                res = session.put(url, json=step.get('json'), timeout=timeout)
            elif method == 'DELETE':
                res = session.delete(url, timeout=timeout)
            else:
                results.append({'ok': False, 'reason': f'Unsupported method {method}'})
                return {'ok': False, 'steps': results}
        except requests.RequestException as e:
            results.append({'ok': False, 'reason': f'Connection error: {e}'})
            return {'ok': False, 'steps': results}

        if res.status_code >= 400:
            out = {'ok': False, 'reason': f'Bad status {res.status_code}', 'status': res.status_code, 'body': res.text}
            out['debug'] = generate_debug_prompt({'method': method, 'path': path, 'response_type': 'json' if step.get('expect_json') else 'text', 'required_keys': step.get('required_keys', [])}, out)
            results.append(out)
            return {'ok': False, 'steps': results}

        step_result = {'ok': True, 'status': res.status_code}
        if step.get('expect_json'):
            try:
                data = res.json()
            except Exception:
                step_result.update({'ok': False, 'reason': 'Response not JSON', 'body': res.text})
                results.append(step_result)
                return {'ok': False, 'steps': results}

            required = step.get('required_keys', [])
            ok, missing = json_has_keys(data, required)
            if not ok:
                # detect wrapper if possible for better message
                wrapper = None
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (dict, list)):
                            w_ok, _ = json_has_keys(v, required)
                            if w_ok:
                                wrapper = k
                                break

                reason = f"Missing keys: {missing}."
                if wrapper:
                    reason += f" Keys found under wrapper '{wrapper}'."
                step_result.update({'ok': False, 'reason': reason, 'json': data})
                step_result['debug'] = generate_debug_prompt({'method': method, 'path': path, 'response_type': 'json', 'required_keys': required}, step_result)
                results.append(step_result)
                return {'ok': False, 'steps': results}

            # type validation if provided in the step (field_types)
            field_types = step.get('field_types', {})
            if field_types:
                t_ok, details = json_validate_types(data, field_types)
                if not t_ok:
                    step_result.update({'ok': False, 'reason': 'Type mismatch', 'json': data, 'type_mismatches': details})
                    step_result['debug'] = generate_debug_prompt({'method': method, 'path': path, 'response_type': 'json', 'required_keys': required}, step_result)
                    results.append(step_result)
                    return {'ok': False, 'steps': results}

            step_result['json'] = data

        results.append(step_result)

    return {'ok': True, 'steps': results}


def compute_score(summary: Dict[str, Any]) -> int:
    """Compute confidence score from summary.

    We weight: routes (30), response validity (30), scenarios (40).
    The `summary` may provide granular scenario step counts via
    `scenario_steps_passed` and `scenario_steps_total` for finer scoring.
    """
    score = 0
    try:
        # routes
        rp = summary.get('routes_passed', 0)
        rt = summary.get('routes_total', 0)
        if rt:
            score += int(30 * (rp / rt))

        # response validity
        rv = summary.get('response_valid_count', 0)
        rt2 = summary.get('response_total', 0)
        if rt2:
            score += int(30 * (rv / rt2))

        # scenarios: prefer step-level metrics if available
        sp = summary.get('scenarios_passed', 0)
        st = summary.get('scenarios_total', 0)
        ssp = summary.get('scenario_steps_passed')
        sst = summary.get('scenario_steps_total')
        if sst:
            frac = (ssp / sst) if sst else 0
            score += int(40 * frac)
        elif st:
            score += int(40 * (sp / st))
    except Exception:
        return 0
    return max(0, min(100, score))


if __name__ == '__main__':
    print('response_validator module - import and use programmatically')
