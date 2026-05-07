"""Structured world event helper for 3D build simulator integration."""
import time


def emit_world_event(event_type, payload):
    """Return a structured event dict for forwarding to the 3D client.

    Schema:
        {"type": <EVENT_TYPE>, "payload": {...}, "timestamp": <unix>}
    """
    return {
        "type": event_type,
        "payload": payload or {},
        "timestamp": time.time(),
    }
