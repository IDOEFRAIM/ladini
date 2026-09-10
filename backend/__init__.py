"""
Backend src Ladini — Architecture Monolithique Modulaire.

Couches (de bas en haut) :
  core/          → Fondations (settings, database, logger, security)
  services/      → Accès externe (LLM, DB handler, Azure TTS, scraper)
  tools/         → Outils métier (météo, marché, sol, santé, culture)
  agents/        → Agents IA (LangGraph sub-graphs)
  orchestrator/  → Flux principal (analyse → agents → audio → persist)
  rag/           → Recherche vectorielle (FAISS + LlamaIndex)
  api/           → Routes HTTP (FastAPI)
  workers/       → Tâches Celery async
  main.py        → Point d'entrée FastAPI (lifecycle, middlewares, routes)

Règle d'import : chaque couche n'importe que les couches en-dessous.
"""
