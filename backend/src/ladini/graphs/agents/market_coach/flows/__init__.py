"""Domaines métier du MarketCoach (Buyer / Producer).

Chaque sous-package isole les contextes typés, les flows LangGraph et les
actions DB/MCP propres à son rôle. Le `core/` orchestrateur consomme leurs
fonctions de haut niveau, jamais leur tripes.
"""
