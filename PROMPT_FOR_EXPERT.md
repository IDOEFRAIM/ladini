# Sujet : Refactoring de l'implémentation MCP (Model Context Protocol) pour AgriConnect

**Objectif :** Robustesse, Sécurité et Scalabilité de l'architecture Agent/Outils.

---

## 1. Contexte Technique
*   **Stack :** Python 3.10+, FastAPI (Asynchrone), Pydantic v2 (Validation stricte).
*   **Architecture Actuelle :**
    *   **Gateway / Hub (ShieldHub) :** Un point d'entrée central (FastAPI) qui reçoit les requêtes HTTP des agents et les route.
    *   **Serveurs MCP :**
        1.  `RAG_SERVER` : Gère la recherche vectorielle (AgileRetriever, Redis). Implémenté in-process via `FastMCP`.
        2.  `DB_SERVER` : Gère la persistance et les actions métier (AgriDatabaseService). Implémenté in-process via `FastMCP`.
    *   **Sécurité :** Un client `MCPPermissionClient` (le "Shield") qui intercepte chaque appel pour valider les scopes (`TOOL_SCOPE_MAP`) et les risques (`TOOL_RISK_MAP`).

## 2. Problèmes Actuels & Points de Douleur
1.  **Instabilité des Permissions (Fail-Closed) :**
    *   Le système rejette par défaut tout outil non explicitement mappé (`PermissionDenied`).
    *   La maintenance des listes `TOOL_SCOPE_MAP` est manuelle et sujette à l'oubli, provoquant des erreurs 500 lors de l'ajout de nouveaux outils.
2.  **Gestion de Session & Concurrence :**
    *   Les appels aux serveurs in-process (`agri_rag_server`, `agri_db_server`) souffrent parfois de conflits d'Event Loop (`There is no current event loop in thread...`), obligant à des hacks (création de loop par appel) qui ne sont pas scalables sous charge.
    *   Pas de véritable pool de connexions ou de gestion de cycle de vie robuste pour les serveurs MCP.
3.  **Découverte des Outils :**
    *   La découverte repose sur des hacks (import de `TOOL_MAP` global ou introspection fragile) plutôt qu'un protocole standardisé de "Discovery" dynamique.

## 3. Objectifs de la Mission de Refactoring

### A. Standardisation du Registry d'Outils
Proposer une architecture de configuration centralisée (ex: `mcp_registry.py` avec Pydantic Settings) qui :
*   Charge dynamiquement les outils disponibles au démarrage.
*   Associe automatiquement chaque outil à son serveur (RAG vs DB).
*   Définit les métadonnées de sécurité (Scope, Risk) de manière déclarative (décorateurs ?) pour éliminer le fichier statique `constants.py` difficile à maintenir.

### B. Gestion de Session Robuste (Async Context Managers)
Réimplémenter la couche de connexion aux serveurs MCP :
*   Utiliser des **Context Managers Asynchrones** (`async with MCPSession(...)`) pour garantir la propreté des ressources.
*   Si les serveurs restent in-process, assurer une compatibilité totale avec l'Event Loop de `uvicorn` (pas de `new_event_loop` sauvage).
*   Si passage en mode "Remote" (SSE/Stdio), implémenter un **Pool de Connexions** résilient.

### C. Middleware de Sécurité (Shield 2.0)
Le "Shield" doit évoluer vers un middleware modulaire :
*   **Validation Dynamique :** Vérifier les permissions avant l'exécution via le Registry standardisé.
*   **Audit Log Résilient :** Le logging (qui écrit en DB via `persist_conversation`) ne doit jamais faire planter l'appel principal (fail-safe pour l'audit, fail-closed pour l'action).
*   **Typage Strict :** Garantir que toutes les entrées/sorties sont validées par Pydantic v2 avant d'atteindre le code métier.

### D. Optimisation Agent-Tool Dispatching
*   L'agent doit pouvoir demander une action (ex: `search_agronomy_docs`) sans savoir quel serveur l'héberge.
*   Le Router intelligent doit dispatcher vers `RAG_SERVER` ou `DB_SERVER` de manière transparente.
*   Gestion propre des **Timeouts** et des **Retries** (backoff exponentiel) pour les appels longs (RAG).

## 4. Livrables Attendus
Un ensemble de modules Python (ex: `mcp_manager.py`, `schemas.py`, `security.py`) prêts à l'emploi (plug-and-play dans FastAPI) qui :
1.  Éliminent les erreurs `PermissionDenied` dues à des oublis de configuration.
2.  Corrigent définitivement les erreurs d'Event Loop asyncio.
3.  Offrent une DX (Developer Experience) simple pour ajouter un nouvel outil sans toucher à 3 fichiers différents.



Prochaine étape suggérée :Pour rendre cela concret, tu devrais essayer de transformer ta prédiction rouge en "vrai prix". Pour cela, il faut faire l'opération inverse de la dérivée fractionnaire (une intégration fractionnaire), mais c'est complexe.Une alternative plus simple pour ton IA :Utiliser la valeur prédite par l'IA pour générer un signal d'achat/vente (ex: si la courbe rouge monte de plus de $X\%$, j'achète).Calculer le gain théorique sur le S&P 500 avec cette stratégie.