"""Invariant du feature COMMERCIAL (relances manuelles) : un message envoyé
par un commercial est un envoi WhatsApp SORTANT — il ne doit jamais pouvoir
devenir une entrée LangGraph, ni muter l'état conversationnel d'un
utilisateur (`agri_workspaces.langgraph_state`).

Concrètement, `ladini.services.commercial` et `ladini.api.routes.commercial_admin`
ne doivent JAMAIS importer :

  - `ladini.workspace.store.WorkspaceStore` en ÉCRITURE (le module peut lire
    via `WorkspaceStore().get(...)`, jamais appeler `.save()`) ;
  - `ladini.workspace.checkpointer` (le checkpointer LangGraph) ;
  - `ladini.api.tasks` (la tâche Celery `process_agent_task` qui pilote le
    graphe) ;
  - quoi que ce soit sous `ladini.graphs` (orchestration conversationnelle,
    interpreter NEW_TASK/ANSWER/DEVIATION).

Ce test échoue si l'une de ces dépendances est réintroduite, ou si le code
appelle `WorkspaceStore().save(...)` (grep AST sur le nom d'attribut `save`
combiné à une instance de `WorkspaceStore` serait trop fragile — on vérifie
plus simplement qu'aucun import ne rend cet appel possible depuis ce module,
et qu'aucun appel littéral `.save(` n'apparaît dans le fichier).
"""

from __future__ import annotations

import ast
import os

import pytest

SRC = os.path.join("src", "ladini")

COMMERCIAL_FILES = (
    os.path.join(SRC, "services", "commercial", "admin_api.py"),
    os.path.join(SRC, "api", "routes", "commercial_admin.py"),
)

FORBIDDEN_MODULES = (
    "ladini.workspace.checkpointer",
    "ladini.api.tasks",
    "ladini.graphs",
)


def _imported_modules(path: str) -> list[str]:
    tree = ast.parse(open(path, encoding="utf-8").read())
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
    return out


@pytest.mark.architecture
@pytest.mark.parametrize("path", COMMERCIAL_FILES)
def test_commercial_module_never_imports_langgraph_entrypoints(path):
    imported = _imported_modules(path)
    offenders = [m for m in imported if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_MODULES)]
    assert not offenders, f"{path} importe un point d'entrée LangGraph interdit : {offenders}"


@pytest.mark.architecture
@pytest.mark.parametrize("path", COMMERCIAL_FILES)
def test_commercial_module_never_calls_workspace_save(path):
    """`WorkspaceStore` peut être lu (`.get(...)`) mais jamais écrit
    (`.save(...)`) depuis le chemin de relance commerciale — sinon un message
    commercial pourrait finir par muter `active_goal`/`active_form`/
    `langgraph_state` comme un tour d'agent normal.

    Analyse l'AST (pas le texte brut) pour ignorer les mentions de
    `.save()` dans les docstrings/commentaires et ne repérer qu'un VRAI
    appel `x.save(...)` dans le code exécutable.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    offenders = [
        ast.dump(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "save"
    ]
    assert not offenders, (
        f"{path} appelle .save(...) — un message commercial ne doit jamais "
        "écrire dans WorkspaceStore (risque de corruption du tunnel LangGraph)."
    )


@pytest.mark.architecture
def test_send_follow_up_only_talks_to_whatsapp_channel_and_audit_log():
    """Documente et verrouille le mécanisme d'envoi : `WhatsAppChannel.send()`
    directement, jamais `NotificationOutbox`/dispatcher (qui serait
    asynchrone et ne garantirait pas l'immédiateté attendue du MVP), jamais
    un import de `process_agent_task`."""
    from ladini.services.commercial import admin_api

    assert admin_api.WhatsAppChannel.__module__ == "ladini.workers.outbox.channels.whatsapp"
    assert "process_agent_task" not in dir(admin_api)
