"""ALLOW-LIST D'EXPOSITION MCP — la seule chose qui fait d'une méthode un outil.

## La règle (convention d'architecture officielle)

> Une méthode publique de `AgriDatabaseService` n'est PAS un outil MCP,
> sauf si son nom figure explicitement dans `MCP_EXPOSED_TOOLS`.

Voir `docs/MCP_TOOL_EXPOSURE_POLICY.md`.

## Pourquoi ce module existe

Jusqu'ici, `protocols/mcp/servers/h.py::_compute_exposed_methods()`
exposait **toute** méthode async publique du service, par introspection.
La sécurité reposait alors sur l'ABSENCE d'entrée dans `TOOL_SCOPE_MAP`
(deny-by-omission) — un modèle opt-out où l'ajout d'une méthode crée un
outil, et où un scope posé « par cohérence » ouvre un accès.

Ce modèle a produit quatre failles, toutes de la même racine :

* `update_order_status` — statut/paiement arbitraires, sans propriétaire ;
* `mark_escrow_paid` — déclarer un paiement reçu sans le fournisseur ;
* `expire_pending_payments` — annulation en masse, sans acteur ;
* `cancel_preorder_draft(target_status)` — statut cible non validé.

L'exposition est désormais **opt-in** : cette liste est la source
d'autorité de ce QUI existe comme outil ; `TOOL_SCOPE_MAP` reste la source
d'autorité de QUI a le droit de l'appeler.

```
MCP_EXPOSED_TOOLS  →  TOOL_SCOPE_MAP  →  autorisation runtime
   (existe ?)           (permission ?)      (fail-closed)
```

## Comment ajouter un outil

1. La méthode doit résoudre son acteur et filtrer la ressource par lui
   (voir `tests/architecture/test_order_mutations_require_ownership.py`).
2. Ajouter son nom ici, dans le groupe correspondant.
3. Lui déclarer un scope dans `TOOL_SCOPE_MAP`.

Sans les trois, l'appel est refusé — jamais autorisé par défaut.

## Ce qui n'est délibérément PAS exposé

* **Système** (appelé en direct par un cron/IPN/worker, jamais par l'agent) :
  `mark_escrow_paid`, `expire_pending_payments`, `ensure_performance_indexes`,
  `add_product_photo`/`add_bid_photo`/`add_auction_photo` (tâches média),
  `check_and_expire_auctions`.
* **Interne** (helpers de résolution d'identité, appelés dans la couche
  service) : `get_buyer_profile`, `get_producer_profile`, `guess_category`,
  `resolve_sub_category`, `normalize_unit`.
* **Dangereux** : `update_order_status` (statut/paiement arbitraires).
* **Mort / hérité** : `finalize_multi_order` (produirait une commande
  multi-producteurs), `update_production_visibility`, `toggle_product_availability`,
  `rate_delivery`, `get_voice_catalog`, et les lectures publiques non câblées.

Une méthode Python publique qui n'est pas ici reste parfaitement appelable
**depuis le code** : « public » ≠ « outil MCP ».
"""

from __future__ import annotations

#: Outils réellement atteignables via MCP. Construit à partir des appelants
#: RÉELS mesurés dans le dépôt : `tool_name` des goals de `INTENT_CONFIG`
#: qui résolvent vers une méthode existante, plus tout nom littéral passé à
#: `_call(...)`, `call_db(...)` ou `call_tool(...)` dans le paquet agent.
MCP_EXPOSED_TOOLS: frozenset[str] = frozenset(
    {
        # ── Identité / profil ────────────────────────────────────────────
        "identify_or_create_user",
        "create_user_profile",
        "get_user_by_phone",
        "get_account_status",
        "update_communication_prefs",
        "update_geo_location",
        # ── Exploitation (farm) ──────────────────────────────────────────
        "create_farm",
        "update_farm",
        "get_farms",
        "get_or_create_farm",
        "get_producer_farm",
        # ── Catalogue produit ────────────────────────────────────────────
        "create_product",
        "update_product_price_and_qty",
        "delete_product",
        "get_my_products",
        "search_products",
        "validate_stock_availability_atomic",
        # ── Stock (lecture + récolte) ────────────────────────────────────
        "add_stock",
        "get_stocks",
        "get_producer_stocks",
        # `get_farm_stocks` : inventaire détaillé d'UNE exploitation (goal
        # STOCK_GET_DETAIL). Ré-exposé le 2026-09-10 après implémentation de
        # la méthode manquante — la déclaration existait dans TOOL_SCOPE_MAP
        # mais sans code derrière, et le goal était mort (voir
        # interpreter/routing.py::_DEPRECATED_INTENTS).
        "get_farm_stocks",
        "get_stock_movements",
        # ── Production future ────────────────────────────────────────────
        "declare_future_production",
        "update_production_fields",
        "list_producer_productions",
        "get_offer_reservations",
        "reserve_future_offer",
        # ── Panier / précommande ─────────────────────────────────────────
        "create_preorder_draft",
        "confirm_preorder_draft",
        "cancel_preorder_draft",
        # ── Commandes ────────────────────────────────────────────────────
        "get_buyer_orders_dashboard",
        "get_transaction_summary",
        "get_producer_orders",
        "cancel_pending_order",
        "confirm_delivery_and_payment",
        "cancel_confirmed_order",
        "confirm_order_by_producer",
        "record_sale",
        # ── Enchères / RFQ ───────────────────────────────────────────────
        "create_auction",
        "update_auction_fields",
        "get_auctions",
        # ── Approvisionnement récurrent (Phase 2/4) ─────────────────────────
        "create_recurring_need",
        "create_recurring_needs",
        "update_recurring_need",
        "list_my_recurring_needs",
        "get_recurring_need_detail",
        "accept_match_proposal",
        "mark_order_delivery_status",
        "record_order_reception",
        "list_my_deliverable_orders",
        "get_auction_bids",
        "get_auctions_bids",
        "get_producer_auctions",
        "place_bid",
        "update_bid_price",
        "get_my_active_bids",
        "select_winning_bid",
        # ── Négociation ──────────────────────────────────────────────────
        "initiate_negotiation_session",
        "update_negotiation_offer",
        "close_negotiation_session",
        # ── Escrow (chemin désactivé en production, câblé côté agent) ────
        "initiate_escrow_payment",
        "verify_delivery_otp",
        "list_producer_escrowed_orders",
        # ── Marché / référentiel ─────────────────────────────────────────
        "get_market_snapshot",
        "check_price_anomaly",
        "get_available_zones",
        "get_zone_by_name",
        "get_product_category_unit_config",
        # ── Modération / signaux ─────────────────────────────────────────
        "get_prohibited_terms",
        "record_moderation_strike",
        "record_demand_signal",
        # ── Finance producteur ───────────────────────────────────────────
        "add_expense",
        "get_expense_summary",
        # ── Photos (produit / offre / enchère) ───────────────────────────
        # Capacité UTILISATEUR (le producteur envoie une photo par WhatsApp).
        # L'implémentation actuelle appelle le service en direct depuis
        # `workers/media/product_photo_task.py`, mais ces outils ont été
        # délibérément déclarés avec un scope explicite lors du chantier
        # « photo produit par WhatsApp », et `tests/unit/test_mcp_hardening.py`
        # verrouille leur exposition. On conserve donc cette décision : le
        # mode d'appel actuel est un détail d'implémentation, pas une raison
        # de retirer une capacité produit.
        "add_product_photo",
        "add_bid_photo",
        "add_auction_photo",
    }
)

__all__ = ["MCP_EXPOSED_TOOLS"]
