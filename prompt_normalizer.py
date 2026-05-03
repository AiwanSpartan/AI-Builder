"""Prompt Normalization Engine

Provides functions to classify prompts, extract intent into a simple structured
Architect-like format, and simplify or reject overly complex prompts.
"""
import re
from typing import Dict, Any, Tuple


def classify_prompt(prompt: str) -> str:
    p = prompt.lower()
    if any(x in p for x in ("api", "endpoint", "data", "route")):
        return "api"
    if any(x in p for x in ("game", "player", "3d", "scene")):
        return "game"
    if any(x in p for x in ("website", "ui", "web", "html", "css", "react")):
        return "web"
    return "unknown"


def complexity_score(prompt: str) -> int:
    """Quick heuristic score of prompt complexity (higher = more complex)."""
    # count clauses and tokens
    clauses = re.split(r'[.;\n]', prompt)
    tokens = prompt.split()
    score = len(clauses) + len(tokens) // 20
    # bonus for many distinct requirements words
    for kw in ("auth", "database", "realtime", "deploy", "payment"):
        if kw in prompt.lower():
            score += 3
    return score


def extract_intent(prompt: str) -> Dict[str, Any]:
    """Extract a small structured intent from a freeform prompt.

    Returns a dict like:
      { 'app_type': 'api', 'features': [...], 'requirements': '...' }
    """
    app_type = classify_prompt(prompt)
    features = []
    reqs = []

    # simple patterns
    if re.search(r"get\s+weather|weather\s+api", prompt, re.I):
        features.append('get_weather')
        reqs.append('GET /weather - returns JSON {"temp": number, "condition": string}')

    # extract mentions of routes like GET /foo or POST /bar
    routes = re.findall(r"(GET|POST|PUT|DELETE)\s+(/[\w/{}-]*)", prompt, re.I)
    for m in routes:
        reqs.append(f"{m[0].upper()} {m[1]}")

    # fallback: if the prompt mentions 'login' or 'auth'
    if re.search(r"login|auth|authenticate", prompt, re.I):
        features.append('auth')

    return {'app_type': app_type, 'features': features, 'requirements': reqs, 'raw': prompt}


def normalize_prompt(prompt: str, max_complexity: int = 6) -> Tuple[bool, Dict[str, Any]]:
    """Normalize prompt; returns (ok, payload).

    If not ok, payload contains 'reason' indicating split/ rejection.
    """
    score = complexity_score(prompt)
    if score > max_complexity:
        return False, {'reason': 'too_complex', 'score': score}

    intent = extract_intent(prompt)
    # ensure minimal structure
    if not intent['requirements'] and intent['app_type'] == 'api':
        # try to infer a basic route
        intent['requirements'].append('GET /health - returns 200')

    return True, intent


if __name__ == '__main__':
    print('prompt_normalizer: import and call normalize_prompt(prompt)')
