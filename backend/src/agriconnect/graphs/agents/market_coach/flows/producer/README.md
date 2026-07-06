# Producer Flow — logique MarketCoach côté producteur

Ce dossier décrit tout ce que l’agent MarketCoach exécute pour un **producteur** :
interprétation des requêtes, résolution des identifiants métier (fermes, stocks,
enchères), construction des menus AG‑UI et préparation des appels MCP.

## 1. Chaîne d’exécution
1. **`interpreter/routing.py`** détecte les intentions producteur (fast-path sur
   `SALES_LIST_ORDERS`, filtres de statut, etc.) et remplit `state.current_goal`.
2. **`input_normalizer` + identité** chargent `user_phone`, `user_id`, les fermes
   connues (`user_farms_cache`) et préparent `transaction_payload`.
3. **`nodes/validation.py`** compare le payload avec `INTENT_CONFIG` : il comble
   les champs dérivables, déclenche des menus pour les champs manquants (fermes,
   stocks) et renvoie l’utilisateur vers le slot attendu.
4. **`flows/producer/flow.py` (`producer_context_resolver`)** complète le payload :
   - `_resolve_default_farm` → auto-résolution de `farm_id`
   - `_resolve_stock` → sélection d’un lot
   - `_resolve_auction`, `_resolve_my_bids`, `_resolve_bid` → enchères et offres
5. **`ui_engine.py`** convertit chaque `MenuRequest` du flow en composant AG‑UI
   standard, installe `available_mapping` et garde la trace de `kind` dans
   `working_memory.available_mapping_kind`.
6. **`ensure_farm_node` (`flows/producer/farm_logic.py`)** est invoqué après le
   context resolver pour provisionner automatiquement une ferme si une action
   critique sans `farm_id` doit quand même s’exécuter.
7. **`confirmation_gate`** synthétise un récapitulatif, vérifie les champs
   sensibles (prix/quantité) et exige un accord explicite avant d’écrire.
8. **`nodes/executor.py`** appelle `prepare_market_action` (dans `actions.py`),
   passe par le `TaskHandler` pour les opérations d’écriture, puis déclenche le
   MCP tool final via `MarketRuntime.call_db`.

## 2. Modules clés

### `flow.py`
- Point d’entrée du resolvateur producteur.
- Fonctionne par branches conditionnelles sur `state.current_goal` pour éviter
  les appels MCP inutiles et centraliser la génération des menus.
- Les helpers `_resolve_*` retournent déjà un `MenuRequest` complet (titre,
  options, `kind`), ce qui garantit une présentation homogène via `ui_engine`.

### `actions/` (plugins)
- Chaque domaine (`stock.py`, `sales.py`, `procure.py`, `agro.py`, etc.) expose
  des handlers décorés via `@register_action`. Les helpers nettoient les champs,
  normalisent les unités (`normalize_quantity_to_kg`) et retournent le tuple
  `(tool_name, tool_args)` attendu par FastMCP.
- Le module `registry.py` auto-découvre ces plugins au démarrage et devient la
  **seule** source de vérité pour savoir si une intention est READ/WRITE.
- `nodes/executor.py` récupère le handler via `registry.get_action(...)` et
  échoue immédiatement si un intent n’est pas enregistré (fail-fast).

### `state.py`
- Déclare `ProducerContext` (projection TypedDict injectée dans
  `MarketAgentState`).
- Principal champ : `user_farms_cache`, alimenté par l’`input_normalizer` et
  réutilisé dans `nodes/validation.py` pour proposer la bonne ferme.

### `farm_logic.py`
- `ensure_farm_node` récupère/auto-crée une ferme s’il n’y en a pas.
- Met à jour `transaction_payload`, `stable_entities` et ajoute un message
  `_AUTO_FARM_NOTICE` lorsque l’autocreation réussit.

### `nodes/executor.py`
- `_build_task_payload` extrait `price`, `quantity`, `unit` et le contexte pour
  le `TaskHandler` (garde-fou contre les écritures incohérentes).
- `_auto_provision_farm_if_needed` combine le schéma MCP et `FARM_CRITICAL_GOALS`
  afin d’invoquer `ensure_farm_node` juste avant l’exécution.
- `mcp_tool_executor` fusionne le résultat de `TaskHandler`, applique les
  corrections `available_mapping`, puis deferre à `MarketRuntime.call_db`.

## 3. Résolution des fermes et stocks
- `GOALS_NEEDING_FARM_ID` (dans `flow.py`) dérive automatiquement les intents qui
  listent `farm_id` dans `INTENT_CONFIG`. Avant toute autre action, le resolver
  tente `_resolve_default_farm` pour réduire les boucles utilisateur.
- `FARM_CRITICAL_GOALS` (dans `core/base.py`) déclenche `ensure_farm_node` au
  niveau du nœud `ensure_farm_node` si, malgré tout, un MCP tool exige encore un
  `farm_id` manquant.
- `_resolve_stock` interroge `get_producer_stocks`, filtre selon le produit
  et construit un menu structuré avec quantité/unité pour aider la sélection.

## 4. Construction des menus
- Tous les menus passent par `MenuRequest` → `ui_engine`. Cela garantit :
  - `available_mapping` utilisable par l’interpréteur (`expected_input=SELECTION`).
  - Un `working_memory.available_mapping_kind` qui permet à l’agent de donner un
    sens aux réponses « 1 », « 2 », ou aux commandes contextuelles.
- Les helpers `MenuOption` contiennent déjà `index`, `label`, `value`, ce qui
  simplifie l’exploitation côté nodes (`pending_menu` est consommé une fois).

## 5. Passerelle MCP & confirmation
- Chaque `_prep_*` appelle un MCP tool officiel (ex. `create_product`,
  `get_auctions`, `commit_staged_transaction`). Aucun alias local n’est toléré.
- Le `TaskHandler` (cf. `agriconnect/agents/task_handler.py`) impose trois
  phases `perceive → think → act` avant d’autoriser l’écriture.
- `confirmation_gate` assemble les champs requis (`INTENT_CONFIG[...]`), formate
  un message utilisateur et injecte `execution_authorized=True` seulement après
  validation.

## 6. Extension et bonnes pratiques
- **Nouvel outil producteur** : ajouter un `_prep_*` dans `actions.py`, référencer
  l’intent dans le bon dictionnaire READ/WRITE, puis (si nécessaire) étendre
  `producer_context_resolver` pour préparer les IDs attendus.
- **Nouveau type de menu** : retourner un `MenuRequest` depuis le flow au lieu
  d’un dict brut — `ui_engine` prendra le relais.
- **Nouveau champ d’état** : déclarer la clé dans `ProducerContext` puis la
  manipuler via les reducers (`replace_value`) pour éviter les fuites de state.
- **Tests** : privilégier les scénarios conversationnels complets (fast-path
  → validation → context_resolver → confirmation). Les `safe_call_tool` sur
  `MarketRuntime` permettent de mocker facilement les réponses MCP.

## 7. Limitations connues
- Les heuristiques de matching produit/stock sont exactes (pas de fuzzy
  matching) ; des variantes orthographiques doivent encore être prises en charge
  côté flows.
- La normalisation des unités est centrée sur les kg : ajouter une unité globale
  nécessite de mettre à jour `_UNIT_TO_KG`.

Cette page doit être mise à jour dès qu’un nouvel intent producteur ou un
nouveau garde-fou est introduit afin de rester la référence opérationnelle.
