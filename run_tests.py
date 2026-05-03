"""Run test suite locally: execute `rv_test.py` and `repair_loop.py`,
capture their outputs to `test_reports/` and exit non-zero if failures.
"""
import os
import subprocess
import time

OUT_DIR = os.path.join(os.path.dirname(__file__), 'test_reports')
os.makedirs(OUT_DIR, exist_ok=True)


def run_cmd(cmd, out_path):
    with open(out_path, 'wb') as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, shell=True)
        proc.wait()
        return proc.returncode


def main():
    ts = int(time.time())
    rv_report = os.path.join(OUT_DIR, f'rv_test_{ts}.txt')
    repair_report = os.path.join(OUT_DIR, f'repair_loop_{ts}.txt')
    pipeline_report = os.path.join(OUT_DIR, f'pipeline_test_{ts}.txt')

    print('Running rv_test.py...')
    rc1 = run_cmd(f'c:/Users/aiwan/Documents/AI-Builder/.venv/Scripts/python.exe rv_test.py', rv_report)
    print('rv_test.py exit code', rc1)

    print('Running repair_loop.py...')
    rc2 = run_cmd(f'c:/Users/aiwan/Documents/AI-Builder/.venv/Scripts/python.exe repair_loop.py', repair_report)
    print('repair_loop.py exit code', rc2)

    print('Running test_pipeline.py...')
    rc3 = run_cmd(f'c:/Users/aiwan/Documents/AI-Builder/.venv/Scripts/python.exe test_pipeline.py', pipeline_report)
    print('test_pipeline.py exit code', rc3)

    if rc1 != 0 or rc2 != 0 or rc3 != 0:
        print('One or more tests failed. Reports are in', OUT_DIR)
        raise SystemExit(2)

    print('All tests passed. Reports are in', OUT_DIR)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
