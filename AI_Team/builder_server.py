"""AI Office Builder backend launcher.

Runtime behavior is split across dedicated modules in AI_Team:
- server_config.py
- server_state.py
- server_io.py
- server_ai.py
- server_pipeline.py
- server_personas.py
- server_routes.py
"""

from server_state import app
import server_routes  # noqa: F401  # Registers Flask and websocket routes.


if __name__ == '__main__':
    print("🏢 AI Office Server starting on http://localhost:5000")
    app.run(debug=False, port=5000, threaded=True)
