"""Simple smoke test for the phased builder.

Runs `run_phased_builder()` with a canonical weather prompt and exits
non-zero if the builder/validator reports failure.
"""
import sys

from pipeline import run_phased_builder


def main():
    prompt = 'Build a weather API that returns temperature and condition via GET /weather'
    rc = run_phased_builder(prompt, base_url='http://localhost:8000')
    print('PHASED_BUILDER_TEST_EXIT_CODE', rc)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
