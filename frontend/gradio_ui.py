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

    api_url = "http://127.0.0.1:8000/formation"
    if not api_url:
        return (
            "AGRO_API_URL not set. This UI only forwards requests to the API.\n"
            "Set environment variable AGRO_API_URL=http://127.0.0.1:8000/formation"
        )

    try:
        resp = requests.post(api_url, json={"question": question, "profile": profile}, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("answer") or json.dumps(data, ensure_ascii=False)
        return f"HTTP {resp.status_code}: {resp.text}"
    except Exception as exc:
        logger.exception("Remote API call failed: %s", exc)
        return f"Erreur HTTP: {exc}"


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
