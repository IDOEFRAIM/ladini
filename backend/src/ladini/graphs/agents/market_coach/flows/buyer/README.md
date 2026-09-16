# Buyer Flow — Tunnel transactionnel Acheteur

Ce dossier regroupe toute la logique métier du côté acheteur. Il est organisé pour
séparer l’orchestration de la machine à états (LangGraph) de la logique pure de
transaction.

## Structure
- `flow.py` : cœur fonctionnel. Implémente les trois phases principales :
  1. **Cart management** (`cart_management`) — résolution produit, verrouillage
     stock atomique, calcul des totaux et menus AG‑UI.
  2. **Précommande** (`_create_preorder`) — conversion panier → brouillon,
     génération du menu de confirmation, création/annulation/confirmation via MCP.
  3. **Négociation** (`negotiation_gate`) — ouverture/suivi des contre-offres,
     mise à jour d’enchère et menus d’actions.
  Le routeur `_update_preorder_phase` force les transitions déterministes (CART →
  PREORDER_DRAFTED → CONFIRMED) suivant `preorder_workflow`.
- `state.py` : définition de `BuyerContext`, c’est‑à‑dire les clés d’état propres
  au domaine acheteur (panier, workflow, last_order_summary, etc.). Les reducers
  sont alignés sur `MarketAgentState`.
- `__init__.py` : expose les symboles principaux pour empêcher les import cycles.

## Principales fonctionnalités
1. **Menu contractuel unique** : les menus sont retournés sous forme de
   `MenuRequest`, transformés par `nodes/ui_engine.py` pour des réponses AG‑UI
   cohérentes.
2. **Séparation MCP / Graphe** : toutes les interactions DB passent par
   `safe_call_tool` (wrappe `MarketRuntime`). Les nodes ne connaissent pas le
   schéma MCP.
3. **Résilience tunnel** :
   - `preorder_workflow` pilote le routage, jamais d’heuristique fondée sur
     l’IA.
   - `last_order_summary` conserve les confirmations pour réémettre un reçu.
   - `fallback_recommendations` fournit des alternatives (stock insuffisant,
     zone manquante, etc.).
4. **Négociation multi-étapes** : gestion d’une session `negotiation_context`
   avec menu dynamique (acceptation, contre-offre, abandon).

## Parcours fonctionnel détaillé
Le tunnel acheteur s’enclenche dès qu’un utilisateur identifié comme `BUYER`
exprime un besoin produit. Les étapes clés et leurs implémentations sont :

1. **Expression du besoin & capture des entités** — `cart_management` récupère
   `product`, `quantity` et `unit` depuis `transaction_payload`, `stable_entities`
   ou directement depuis le texte via `_infer_product_from_text`. Dès qu’une
   quantité est connue, `buyer_context_resolver` peut forcer l’objectif
   `BUYER_ADD_TO_CART` pour transformer la requête en panier.
2. **Vérification catalogue / stock** — `_resolve_product_ref` appelle
   `search_products`, puis `validate_stock_availability_atomic` garantit que la
   quantité est disponible avant de créer une ligne `active_cart`. Les échecs
   alimentent `fallback_recommendations` (alternatives produits/unités).
3. **Précommande déterministe** — `_update_preorder_phase` maintient le couple
   `current_goal` ↔ `preorder_workflow.phase` (CART → PREORDER_DRAFTED →
   CONFIRMED). `_create_preorder` (helper MCP) matérialise le panier via
   `create_preorder_draft` puis `confirm_preorder_draft`, tandis que
   `_preorder_action_menu` expose les actions (confirmer, ajouter, annuler).
4. **Négociation** — `negotiation_gate` pilote l’ouverture
   (`initiate_negotiation_session`), les relances (`update_negotiation_offer`) et
   la sélection gagnante (`select_winning_bid`). Chaque phase alimente
   `negotiation_context` pour reprendre la session avec menus dynamiques.
5. **Gestion des enchères existantes** — la liste des appels d'offres de
   l'acheteur (`BUYER_LIST_AUCTIONS` ; l'ancien doublon `MARKET_MY_REQUESTS`,
   fusionné le 2026-07-21, a été supprimé le 2026-09-13, Deep Intent
   Architecture Cleanup) est servie par `list_buyer_auctions` dans
   `order_tracking.py` (voir point 6). `_resolve_received_bids` /
   `_resolve_buyer_bid_pick` (procurement.py), exclusivement rattachées à
   `MARKET_GET_REQUEST_DETAIL`/`PROCUREMENT_SELECT_WINNER`/
   `PROCUREMENT_ACCEPT_OFFER` (tous supprimés le même jour), ont été retirées.
6. **Suivi de commandes & appels d'offres** — `order_tracking_resolver` (fichier
   dédié) couvre les intents `BUYER_LIST_ORDERS`, `BUYER_CHECK_ORDER_STATUS`,
   `BUYER_CANCEL_ORDER`, `BUYER_LIST_AUCTIONS` et `BUYER_CHECK_AUCTION_STATUS`,
   avec menus expirables et résumés humains
   (`last_order_summary`, `order_tracking_context`).

L’ensemble de ces étapes est orchestré par `buyer_context_resolver`, qui ne se
base que sur l’état (jamais sur des heuristiques LLM) pour router vers le bon
node.

