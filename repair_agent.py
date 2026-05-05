"""Repair agent helper.

Reads prompts saved by `repair_loop.py` (in `repair_prompts/`) and prepares
LLM-ready suggestion files in `repair_suggestions/`.

If the environment variable `OPENAI_API_KEY` is set, the script will attempt
to call the OpenAI Chat Completions API to get a suggested repair. This is
optional; the script will still produce suggestion files even without the key.

Usage:
    python repair_agent.py
    OPENAI_API_KEY=... python repair_agent.py
"""
import os
import json
import glob
import time
from typing import List

import requests


PROMPT_DIR = os.path.join(os.path.dirname(__file__), 'repair_prompts')
OUT_DIR = os.path.join(os.path.dirname(__file__), 'repair_suggestions')
HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'repair_history.json')


def discover_prompts(dir_path: str = PROMPT_DIR) -> List[str]:
    if not os.path.isdir(dir_path):
        return []
    return sorted(glob.glob(os.path.join(dir_path, '*.txt')))


def prepare_messages(prompt_text: str) -> List[dict]:
    system = (
        "You are an expert software engineer tasked with producing a minimal, safe, "
        "and targeted repair for a failing HTTP endpoint based on the debug prompt below. "
        "Return a short plan and, when possible, a precise unified diff or file patch."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt_text},
    ]


def load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_history(h: dict):
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as fh:
            json.dump(h, fh, indent=2)
    except Exception:
        pass


def fingerprint_prompt(text: str) -> str:
    # lightweight fingerprint: normalize whitespace and take SHA1
    import hashlib
    s = ' '.join(text.split()).strip().lower()
    return hashlib.sha1(s.encode('utf-8')).hexdigest()


def _normalize_text(s: str) -> str:
    return ' '.join(s.split()).strip().lower()


def find_similar_in_history(text: str, threshold: float = 0.75):
    """Return (fp, entry, score) for the best matching historic prompt above threshold, else (None, None, 0).

    Uses difflib.SequenceMatcher for a quick similarity check on normalized prompt texts.
    """
    import difflib
    hist = load_history()
    if not hist:
        return None, None, 0.0
    norm = _normalize_text(text)
    best_fp = None
    best_score = 0.0
    best_entry = None
    for fp, entry in hist.items():
        orig = entry.get('prompt_text') or entry.get('prompt') or ''
        score = difflib.SequenceMatcher(None, norm, _normalize_text(orig)).ratio()
        if score > best_score:
            best_score = score
            best_fp = fp
            best_entry = entry

    if best_score >= threshold:
        return best_fp, best_entry, best_score
    return None, None, best_score


def call_openai_chat(messages: List[dict], model: str = None) -> str:
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY not set')
    model = model or os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')
    url = 'https://api.openai.com/v1/chat/completions'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {
        'model': model,
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': 1500,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    # extraction depends on API shape
    try:
        return data['choices'][0]['message']['content']
    except Exception:
        return json.dumps(data)


def process_prompts():
    files = discover_prompts()
    os.makedirs(OUT_DIR, exist_ok=True)
    if not files:
        print('No prompts found in', PROMPT_DIR)
        return []

    suggestions = []
    for i, fpath in enumerate(files, start=1):
        with open(fpath, 'r', encoding='utf-8') as fh:
            txt = fh.read()

        msg = prepare_messages(txt)
        suggestion = {'prompt_file': os.path.basename(fpath), 'prompt': txt, 'suggestion': None, 'created_at': int(time.time())}

        # attempt to reuse history: exact fingerprint first, then fuzzy match
        history = load_history()
        fp = fingerprint_prompt(txt)
        reused = False
        if fp in history:
            prev = history[fp]
            print('Found historic suggestion for', fpath, '- reusing (exact)')
            suggestion['suggestion'] = prev.get('suggestion')
            suggestion['model_used'] = prev.get('model_used')
            suggestion['reused_from'] = prev.get('created_at')
            reused = True
        else:
            # fuzzy match similar prompts in history
            sim_fp, sim_entry, score = find_similar_in_history(txt, threshold=0.72)
            if sim_fp:
                print(f'Found similar historic suggestion for {fpath} (score={score:.2f}) - reusing')
                suggestion['suggestion'] = sim_entry.get('suggestion')
                suggestion['model_used'] = sim_entry.get('model_used')
                suggestion['reused_from'] = sim_entry.get('created_at')
                suggestion['reused_similarity'] = score
                reused = True

        if reused:
            ts = suggestion['created_at']
            out_name = os.path.join(OUT_DIR, f'suggestion_{ts}_{i}.json')
            with open(out_name, 'w', encoding='utf-8') as out_f:
                json.dump(suggestion, out_f, indent=2)
            suggestions.append(out_name)
            continue

        # try LLM if key present
        if os.environ.get('OPENAI_API_KEY'):
            try:
                print('Calling OpenAI for', fpath)
                out = call_openai_chat(msg)
                suggestion['suggestion'] = out
                suggestion['model_used'] = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')
                # persist to history for future reuse
                try:
                    history = load_history()
                    fp = fingerprint_prompt(txt)
                    history[fp] = {'prompt_text': txt, 'suggestion': out, 'model_used': suggestion['model_used'], 'created_at': suggestion['created_at']}
                    save_history(history)
                except Exception:
                    pass
            except Exception as e:
                suggestion['error'] = str(e)
                print('OpenAI call failed:', e)

        # write suggestion file
        ts = suggestion['created_at']
        out_name = os.path.join(OUT_DIR, f'suggestion_{ts}_{i}.json')
        with open(out_name, 'w', encoding='utf-8') as out_f:
            json.dump(suggestion, out_f, indent=2)
        suggestions.append(out_name)

    print(f'Wrote {len(suggestions)} suggestion(s) to', OUT_DIR)
    return suggestions


if __name__ == '__main__':
    process_prompts()
