"""Application state and websocket broadcasting helpers."""

import json
import time

from flask import Flask
from flask_cors import CORS
from flask_sock import Sock

app = Flask(__name__)
CORS(app)
sock = Sock(app)

clients = []


def _initial_agents_state():
    return {
        "architect": {"state": "idle", "message": "Waiting for a task...", "progress": 0},
        "coder": {"state": "idle", "message": "Waiting for a task...", "progress": 0},
        "debugger": {"state": "idle", "message": "Waiting for a task...", "progress": 0},
        "tester": {"state": "idle", "message": "Waiting for a task...", "progress": 0},
        "reviewer": {"state": "idle", "message": "Waiting for a task...", "progress": 0},
    }


build_state = {
    "status": "idle",
    "agents": _initial_agents_state(),
    "output": {"plan": "", "code": "", "tests": "", "review": ""},
    "log": [],
}


def broadcast(event, data):
    """Send an event to all connected websocket clients."""
    message = json.dumps({"event": event, "data": data})
    dead = []
    for ws in clients:
        try:
            ws.send(message)
        except Exception:
            dead.append(ws)

    for ws in dead:
        if ws in clients:
            clients.remove(ws)


def log(msg):
    """Append an entry to the in-memory activity log and broadcast it."""
    entry = {"time": time.strftime("%H:%M:%S"), "msg": msg}
    build_state["log"].append(entry)
    broadcast("log", entry)


def set_agent(name, state, message, progress=None):
    """Update one agent's state and emit a websocket update."""
    build_state["agents"][name]["state"] = state
    build_state["agents"][name]["message"] = message
    if progress is not None:
        build_state["agents"][name]["progress"] = progress

    broadcast("agent_update", {"name": name, **build_state["agents"][name]})
    log(f"[{name.upper()}] {message}")


def reset_build_state_for_new_request():
    """Reset progress/output sections before a new build request starts."""
    build_state["agents"] = {
        name: {"state": "idle", "message": "Waiting...", "progress": 0}
        for name in build_state["agents"].keys()
    }
    build_state["log"] = []
    build_state["output"] = {"plan": "", "code": "", "tests": "", "review": ""}