## Points d’extension
- Ajouter un nouvel outil MCP ? implémenter un helper + wrapper
  `safe_call_tool` dans ce dossier, puis retourner un `MenuRequest` ou un
  patch d’état.
- Ajouter une phase au tunnel : étendre `preorder_workflow` + ajuster
  `_update_preorder_phase` et le DomainRouter.
- Nouveaux champs d’état : les définir dans `state.py` pour bénéficier des
  reducers canoniques.

## Pipeline global côté buyer

1. **Interprétation & planification**
   - `interpreter/routing.py` filtre les intentions autorisées pour les acheteurs,
     applique les fast-paths structurants (sélection numérique, reprise de tunnel)
     puis délègue au LLM. Le `goal_planner` verrouille `current_goal` et
     reconstruit systématiquement le mapping de désambiguïsation depuis
     `INTENT_DISAMBIGUATION` pour éviter les boucles.
   - `nodes/memory.py` fusionne les entités extraites avec le `transaction_payload`,
     préserve l’objectif actif dans `working_memory`, et résout les menus AG‑UI
     (`selection_index` → `resolved_id`).

2. **Validation & routage déterministe**
   - `nodes/validation.py` applique le contrat `INTENT_CONFIG` : extraction
     déterministe des quantités/unités, appel LLM en dernier recours, reset des
     champs pollués lors d’un changement de goal.
   - `core/router.DefaultDomainRouter` oriente ensuite vers les nœuds dédiés
     (`cart_management`, `negotiation_gate`, `order_tracking_resolver`) en fonction
     du goal courant.

3. **Flows métiers**
   - `cart_management` gère la résolution produit → vendeur, vérifie le stock et
     alimente `active_cart` + `preorder_workflow.phase`.
   - `_create_preorder` et `_update_preorder_phase` pilotent la transition
     CART → PREORDER_DRAFTED → CONFIRMED en garantissant un menu d’actions unique.
   - `negotiation_gate` encapsule ouverture de session, consultation d’offres et
     acceptation via menus dynamiques.
   - `order_tracking_resolver` expose `list_orders`, `check_order_status` et
     `cancel_order` avec un cache de mapping 30 min pour les menus.

4. **Sortie & nettoyage**
   - `nodes/ui_engine.py` convertit `MenuRequest` en composant AG‑UI tout en
     synchronisant `available_mapping_kind`.
   - `nodes/response_handlers.py` compose le message final (texte + composant)
     et recharge le mapping dans les réponses.
   - `nodes/cleanup.py` ne purge plus les métadonnées de désambiguïsation tant
     qu’un menu actif est affiché, préservant la résolution numérique au tour
     suivant.

## Points de vigilance

- **Synchronisation `current_goal`/`preorder_workflow.phase`** : toute nouvelle
  action doit respecter les transitions explicites dans `_update_preorder_phase`
  au risque de casser le tunnel (pas de mutation implicite côté nodes).
- **Menus dynamiques** : si `available_mapping` est perdu, le `goal_planner`
  reconstruit depuis `disambiguation_trigger_id`. Vérifier que `post_response_cleanup`
  conserve bien `working_memory.disambiguation_trigger_id` lorsque
  `pending_menu` est présent.
- **Sélections numériques** : `memory_update` ne purge `selection_index` qu’après
  résolution réussie. En cas de nouveau menu, s’assurer que le snapshot AG-UI est
  valide (`working_memory.menu_snapshot_id`, `vendor_selection_context`, etc.).
- **Services MCP** : tous les helpers (`safe_call_tool`, `_safe_tracking_call`)
  filtrent les `None`. Les retours doivent contenir `status`, `message`, `mapping`
  pour être exploités par les menus.

## Checklist debug rapide

1. **Intent bloqué ?** Vérifier `goal_planner` et `working_memory.active_goal`
   (logs `Ladini.Market.InterpreterRouting`).
2. **Sélection qui boucle ?** Contrôler `available_mapping`, `disambiguation_trigger_id`
   et la présence du snapshot courant (`working_memory.menu_snapshot_id`).
3. **Précommande figée ?** Inspecter `preorder_workflow.phase` + `current_goal`
   avant et après l’appel `_create_preorder`.
4. **Négociation silencieuse ?** Confirmer que `negotiation_context.phase`
   vaut `NEGOTIATION_MENU` ou `VIEWING_OFFERS` avant d’attendre un prix/index.
5. **Suivi de commande vide ?** Vérifier la réponse `get_buyer_orders_dashboard`
   (`status`, `mapping`) et la validité du snapshot (TTL 30 min).

## Tests manuels
- **Onboarding** : `python -m ladini.graphs.agents.market_coach.core.graph_builder`
  lançait déjà `test_onboarding_flow` (LLM requis).
- **Parcours acheteur complet (panier → précommande → négociation)** : le même
  module expose désormais `demo_buyer_purchase_flow` (voir `if __name__ ==
  "__main__"`). La démo utilise un runtime MCP fictif et monkey-patche
  `_create_preorder` pour fournir un scénario déterministe reproductible :
  ajout d’un produit, création/confirmation d’une précommande, ouverture d’une
  négociation et acceptation d’une offre.

```bash
poetry run python -m ladini.graphs.agents.market_coach.core.graph_builder
```

Les logs indiquent, étape par étape, le `current_goal`, le statut et les menus
AG‑UI générés, ce qui facilite le debug des régressions côté buyer.
