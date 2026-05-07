"""Application state and websocket broadcasting helpers."""

import collections
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

# Short rolling buffer of human-readable build events the NPCs can reference
# in chat. Capped so older items get evicted automatically — the goal is
# "what just happened?" awareness, not a permanent history.
_RECENT_EVENTS_MAX = 8
_recent_events = collections.deque(maxlen=_RECENT_EVENTS_MAX)


def record_event(text):
    """Append a kid-friendly description of something that just happened in
    the build pipeline (e.g. "Bob finished the planning"). Used by the
    NPC chat layer so workers can answer "what's going on?" with real,
    pipeline-aware context instead of generic chatter."""
    if not text:
        return
    clean = " ".join(str(text).split())[:160]
    if not clean:
        return
    _recent_events.append({"time": time.strftime("%H:%M:%S"), "text": clean})


def get_recent_events(limit=5):
    """Return the most recent events, newest last, as a list of dicts."""
    if limit <= 0:
        return []
    items = list(_recent_events)
    return items[-limit:]


def clear_recent_events():
    _recent_events.clear()


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
    # Clear any stale ETA info from a previous build so a reconnect during
    # idle doesn't show a stale countdown.
    build_state.pop("started_at", None)
    build_state.pop("eta_seconds", None)
    build_state.pop("modules", None)
    build_state.pop("recommendations", None)
    build_state.pop("first_error", None)
    # Fresh build → fresh event memory so NPCs don't reference last build's history.
    clear_recent_events()
