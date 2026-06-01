"""Gradio frontend for the Formation agent.

Usage:
  set PYTHONPATH=backend/src
  set AGRO_API_URL=http://127.0.0.1:8000/formation   # optional
  python frontend/gradio_ui.py

If `AGRO_API_URL` is set the UI will POST to the remote API; otherwise it
will instantiate a local `FormationAgro` and call it directly.
"""
import json
import os
import logging
import gradio as gr
import requests
import sys
import asyncio

# ensure backend package is importable when running the UI locally
ROOT_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend", "src"))
if ROOT_BACKEND not in sys.path:
    sys.path.insert(0, ROOT_BACKEND)
# Allow local runs without MCP/Shield by setting bypass flag
os.environ.setdefault("FORMATION_LOCAL_NO_SHIELD", "1")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gradio.agro")

DEFAULT_PROFILE = {"niveau": "débutant", "région": "Boucle du Mouhoun"}


def generate_answer(question: str, profile_text: str) -> str:
    question = (question or "").strip()
    if not question:
        return "Posez une question valide."

    # Parse profile JSON (fallback to default)
    try:
        profile = json.loads(profile_text) if profile_text else dict(DEFAULT_PROFILE)
    except Exception as e:
        logger.warning("Invalid profile JSON: %s", e)
        profile = dict(DEFAULT_PROFILE)

    api_url = os.environ.get("AGRO_API_URL")
    if api_url:
        try:
            resp = requests.post(api_url, json={"question": question, "profile": profile}, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("answer") or json.dumps(data, ensure_ascii=False)
            return f"HTTP {resp.status_code}: {resp.text}"
        except Exception as exc:
            logger.exception("Remote API call failed: %s", exc)
            return f"Erreur HTTP: {exc}"

    # Local mode: invoke Formation graph directly using the compiled graph (advisor-first)
    try:
        from agriconnect.graphs.agents.formation.graph import FormationCoach

        coach = FormationCoach.from_config()
        context = profile
        # try sync handle first, fall back to async run
        try:
            out = coach.handle(question, context)
        except Exception:
            out = asyncio.run(coach.run(question, context))

        # `out` is a dict with standardized keys
        if isinstance(out, dict):
            resp_text = out.get("full_text") or (out.get("structured_data") and json.dumps(out.get("structured_data"), ensure_ascii=False)) or str(out)
            return resp_text
        return str(out)
    except Exception as exc:
        logger.exception("Local Formation graph invocation failed: %s", exc)
        return f"Erreur locale: {exc}"


def build_ui():
    with gr.Blocks(title="AgriConnect — Formation") as demo:
        gr.Markdown("# AgriConnect — Formation (Gradio UI)")
        with gr.Row():
            q = gr.Textbox(label="Question", placeholder="Posez votre question agricole ici...", lines=2)
        with gr.Row():
            p = gr.Textbox(label="Profil (JSON)", value=json.dumps(DEFAULT_PROFILE, ensure_ascii=False), lines=4)
        with gr.Row():
            btn = gr.Button("Soumettre")
        out = gr.Textbox(label="Réponse", lines=20)

        btn.click(fn=generate_answer, inputs=[q, p], outputs=[out])

    return demo


if __name__ == "__main__":
    demo = build_ui()
    # Default host/port — adjust as needed
    port = int(os.environ.get("GRADIO_PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port, share=False)
