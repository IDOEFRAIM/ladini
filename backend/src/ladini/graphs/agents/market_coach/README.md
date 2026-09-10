# MarketCoach Agent: structure et points d'extension

Ce répertoire contient toute l'implémentation de l'agent MarketCoach (acheteur/producteur).
Chaque sous-partie est isolée pour limiter les dépendances circulaires et faciliter les
évolutions. Ce document décrit le rôle précis de chaque dossier/fichier clé.

## `adapter.py`
Entrée DI canonique pour l'agent. Responsabilités :
- instancie/compile le LangGraph via `core/graph_builder.py` et met le résultat en cache.
- construit l'état initial `MarketAgentState` pour chaque session (profil, phone, goal, etc.).
- expose `MarketCoach.handle(...)` utilisé par l'orchestrateur.

## `graph.py`
Alias historique conservé pour les anciens imports. Ré-exporte simplement les symboles de
`adapter.py` afin de ne plus multiplier les points d'entrée.

## `core/`
Cœur partagé du graphe :
- `graph_builder.py` : assemble les 20+ nœuds LangGraph, insère le `ui_engine` et configure
  la `DefaultDomainRouter`.
- `router.py` : routeur domaine/goal (buyer/producer) + constantes de goals.
- `state.py` : définition complète de `MarketAgentState` (TypedDict + reducers partagés).
- `base.py` et `utils.py` : helpers transverses (logging, MarketRuntime, `_safe_node`, normalisations).

## `flows/`
Logique métier pure (aucun import de nodes). Organisation :
- `buyer/` : tunnel transactionnel acheteur (cart → preorder → negotiation). `flow.py`
  orchestre les phases via `preorder_workflow`. `state.py` définit `BuyerContext`.
- `producer/` : équivalent producteur (gestion de stock, enchères). Même séparation `flow/state`.
- `common/` : composants partagés (menu contracts, onboarding node, helpers).

## `nodes/`
Implémentations LangGraph des nœuds (fonctions async). Chaque fichier = un nœud spécialisé :
- `confirmation_gate.py`, `memory.py`, `ui_engine.py`, `response_handlers.py`, etc.
- Ces nœuds ne contiennent que de la logique de graphe (pas de MCP direct sauf wrappers). Ils
  consomment les flows via `MarketRuntime`.

## `interpreter/`
- `intent.py`, `goal_planner` et `routing` propres à l'analyse utilisateur.
- `intent_router.py` définit les stratégies de réponse (ASK_CLARIFICATION, SUCCESS, ...).
- `interpreter_routing.py` relie l’analyse entrée → goal planner.

## `market.py`
Point d’entrée legacy-friendly pour publier l’agent côté orchestrateur HTTP. Wrappe `adapter`
et gère la configuration runtime (LLM, MCP, cache).

## `security.py`
Contraintes de sécurité spécifiques MarketCoach : filtrage des commandes interdites,
contrôle d’accès, activation du mode « grade entreprise ».

## `server.py`
Expose l’agent sous forme de service FastAPI/Uvicorn prêt à être lancé (utilisé pour les tests
manuels ou les démos). Charge les settings, met en place les middlewares, et redirige vers
`adapter.get_agent_graph`.

## `utils.py`
Bibliothèque utilitaire :
- `MarketRuntime` (facilité d’appel MCP/DB + journalisation).
- Fonctions helpers (`ensure_dict`, `_normalize_quantity_to_kg`, mappings MenuRequest).
- `safe_call_tool` et décorateurs de résilience (_safe_node, instrumentation logs).

---
**Flux recommandé** :
1. L’input utilisateur traverse `interpreter/` pour déterminer `current_goal`.
2. `core/router.DefaultDomainRouter` envoie l’état vers le flow buyer/producer adéquat.
3. Les flows produisent un `pending_menu` + patchs d’état.
4. `nodes/ui_engine.py` convertit `pending_menu` en composant AG-UI, puis `response_handlers`
   finalise la réponse utilisateur.

Ce README doit servir de carte d’orientation : toute nouvelle fonctionnalité devrait se
brancher sur la couche correspondante (flow → node → graph).
