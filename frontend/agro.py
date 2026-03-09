"""
frontend/agro.py — Interactive CLI to test the Formation agent with an agronome.

Runs a REPL loop: the agronome types a question and gets a full answer
produced by the LangGraph workflow (analyze → retrieve → compose → critique).

Usage:
    set PYTHONPATH=backend/src && python frontend/agro.py
"""
import json
import logging
import os
import sys
from typing import Optional

import requests

from agriconnect.agents.formation_agro import FormationAgro

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-28s %(levelname)-5s %(message)s",
)

DEFAULT_PROFILE = {"niveau": "débutant", "région": "Boucle du Mouhoun"}


def _read_profile() -> dict:
    """Optionally let the user set a learner profile at startup."""
    print("\nProfil apprenant (vide = défaut) :")
    print(f"  Défaut : {json.dumps(DEFAULT_PROFILE, ensure_ascii=False)}")
    raw = input("  JSON profil (Enter pour défaut) : ").strip()
    if not raw:
        return dict(DEFAULT_PROFILE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("  ⚠️  JSON invalide, profil par défaut utilisé.")
        return dict(DEFAULT_PROFILE)


def run():
    print("=" * 60)
    print("   FRONTEND AGRO — Test formation agent (agronome)")
    print("=" * 60)

    profile = _read_profile()

    # Determine whether to use remote HTTP API or local agent
    api_url = os.environ.get("AGRO_API_URL")
    use_api = bool(api_url)
    agent: Optional[FormationAgro] = None
    if not use_api:
        agent = FormationAgro(learner_profile=profile)
    else:
        print(f"Using remote API at {api_url}")

    print(f"\n  Profil actif : {json.dumps(profile, ensure_ascii=False)}")
    print("  Tapez 'quit' pour quitter, 'profile' pour changer de profil.\n")

    while True:
        try:
            q = input("🌱 Question > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAu revoir !")
            break

        if not q:
            continue
        if q.lower() in ("quit", "exit", "q"):
            print("Au revoir !")
            break
        if q.lower() == "profile":
            profile = _read_profile()
            agent.default_profile = profile
            print(f"  Profil mis à jour : {json.dumps(profile, ensure_ascii=False)}\n")
            continue
        if use_api:
            try:
                resp = requests.post(
                    api_url,
                    json={"question": q, "profile": profile},
                    timeout=60,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    answer = payload.get("answer") or json.dumps(payload, ensure_ascii=False)
                else:
                    answer = f"HTTP {resp.status_code}: {resp.text}"
            except Exception as exc:
                answer = f"Erreur HTTP: {exc} — bascule en local si possible."
                # Try local fallback
                try:
                    if agent is None:
                        agent = FormationAgro(learner_profile=profile)
                    answer = agent.ask(q, learner_profile=profile)
                except Exception as exc2:
                    answer = f"Fallback failed: {exc2}"
        else:
            answer = agent.ask(q, learner_profile=profile)
        print(f"\n{'─'*60}")
        print(answer)
        print(f"{'─'*60}\n")


if __name__ == "__main__":
    run()
