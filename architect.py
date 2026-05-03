"""Architect expectation loader.

Provides a simple helper to load an Architect spec from a file and return
the parsed schema using `response_validator.parse_architect`.
"""
from typing import List, Dict, Any
import os

from response_validator import parse_architect


def load_architect(path: str) -> List[Dict[str, Any]]:
    """Load architect text from `path` and parse into schema.

    If the file does not exist, raises FileNotFoundError.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    return parse_architect(text)


if __name__ == '__main__':
    print('Use load_architect(path) to parse an Architect file')
