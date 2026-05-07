"""Prune `repair_history.json` to keep size and age under control.

Usage:
  python prune_history.py --max-entries 200 --max-age-days 365
"""
import argparse
import time
import os
import json

HISTORY = os.path.join(os.path.dirname(__file__), 'repair_history.json')


def load_history():
    if not os.path.exists(HISTORY):
        return {}
    try:
        with open(HISTORY, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_history(h):
    try:
        with open(HISTORY, 'w', encoding='utf-8') as fh:
            json.dump(h, fh, indent=2)
    except Exception:
        pass


def prune(max_entries: int, max_age_days: int):
    h = load_history()
    items = []
    for fp, entry in h.items():
        created = entry.get('created_at', 0)
        items.append((fp, created, entry))

    items.sort(key=lambda t: t[1], reverse=True)

    cutoff = time.time() - max_age_days * 24 * 3600
    kept = {}
    for i, (fp, created, entry) in enumerate(items):
        if i < max_entries and created >= cutoff:
            kept[fp] = entry

    save_history(kept)
    print(f'Pruned history: kept {len(kept)} entries (max_entries={max_entries}, max_age_days={max_age_days})')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--max-entries', type=int, default=200)
    p.add_argument('--max-age-days', type=int, default=365)
    args = p.parse_args()
    prune(args.max_entries, args.max_age_days)
