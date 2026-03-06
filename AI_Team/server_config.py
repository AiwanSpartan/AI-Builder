"""Configuration constants for the AI Office Builder backend."""

import os

BASE_DIR = r"C:\Users\aiwan\Documents\AI-Builder"
BUILDS_DIR = os.path.join(BASE_DIR, "Builds")
os.makedirs(BUILDS_DIR, exist_ok=True)

LATEST_OUTPUT_FILE = os.path.join(BUILDS_DIR, "built_app.py")
LATEST_PROJECT_FILE = os.path.join(BUILDS_DIR, "latest_project.txt")
LATEST_ZIP_FILE = os.path.join(BUILDS_DIR, "latest_build.zip")

MODELS = {
    "architect": "deepseek-r1:7b",
    "coder": "qwen2.5-coder:7b",
    "debugger": "deepseek-coder:6.7b",
    "tester": "qwen2.5-coder:7b",
    "reviewer": "mistral:7b",
}

MAX_DEBUG_FIX_ATTEMPTS = 5
MAX_REBUILD_ATTEMPTS = 2
MAX_AUTO_REPAIR_ATTEMPTS = 2

DEFAULT_AGENT_TIMEOUT_SECONDS = 180
REPAIR_CALL_TIMEOUT_SECONDS = 120
AGENT_TIMEOUT_SECONDS = {
    "architect": 180,
    "coder": 240,
    "debugger": 180,
    "tester": 90,
    "reviewer": 90,
}
