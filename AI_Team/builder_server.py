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
import threading
import ollama
from server_config import MODELS, NPC_CHAT_MODEL


def _prewarm_npc_model():
    """Load the NPC chat model into GPU memory at startup so first chat is instant."""
    try:
        print(f"[NPC] Pre-warming {NPC_CHAT_MODEL}...")
        ollama.chat(
            model=NPC_CHAT_MODEL,
            messages=[{'role': 'user', 'content': 'hi'}],
            options={'num_predict': 1},
            keep_alive='10m',
        )
        print(f"[NPC] {NPC_CHAT_MODEL} ready.")
    except Exception as e:
        print(f"[NPC] Pre-warm failed (Ollama not running yet?): {e}")


def _prewarm_build_models():
    """Pre-warm each model used in the build pipeline so the first build doesn't
    spend its timeout budget on cold-loading models from disk into GPU memory."""
    seen = set()
    for role in ("architect", "coder", "debugger", "tester", "reviewer"):
        model = MODELS.get(role)
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            print(f"[BUILD] Pre-warming {role} → {model}...")
            ollama.chat(
                model=model,
                messages=[{'role': 'user', 'content': 'ok'}],
                options={'num_predict': 1},
                keep_alive='15m',
            )
            print(f"[BUILD] {model} ready.")
        except Exception as e:
            print(f"[BUILD] Pre-warm failed for {model}: {e}")


if __name__ == '__main__':
    print("🏢 AI Office Server starting on http://localhost:5000")
    threading.Thread(target=_prewarm_npc_model, daemon=True).start()
    threading.Thread(target=_prewarm_build_models, daemon=True).start()
    app.run(debug=False, port=5000, threaded=True)
