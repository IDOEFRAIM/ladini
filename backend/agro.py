"""
backend/agro.py — Backend test runner for the Formation agent.

Runs the full LangGraph workflow (analyze → retrieve → grade → compose →
critique → evaluate) using the RAG retriever directly — no MCP, no A2A.

Usage:
    # Single question (default profile)
    set PYTHONPATH=backend/src && python backend/agro.py

    # Custom question
    set PYTHONPATH=backend/src && python backend/agro.py "Comment traiter le mildiou ?"

    # With learner profile JSON
    set PYTHONPATH=backend/src && python backend/agro.py --profile '{"niveau":"expert","region":"Sahel"}'
"""
import json
import logging
import sys
from agriconnect.agents.formation_agro import FormationAgro

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-28s %(levelname)-5s %(message)s",
)

DEFAULT_PROFILE = {"niveau": "débutant", "région": "Boucle du Mouhoun"}

SAMPLE_QUESTIONS = [
    "Comment faire la rotation des cultures au Burkina ?",
    "Qu'est-ce que le compostage en tas ?",
    "Quand semer le sorgho en zone sahélienne ?",
]


def main():
    profile = dict(DEFAULT_PROFILE)
    questions: list[str] = []

    # Simple arg parsing
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--profile" and i + 1 < len(args):
            try:
                profile = json.loads(args[i + 1])
            except json.JSONDecodeError as e:
                print(f"⚠️  Invalid JSON for --profile: {e}", file=sys.stderr)
            i += 2
        else:
            questions.append(args[i])
            i += 1

    # Join positional args into a single question string
    # (handles shell splitting: python agro.py Comment faire la rotation …)
    if questions:
        questions = [" ".join(questions)]
    else:
        questions = SAMPLE_QUESTIONS

    agent = FormationAgro(learner_profile=profile)

    for idx, q in enumerate(questions, 1):
        print(f"\n{'='*60}")
        print(f"  TEST {idx}/{len(questions)}")
        print(f"  Q: {q}")
        print(f"  Profile: {json.dumps(profile, ensure_ascii=False)}")
        print(f"{'='*60}\n")
        answer = agent.ask(q, learner_profile=profile)
        print(answer)
        print()


if __name__ == "__main__":
    main()
