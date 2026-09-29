"""Espace COMMERCIAL — relances manuelles WhatsApp depuis le dashboard.

Voir `admin_api.py` pour le contrat complet. Point non négociable : ce
paquet ne doit JAMAIS importer `ladini.workspace` (WorkspaceStore /
WorkspaceCheckpointer), `ladini.api.tasks` ni rien sous `ladini.graphs` —
un message commercial est un envoi WhatsApp direct, jamais une entrée
LangGraph. Vérifié par
`tests/architecture/test_commercial_does_not_touch_langgraph.py`.
"""
