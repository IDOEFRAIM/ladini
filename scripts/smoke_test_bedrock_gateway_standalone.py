"""Smoke test MINIMAL, autonome, du gateway Bedrock (OpenAI-compatible) —
ne dépend QUE du package `openai` (déjà installé partout où l'app tourne),
jamais de `ladini.core.*` (évite tout problème de version d'image/venv
désynchronisée avec le code applicatif).

Lit directement les variables d'environnement du process qui l'exécute :
    OPENAI_BASE_URL, OPENAI_API_KEY  (passerelle Bedrock)

Lance-le LÀ OÙ ces variables sont réellement injectées pour l'app — par ex.
à l'intérieur du conteneur `api` en prod :

    docker compose -f docker-compose.prod.yml cp \
        scripts/smoke_test_bedrock_gateway_standalone.py api:/tmp/smoke.py
    docker compose -f docker-compose.prod.yml exec api python3 /tmp/smoke.py

Ou, si `openai` est installé dans un venv/poetry local qui a AUSSI les
variables (ex: un shell où tu as fait `export $(cat .env | xargs)`) :

    python3 scripts/smoke_test_bedrock_gateway_standalone.py

N'affiche JAMAIS la clé — seulement host/modèle/statut/erreur.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

# Les 3 model id Bedrock actuellement configurés dans ce repo
# (core/settings.py) — testés tels quels, jamais devinés.
MODELS_TO_TEST = [
    "qwen.qwen3-32b",
    "deepseek.v3.2",
    "openai.gpt-oss-120b",
]


def main() -> int:
    base_url = os.environ.get("OPENAI_BASE_URL", "")
    api_key = os.environ.get("OPENAI_API_KEY", "")

    print("=" * 70)
    print("Configuration lue depuis l'environnement de CE process")
    print("=" * 70)
    if not base_url:
        print("  ✗ OPENAI_BASE_URL est VIDE dans cet environnement.")
        print("    Le SDK openai visera l'API OpenAI publique par défaut.")
    else:
        parsed = urlparse(base_url)
        print(f"  OPENAI_BASE_URL -> host={parsed.hostname!r} scheme={parsed.scheme!r}")
        if parsed.hostname == "api.openai.com":
            print("  ⚠ Ceci est l'API OpenAI PUBLIQUE, pas une passerelle Bedrock.")
    print(f"  OPENAI_API_KEY  -> {'définie' if api_key else 'VIDE'} (jamais affichée)")

    if not base_url or not api_key:
        print()
        print("Arrêt : impossible de tester sans OPENAI_BASE_URL et OPENAI_API_KEY")
        print("définies dans CET environnement (celui qui exécute ce script).")
        return 1

    try:
        from openai import OpenAI
    except ImportError:
        print()
        print("✗ Le package `openai` n'est pas installé dans cet environnement.")
        print("  pip install openai   (ou lance ce script là où l'app tourne réellement)")
        return 1

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=15.0)

    print()
    print("=" * 70)
    print("Appel minimal par modèle Bedrock configuré")
    print("=" * 70)
    for model in MODELS_TO_TEST:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Réponds seulement OK"}],
                temperature=0.0,
                max_tokens=20,
            )
            content = resp.choices[0].message.content
            print(f"  ✓ {model:<25} | 200 OK | réponse={content!r}")
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            print(
                f"  ✗ {model:<25} | {type(exc).__name__}"
                f"{f' (status_code={status_code})' if status_code is not None else ''}: {exc}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
