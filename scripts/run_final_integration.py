"""Run a final cross-prompt integration set and write a summary report.
"""
from pipeline import run_phased_builder
import json
import os
import time

prompts = [
    'Build a weather API that returns temperature and condition via GET /weather',
    'Build an items API with POST /items and GET /items',
    'Build a simple todo API with POST /todos and GET /todos',
]

OUT = os.path.join(os.path.dirname(__file__), 'test_reports')
os.makedirs(OUT, exist_ok=True)

summary = {'runs': [], 'timestamp': int(time.time())}

for p in prompts:
    rc = run_phased_builder(p)
    summary['runs'].append({'prompt': p, 'rc': rc})

path = os.path.join(OUT, f'final_integration_{int(time.time())}.json')
with open(path, 'w', encoding='utf-8') as fh:
    json.dump(summary, fh, indent=2)

print('Wrote final integration summary to', path)
