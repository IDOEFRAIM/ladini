from agriconnect.graphs.agents.market_coach.core.slots import (
    build_canonical_field_aliases,
)

INTENT_CONFIG = {
    # =======================================================================
    # DOMAINE : GESTION DES STOCKS & RÉCOLTES (WRITE — Producteur)
    # =======================================================================
    "STOCK_REGISTER_HARVEST": {
        "tool_name": "add_stock",
        "required": ["product", "quantity", "farm_id"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Mise en stock / Enregistrement d'une nouvelle récolte",
        "label_map": {
            "product": "produit/culture récolté",
            "quantity": "quantité récoltée",
            "unit": "unité",
            "farm_id": "exploitation source",
        },
    },
    "STOCK_RECORD_MOVEMENT": {
        "tool_name": "add_stock_movement_by_id",
        "required": ["stock_id", "movement_type", "quantity"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Enregistrement d'un mouvement de stock entrant/sortant",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "movement_type": "sens (Entrée/Sortie/Perte)",
            "quantity": "quantité bougée",
            "reason": "motif",
        },
    },
    "STOCK_ADJUST": {
        "tool_name": "adjust_stock_by_id",
        "required": ["stock_id", "quantity"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Correction manuelle de l'inventaire physique",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "quantity": "nouvelle quantité réelle constatée",
            "reason": "motif",
        },
    },
    "STOCK_REMOVE_PARTIAL": {
        "tool_name": "remove_stock_by_id",
        "required": ["stock_id", "quantity"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Retrait partiel du stock disponible",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "quantity": "quantité à retirer",
        },
    },
    "STOCK_DELETE": {
        "tool_name": "delete_stock_by_id",
        "required": ["stock_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Suppression définitive d'une ligne de stock",
        "label_map": {"stock_id": "identifiant stock"},
    },
    "STOCK_UPDATE_LEVEL": {
        "tool_name": "adjust_stock_by_id",
        "required": ["stock_id", "quantity"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "label": "Mise à jour directe du niveau d'un lot",
        "label_map": {
            "stock_id": "identifiant stock",
            "quantity": "nouvelle quantité réelle",
            "unit": "unité (optionnel)",
            "reason": "motif",
        },
    },
    # =======================================================================
    # DOMAINE : MARCHÉ PRODUCTEUR — VENTES (WRITE — PRODUCER)
    # =======================================================================
    "SALES_PUBLISH_PRODUCT": {
        "tool_name": "create_product",
        "required": ["product", "price", "quantity"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Mise en vente d'un produit sur le catalogue public",
        "label_map": {
            "product": "nom du produit",
            "price": "prix unitaire proposé",
            "quantity": "quantité disponible",
            "unit": "unité",
            "description": "détails",
        },
    },
    "SALES_RECORD_DIRECT": {
        "tool_name": "record_sale",
        "required": ["product", "quantity", "price"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Enregistrement d'une vente directe (Cash / Gré à gré)",
        "label_map": {
            "product": "produit vendu",
            "quantity": "quantité",
            "price": "montant total de la vente",
            "unit": "unité",
        },
    },
    "SALES_LIST_ORDERS": {
        "tool_name": "get_producer_orders",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Consultation des commandes acheteurs liées à mes produits",
        "label_map": {
            "status": "statut ciblé (PENDING, CONFIRMED, ...)",
            "limit": "nombre maximum de commandes à afficher",
        },
    },
    "SALES_PLACE_BID": {
        "tool_name": "place_bid",
        "required": ["auction_id", "price"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Proposition de vente face à une demande acheteur existante",
        "label_map": {
            "auction_id": "numéro de l'appel d'offres",
            "price": "votre prix proposé",
            "quantity": "quantité proposée",
            "message": "note",
        },
    },
    "SALES_ACCEPT_CONTRACT": {
        "tool_name": "commit_staged_transaction",
        "required": ["bid_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Validation définitive des termes du contrat verrouillé",
        "label_map": {"bid_id": "numéro de transaction/offre"},
    },
    "SALES_UPDATE_PRODUCT": {
        "tool_name": "update_product_price_and_qty",
        "required": ["product_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "label": "Mise à jour d'un produit du catalogue (prix, quantité, nom, unité)",
        "label_map": {
            "product_id": "référence produit",
            "price": "nouveau prix unitaire",
            "quantity": "nouvelle quantité disponible",
            "product": "nouveau nom du produit",
            "unit": "unité (optionnel)",
        },
    },
    # (2026-09-04, Product Completeness Phase 2) : RETRAIT d'un produit du
    # catalogue. La méthode DB `delete_product` (services/database/product.py)
    # existait déjà, complète et sûre — verrou FOR UPDATE, contrôle de
    # propriété, REFUS si des commandes actives existent, archivage doux
    # (`is_available=False`, historique préservé) si le produit a déjà été
    # commandé, suppression physique seulement s'il n'a jamais servi. Aucun
    # goal ne l'atteignait : un producteur ne pouvait donc JAMAIS retirer un
    # produit épuisé/erroné de son catalogue, alors que les acheteurs, eux,
    # continuaient de le voir. Le `product_id` est résolu conversationnellement
    # (`flows/producer/flow.py::_resolve_product_for_unpublish`), jamais
    # demandé comme UUID brut — voir aussi `nodes/validation.py`
    # (`_RESOLVER_PASSTHROUGH`).
    "SALES_UNPUBLISH_PRODUCT": {
        "tool_name": "delete_product",
        "required": ["product_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "label": "Retrait d'un produit de mon catalogue de vente",
        "label_map": {
            "product_id": "référence du produit à retirer",
        },
    },
    # Mise à jour d'une PRODUCTION FUTURE / lot (MarketOffer), distincte du
    # produit catalogue ci-dessus. Permet de corriger prix, quantité, NOM
    # (product), unité, date de disponibilité ou type (culture/élevage) d'un lot
    # déjà déclaré — y compris renommer un lot mal nommé "culture". cycle_id est
    # résolu par la sélection du numéro dans la liste des cultures/futures récoltes.
    "SALES_UPDATE_PRODUCTION": {
        "tool_name": "update_production_fields",
        "required": ["cycle_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "label": "Mise à jour d'un lot / production future (prix, quantité, nom, date…)",
        "label_map": {
            "cycle_id": "référence de la production",
            "price": "nouveau prix unitaire",
            "quantity": "nouvelle quantité prévue",
            "product": "nouveau nom du produit",
            "unit": "unité (optionnel)",
            "estimated_available_at": "nouvelle date de disponibilité",
            "production_type": "type (culture ou élevage)",
        },
    },
    # Escrow (Paydunya) : le producteur transmet le code de livraison à 4
    # chiffres reçu de l'acheteur pour débloquer ses fonds bloqués. Tunnel
    # auto-suffisant "producer_escrow" (extraction déterministe du code,
    # jamais de classification LLM sur le code lui-même) — voir
    # flows/producer/flow.py::_resolve_delivery_otp.
    "PRODUCER_CONFIRM_DELIVERY_OTP": {
        "tool_name": "verify_delivery_otp",
        "required": ["otp_code"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "handled_by_flow": True,
        "label": "Confirmation de livraison par code secret (débloque le paiement séquestré)",
        "label_map": {
            "otp_code": "code de livraison à 4 chiffres",
        },
    },
    # (2026-09-04, clôture F1 — paiement à la livraison) : NE couvre PAS le
    # même cas que PRODUCER_CONFIRM_DELIVERY_OTP ci-dessus (réservé aux
    # commandes escrow — `payment_status` déjà `ESCROWED`, code secret déjà
    # émis). Ce goal couvre les commandes en paiement CASH à la livraison
    # (`payment_status="PENDING"`, aucun paiement en ligne) — RFQ gagnées
    # (`select_winning_bid`) ou préorder confirmé hors-escrow
    # (`confirm_preorder_draft`). Pas de tunnel dédié
    # (`handled_by_flow` absent) : passe par le mécanisme GÉNÉRIQUE
    # confirmation_gate/mcp_tool_executor, même précédent que
    # `SALES_RECORD_DIRECT` — la résolution de QUELLE commande est visée
    # (jamais "la dernière commande") vit dans
    # `flows/producer/flow.py::_resolve_order_for_delivery_payment`.
    # (2026-09-04, Phase 5 — décision produit #1) : le producteur annule une
    # commande CONFIRMÉE qu'il ne peut pas honorer. `CONFIRMED` était le SEUL
    # état du produit sans sortie côté producteur : ne pouvant ni livrer ni se
    # rétracter, il devait demander à l'acheteur d'annuler. Symétrie stricte
    # du chemin acheteur (`BUYER_CANCEL_ORDER`) — aucun nouveau statut, aucun
    # remboursement (paiement à la livraison). `order_id` est résolu
    # conversationnellement (`_resolve_order_for_cancellation`), jamais
    # demandé comme UUID — voir `_RESOLVER_PASSTHROUGH` dans
    # `nodes/validation.py`.
    "PRODUCER_CANCEL_ORDER": {
        "tool_name": "cancel_confirmed_order",
        "required": ["order_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "label": "Annulation d'une commande que je ne peux pas honorer",
        "label_map": {
            "order_id": "numéro de la commande à annuler",
            "reason": "motif (rupture, aléa de production…)",
        },
    },
    "PRODUCER_CONFIRM_DELIVERY_PAYMENT": {
        "tool_name": "confirm_delivery_and_payment",
        "required": ["order_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "label": "Confirmation de livraison et paiement reçu à la livraison (cash, sans escrow)",
        "label_map": {
            "order_id": "numéro de la commande",
        },
    },
    # =======================================================================
    # DOMAINE : MARCHÉ ACHETEUR — APPROVISIONNEMENT (WRITE — BUYER)
    # =======================================================================
    "PROCUREMENT_CREATE_REQUEST": {
        "tool_name": "create_auction",
        "required": ["product", "quantity", "price"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Publication d'une demande d'approvisionnement / appel d'offres",
        "label_map": {
            "product": "produit recherché",
            "quantity": "quantité totale cherchée",
            "price": "prix plafond proposé",
            "unit": "unité",
            "zone": "région de collecte",
            "deadline": "date limite",
        },
    },
    "PROCUREMENT_SELECT_WINNER": {
        "tool_name": "select_winning_bid",
        "required": ["auction_id", "bid_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Sélection et validation de l'offre gagnante sur mon marché",
        "label_map": {
            "auction_id": "numéro de votre appel d'offres",
            "bid_id": "numéro de la proposition retenue",
        },
    },
    "PROCUREMENT_ACCEPT_OFFER": {
        "tool_name": "accept_bid",
        "required": ["bid_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Achat direct simple d'un produit du catalogue indexé",
        "label_map": {"bid_id": "numéro du produit catalogue"},
    },
    "BUYER_REQUEST": {
        "tool_name": "search_products",
        "required": ["product"],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Demande acheteur — recherche catalogue avant appel d'offres",
        "label_map": {
            "product": "produit recherché",
            "quantity": "quantité souhaitée (optionnel)",
            "unit": "unité (optionnel)",
        },
    },
    # =======================================================================
    # DOMAINE : TUNNEL TRANSACTIONNEL ACHETEUR ("Grade Entreprise")
    # Panier multi-items → Précommande → Négociation. Le routage de phase
    # est déterministe (preorder_workflow["phase"]) dans le buyer_flow.
    # =======================================================================
    "BUYER_ADD_TO_CART": {
        "tool_name": "add_to_cart",
        # (2026-08-30, refonte "palier avant quantité") : `quantity`/`unit`
        # retirés de `required`. Incident réel confirmé en direct : avec ces
        # deux champs requis ici, `tunnel_manager.is_cart_routeable()`
        # refusait de router vers `cart_management` tant que la quantité
        # n'était pas déjà connue (garde-fou anti-boucle générique) — le
        # validator répondait alors LUI-MÊME "quelle quantité ?" via son
        # propre prompt générique, AVANT même que `cart_management` ait pu
        # découvrir qu'un produit propose plusieurs conditionnements et
        # montrer le menu de paliers. `cart_management` gère déjà
        # entièrement lui-même la demande de quantité (avec ou sans palier,
        # voir flows/buyer/cart.py) — seul `product` doit bloquer le
        # routage ici.
        "required": ["product"],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Ajout d'un produit au panier de précommande",
        "label_map": {
            "product": "produit à ajouter",
            "quantity": "quantité souhaitée",
            "unit": "unité",
        },
    },
    "BUYER_VIEW_CART": {
        "tool_name": "view_cart",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Consultation du panier de précommande en cours",
        "label_map": {},
    },
    "BUYER_CREATE_PREORDER": {
        "tool_name": "create_preorder",
        "required": [],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Création d'une précommande à partir du panier actif",
        "label_map": {
            "expected_fulfillment_date": "date de livraison souhaitée",
            "payment_method": "moyen de paiement",
        },
    },
    "BUYER_PREORDER_INIT": {
        "tool_name": "init_preorder",
        "required": [],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Validation du panier et création d'une précommande (brouillon)",
        "label_map": {},
    },
    "BUYER_PREORDER_CONFIRM": {
        "tool_name": "confirm_preorder",
        "required": [],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Confirmation de la précommande brouillon (commande ferme)",
        "label_map": {},
    },
    "BUYER_NEGOTIATE_PRICE": {
        "tool_name": "negotiate_price",
        "required": ["product", "price"],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Ouverture d'une négociation de prix avec un producteur",
        "label_map": {
            "product": "produit à négocier",
            "price": "prix proposé",
            "quantity": "quantité concernée",
        },
    },
    # --- Suivi conversationnel de commandes (Buyer Order Tracking) ---
    "BUYER_CHECK_ORDER_STATUS": {
        "tool_name": "check_order_status",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Vérification du statut d'une commande (Où est ma commande ?)",
        "label_map": {
            "order_id": "numéro/référence de la commande",
        },
    },
    "BUYER_LIST_ORDERS": {
        "tool_name": "list_buyer_orders",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Consultation du tableau de bord des commandes en cours",
        "label_map": {},
    },
    "BUYER_CANCEL_ORDER": {
        "tool_name": "cancel_order",
        "required": [],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Annulation d'une commande en attente",
        "label_map": {
            "order_id": "numéro/référence de la commande à annuler",
        },
    },
    # --- Suivi conversationnel des enchères (Buyer Auction Tracking) ---
    "BUYER_LIST_AUCTIONS": {
        "tool_name": "get_auctions",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Liste de mes appels d'offres (tous statuts)",
        "label_map": {},
    },
    "BUYER_CHECK_AUCTION_STATUS": {
        "tool_name": "get_auction_bids",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Détail d'une enchère et offres reçues",
        "label_map": {
            "auction_id": "numéro de l'appel d'offres",
        },
    },
    # =======================================================================
    # DOMAINE : AGRONOMIE — PILOTAGE DE CULTURE (WRITE)
    # =======================================================================
    "CROP_START_CYCLE": {
        "tool_name": "create_crop_cycle",
        "required": ["farm_id", "product", "surface"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Démarrage d'un nouveau cycle de culture (semis/plantation)",
        "label_map": {
            "farm_id": "identifiant exploitation",
            "product": "culture",
            "surface": "superficie parcelle",
            "variety": "variété/semence",
        },
    },
    "DECLARE_CROP_CYCLE": {
        "tool_name": "declare_future_production",
        "required": [
            "farm_id",
            "production_type",
            "product",
            "quantity",
            "estimated_available_at",
            "price",
        ],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Déclaration d'un lot futur (culture/élevage) pour précommande",
        "label_map": {
            "farm_id": "identifiant exploitation",
            "production_type": "culture (plante) ou élevage (animal)",
            "product": "culture ou espèce",
            "quantity": "quantité prévue",
            "unit": "unité (KG, HEAD...)",
            "estimated_available_at": "date de disponibilité",
            "price": "prix unitaire prévu",
            "surface": "superficie (si culture)",
            "breed": "race (si élevage)",
            "preorder_enabled": "précommande active (oui/non)",
            "is_public": "visible catalogue (oui/non)",
        },
    },
    "CROP_RECORD_INTERVENTION": {
        "tool_name": "log_intervention",
        "required": ["cycle_id", "intervention_type"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Saisie d'une intervention technique sur site (irrigation, fertilisation)",
        "label_map": {
            "cycle_id": "cycle de culture",
            "intervention_type": "type d'action",
            "input_used": "intrant/matériel",
            "quantity": "quantité intrant",
            "details": "observations",
        },
    },
    "CROP_RECORD_OBSERVATION": {
        "tool_name": "add_growth_log",
        "required": ["cycle_id", "stage_label", "observation"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Enregistrement d'un suivi de croissance ou diagnostic",
        "label_map": {
            "cycle_id": "cycle de culture",
            "stage_label": "stade observé",
            "observation": "notes de suivi",
        },
    },
    "CROP_UPDATE_STAGE": {
        "tool_name": "add_crop_growth_stage",
        "required": ["cycle_id", "stage_name"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Changement formel de stade phénologique",
        "label_map": {
            "cycle_id": "cycle de culture",
            "stage_name": "nom du nouveau stade",
        },
    },
    "CROP_UPDATE_SOIL": {
        "tool_name": "update_soil_profile",
        "required": ["farm_id", "ph"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Enregistrement d'une analyse de sol (pH/Matière Organique)",
        "label_map": {
            "farm_id": "identifiant exploitation",
            "ph": "acidité sol (pH)",
            "organic_matter": "taux matière organique",
        },
    },
    # =======================================================================
    # DOMAINE : FINANCES (WRITE — Producteur)
    # =======================================================================
    "FINANCE_LOG_EXPENSE": {
        "tool_name": "add_expense",
        "required": ["price", "farm_id"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Enregistrement d'une dépense d'exploitation / charge",
        "label_map": {
            "price": "montant dépense",
            "product": "nature/libellé charge",
            "category": "catégorie",
            "farm_id": "exploitation concernée",
        },
    },
    # =======================================================================
    # DOMAINE : EXPLOITATION AGROBIZ (WRITE)
    # =======================================================================
    "FARM_CREATE": {
        "tool_name": "get_or_create_farm",
        "required": ["farm_name", "zone"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Déclaration d'une nouvelle exploitation agricole",
        "label_map": {
            "farm_name": "nom domaine",
            "surface": "superficie totale",
            "zone": "zone géographique",
        },
    },
    "FARM_UPDATE": {
        "tool_name": "update_farm",
        "required": ["farm_id"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Mise à jour des informations d'un domaine",
        "label_map": {
            "farm_id": "identifiant exploitation",
            "farm_name": "nouveau nom",
            "surface": "superficie",
        },
    },
    # =======================================================================
    # DOMAINE : PROFIL UTILISATEUR & GEO (WRITE — Générique)
    # =======================================================================
    "PROFILE_SET_GEO": {
        "tool_name": "update_geo_location",
        "required": ["latitude", "longitude"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Mise à jour de votre position GPS réelle",
        "label_map": {"latitude": "latitude", "longitude": "longitude"},
    },
    "PROFILE_SET_PREFS": {
        "tool_name": "update_communication_prefs",
        "required": ["language"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Configuration langue et notifications",
        "label_map": {"language": "langue", "allow_voice": "notifications vocales"},
    },
    "PROFILE_SWITCH_ROLE": {
        "tool_name": "create_agent_action",
        "required": ["target_role"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Changement de mode d'interface (PRODUCER/BUYER)",
        "label_map": {"target_role": "rôle cible"},
    },
    # =======================================================================
    # SYSTEME & SÉCURITÉ (WRITE)
    # =======================================================================
    "SYSTEM_REPORT_ANOMALY": {
        "tool_name": "report_anomaly",
        "required": ["target_id", "anomaly_type", "description"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Signalement d'une anomalie technique/marché",
        "label_map": {
            "target_id": "cible problème",
            "anomaly_type": "type incident",
            "description": "détails",
        },
    },
    "SYSTEM_BIND_ZONE": {
        "tool_name": "create_agent_action",
        "required": ["zone"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Rattachement territorial (zone agricole)",
        "label_map": {"zone": "nom zone"},
    },
    "SYSTEM_COMMIT_TRANSACTION": {
        "tool_name": "commit_staged_transaction",
        "required": ["staging_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Validation définitive d'une transaction verrouillée",
        "label_map": {"staging_id": "identifiant de staging"},
    },
    # =======================================================================
    # intentions DE LECTURE (READ — Consultations MCP Réelles)
    # Mappage strict sémantique des label_map
    # =======================================================================
    "STOCK_GET_SUMMARY": {
        "tool_name": "get_stocks",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Inventaire global multi-sites (Outil: get_stocks)",
        "label_map": {"zone": "filtre zone", "product": "filtre produit"},
    },
    "STOCK_GET_DETAIL": {
        "tool_name": "get_farm_stocks",
        "required": ["farm_id"],
        "action_type": "READ",
        "requires_farm": True,
        "label": "Inventaire détaillé par exploitation",
        "label_map": {"farm_id": "identifiant exploitation"},
    },
    "STOCK_GET_MOVEMENTS": {
        "tool_name": "get_stock_movements",
        "required": ["stock_id"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Grand livre de traçabilité d'un stock",
        "label_map": {"stock_id": "identifiant stock"},
    },
    "SALES_GET_CATALOG": {
        "tool_name": "get_stocks",
        "required": ["phone", "farm_id"],
        "action_type": "READ",
        "requires_farm": True,
        "label": "Consultation de mon catalogue de produits en vente",
        "label_map": {
            "phone": "votre téléphone",
            "farm_id": "identifiant exploitation",
        },
    },
    # SPLIT (2026-07-20, UX + bug de routage) : l'ancien goal unique
    # "MARKET_GET_REQUESTS" avait un sens OPPOSÉ selon le rôle — parcourir le
    # marché pour un PRODUCTEUR (browse_auctions), voir SES PROPRES appels
    # d'offres pour un ACHETEUR (list_buyer_auctions). Même nom, deux
    # comportements incompatibles → confusion utilisateur ET risque de
    # maintenance. Remplacé par deux goals explicites, chacun mono-rôle.
    # MARKET_MY_REQUESTS est depuis fusionné (2026-07-21) dans
    # list_buyer_auctions/order_tracking.py — voir AUCTION_TRACKING_GOALS.
    "MARKET_BROWSE_REQUESTS": {
        "tool_name": "get_auctions",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Parcours des appels d'offres du marché (producteur cherche à répondre)",
        "label_map": {"zone": "zone", "product": "produit", "status": "statut"},
    },
    "MARKET_MY_REQUESTS": {
        "tool_name": "get_auctions",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Consultation de mes propres appels d'offres publiés (acheteur)",
        "label_map": {"zone": "zone", "product": "produit", "status": "statut"},
    },
    "MARKET_GET_REQUEST_DETAIL": {
        "tool_name": "get_auctions_bids",
        "required": ["auction_id"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Consultation des offres reçues sur mon appel d'offres",
        "label_map": {"auction_id": "identifiant enchère"},
    },
    "MARKET_GET_MY_PROPOSALS": {
        "tool_name": "get_my_active_bids",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Suivi de mes propositions de vente envoyées",
        "label_map": {"phone": "votre téléphone"},
    },
    "MARKET_SNAPSHOT": {
        "tool_name": "get_market_snapshot",
        "required": ["zone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Cours et prix actuel du marché local",
        "label_map": {"zone": "zone de cotation"},
    },
    "MARKET_SNAPSHOT_ZONAL": {
        "tool_name": "get_zone_market_overview",
        "required": ["zone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Volumes de transaction et tendances locaux",
        "label_map": {"zone": "zone"},
    },
    "AGRO_GET_CYCLES": {
        "tool_name": "get_crop_cycles",
        "required": ["farm_id"],
        "action_type": "READ",
        "requires_farm": True,
        "label": "Historique des cycles de culture d'un domaine",
        "label_map": {"farm_id": "identifiant exploitation"},
    },
    "AGRO_GET_STANDARDS": {
        "tool_name": "get_crop_requirements",
        "required": ["product"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Besoins biologiques théoriques d'une culture",
        "label_map": {"product": "culture"},
    },
    "AGRO_GET_ECONOMICS": {
        "tool_name": "get_cycle_economics",
        "required": ["cycle_id"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Bilan financier analytique d'une parcelle",
        "label_map": {"cycle_id": "identifiant cycle"},
    },
    "AGRO_GET_RISKS": {
        "tool_name": "get_active_sanitary_risks",
        "required": ["zone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Analyse des risques sanitaires régionaux",
        "label_map": {"zone": "zone"},
    },
    "FARM_GET_MY_LIST": {
        "tool_name": "get_producer_farm",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Liste de mes domaines et exploitations AgriConnect",
        "label_map": {"phone": "votre téléphone"},
    },
    "FINANCE_GET_SUMMARY": {
        "tool_name": "get_expense_summary",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": True,
        "label": "Bilan comptable synthétique d'exploitation",
        "label_map": {"phone": "téléphone producteur", "days": "historique (jours)"},
    },
    "PROFILE_GET_MCP_USER": {
        "tool_name": "get_user_by_phone",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Consultation profil AgriConnect par téléphone",
        "label_map": {"phone": "téléphone de recherche"},
    },
    "PROFILE_GET_TRUST": {
        "tool_name": "get_trust_score",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Note de confiance commerciale",
        "label_map": {"phone": "votre téléphone"},
    },
    "PROFILE_GET_CONTEXT": {
        "tool_name": "get_user_context",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Variables de session NLU/Agent (Zéro MCP)",
        "label_map": {},
    },
    "DASHBOARD_PRODUCER": {
        "tool_name": "get_producer_dashboard",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Tableau de bord d'exploitation AgriConnect",
        "label_map": {"phone": "votre téléphone"},
    },
    "SEARCH_PRODUCTS": {
        "tool_name": "search_products",
        "required": ["product"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Recherche par mot-clé dans le catalogue",
        "label_map": {"product": "terme recherché"},
    },
    "SEARCH_NEARBY": {
        "tool_name": "get_all_zone_market_overview",
        "required": ["latitude", "longitude"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Recherche infrastructures / offres de proximité GPS",
        "label_map": {"latitude": "latitude", "longitude": "longitude"},
    },
    "VALIDATE_PRICE": {
        "tool_name": "check_price_anomaly",
        "required": ["product", "price", "zone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Vérification cohérence prix face à la tendance marché",
        "label_map": {
            "product": "produit",
            "price": "prix proposé",
            "zone": "marché référence",
        },
    },
    "SYSTEM_GET_PENDING": {
        "tool_name": "get_pending_actions",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Consultation des actions système en attente de traitement",
        "label_map": {},
    },
}


_UPDATE_KEYWORDS = ("UPDATE", "ADJUST", "REMOVE", "DELETE", "TOGGLE")
for _intent_key, _cfg in INTENT_CONFIG.items():
    lifecycle = str((_cfg or {}).get("lifecycle_mode") or "").upper().strip()
    if lifecycle not in {"CREATE", "UPDATE", "READ"}:
        action_type = str((_cfg or {}).get("action_type") or "READ").upper().strip()
        if action_type == "READ":
            lifecycle = "READ"
        elif any(token in _intent_key.upper() for token in _UPDATE_KEYWORDS):
            lifecycle = "UPDATE"
        else:
            lifecycle = "CREATE"
    _cfg["lifecycle_mode"] = lifecycle


# _CANONICAL_FIELD_ALIASES is now derived from core/slots.py (single source of truth).
_CANONICAL_FIELD_ALIASES = build_canonical_field_aliases()


def _canonicalize_field_name(field: str) -> str:
    return _CANONICAL_FIELD_ALIASES.get(field, field)


def _canonicalize_intent_config() -> None:
    for config in INTENT_CONFIG.values():
        required_fields = config.get("required") or []
        canonical_required = []
        for field in required_fields:
            canonical = _canonicalize_field_name(field)
            if canonical not in canonical_required:
                canonical_required.append(canonical)
        config["required"] = canonical_required

        label_map = config.get("label_map") or {}
        canonical_label_map = {}
        for key, label in label_map.items():
            canonical_label_map[_canonicalize_field_name(key)] = label
        config["label_map"] = canonical_label_map


_canonicalize_intent_config()


# =======================================================================
# TUNNEL / BREAKOUT — marquage de routage porté par le catalogue.
# `tunnel` : nom du tunnel transactionnel qui prend en charge l'intent
#            (routage post-validator). Les frozensets de goals sont DÉRIVÉS
#            de ce champ dans `core/goals.py` — ne jamais les redéfinir à la
#            main ailleurs (même pattern anti-drift que GOALS_NEEDING_FARM_ID).
# `breakout` : intent de navigation autorisé à interrompre un tunnel actif.
#              `core/goals.py::NAVIGATION_BREAKOUT_GOALS` en est dérivé et
#              en est la SOURCE UNIQUE ; depuis 2026-09-09 (Bloc 2,
#              Invariant A) son unique consommateur décisionnel est
#              `nodes/cognitive.py::cognitive_guard` — `TunnelManager` ne
#              connaît plus cette liste du tout.
# L'assignation via boucle échoue fort (KeyError) si un intent disparaît du
# catalogue — c'est voulu : la dérive est détectée à l'import, pas en prod.
# =======================================================================

_TUNNEL_ASSIGNMENTS = {
    # BUYER — tunnels transactionnels (handled_by_flow)
    "BUYER_ADD_TO_CART": "cart",
    "BUYER_VIEW_CART": "cart",
    "BUYER_CREATE_PREORDER": "preorder",
    "BUYER_PREORDER_INIT": "preorder",
    "BUYER_PREORDER_CONFIRM": "preorder",
    "BUYER_NEGOTIATE_PRICE": "negotiation",
    "BUYER_CHECK_ORDER_STATUS": "order_tracking",
    "BUYER_LIST_ORDERS": "order_tracking",
    "BUYER_CANCEL_ORDER": "order_tracking",
    "BUYER_LIST_AUCTIONS": "auction_tracking",
    "BUYER_CHECK_AUCTION_STATUS": "auction_tracking",
    "MARKET_MY_REQUESTS": "auction_tracking",
    # PRODUCER — intents entièrement pris en charge par
    # producer_auction_resolver (jamais confirmation_gate/mcp_tool_executor).
    "MARKET_BROWSE_REQUESTS": "producer_auction",
    "MARKET_GET_MY_PROPOSALS": "producer_auction",
    "SALES_PLACE_BID": "producer_auction",
    # Mise à jour (catalogue / production future) : gèrent leur PROPRE
    # confirmation en interne (corrections vs annulation vs validation),
    # jamais confirmation_gate/mcp_tool_executor génériques — voir
    # flows/producer/flow.py::_resolve_product_for_update /
    # _resolve_cycle_for_update.
    "SALES_UPDATE_PRODUCT": "producer_update",
    "SALES_UPDATE_PRODUCTION": "producer_update",
    # Escrow (Paydunya) : gère sa propre extraction/validation de code, jamais
    # confirmation_gate/mcp_tool_executor génériques — voir
    # flows/producer/flow.py::_resolve_delivery_otp.
    "PRODUCER_CONFIRM_DELIVERY_OTP": "producer_escrow",
}

_BREAKOUT_INTENTS = (
    "BUYER_VIEW_CART",
    "BUYER_LIST_ORDERS",
    "BUYER_CHECK_ORDER_STATUS",
    "BUYER_CANCEL_ORDER",
    # Ex-"MARKET_GET_REQUESTS" (split 2026-07-20) : les deux moitiés
    # conservent le même privilège de sortie de tunnel qu'avant le split.
    "MARKET_BROWSE_REQUESTS",
    "MARKET_MY_REQUESTS",
)

for _goal, _tunnel in _TUNNEL_ASSIGNMENTS.items():
    INTENT_CONFIG[_goal]["tunnel"] = _tunnel
for _goal in _BREAKOUT_INTENTS:
    INTENT_CONFIG[_goal]["breakout"] = True
del _goal, _tunnel


# =======================================================================
# INTENT_ROLE — Single Source of Truth for role-based intent filtering.
# Values: "PRODUCER" (vendor/farmer only), "BUYER" (buyer only), "BOTH".
# Derived sets PRODUCER_INTENTS / BUYER_INTENTS / COMMON_INTENTS in
# interpreter_routing.py are computed from this table; never edit them by hand.
# =======================================================================
INTENT_ROLE = {
    # STOCK — producer only
    "STOCK_REGISTER_HARVEST": "PRODUCER",
    "STOCK_RECORD_MOVEMENT": "PRODUCER",
    "STOCK_ADJUST": "PRODUCER",
    "STOCK_REMOVE_PARTIAL": "PRODUCER",
    "STOCK_DELETE": "PRODUCER",
    "STOCK_GET_SUMMARY": "PRODUCER",
    "STOCK_GET_DETAIL": "PRODUCER",
    "STOCK_GET_MOVEMENTS": "PRODUCER",
    "STOCK_UPDATE_LEVEL": "PRODUCER",
    # SALES — producer
    "SALES_PUBLISH_PRODUCT": "PRODUCER",
    "SALES_RECORD_DIRECT": "PRODUCER",
    "SALES_LIST_ORDERS": "PRODUCER",
    "SALES_PLACE_BID": "PRODUCER",
    "SALES_ACCEPT_CONTRACT": "PRODUCER",
    "SALES_GET_CATALOG": "PRODUCER",
    "SALES_UPDATE_PRODUCT": "PRODUCER",
    "SALES_UNPUBLISH_PRODUCT": "PRODUCER",
    "SALES_UPDATE_PRODUCTION": "PRODUCER",
    "PRODUCER_CONFIRM_DELIVERY_OTP": "PRODUCER",
    "PRODUCER_CONFIRM_DELIVERY_PAYMENT": "PRODUCER",
    "PRODUCER_CANCEL_ORDER": "PRODUCER",
    "MARKET_GET_MY_PROPOSALS": "PRODUCER",
    # PROCUREMENT — buyer
    "PROCUREMENT_CREATE_REQUEST": "BUYER",
    "PROCUREMENT_SELECT_WINNER": "BUYER",
    "PROCUREMENT_ACCEPT_OFFER": "BUYER",
    "BUYER_REQUEST": "BUYER",
    "MARKET_GET_REQUEST_DETAIL": "BOTH",
    # BUYER transactional tunnel (Panier → Précommande → Négociation)
    "BUYER_ADD_TO_CART": "BUYER",
    "BUYER_VIEW_CART": "BUYER",
    "BUYER_CREATE_PREORDER": "BUYER",
    "BUYER_PREORDER_INIT": "BUYER",
    "BUYER_PREORDER_CONFIRM": "BUYER",
    "BUYER_NEGOTIATE_PRICE": "BUYER",
    # Order Tracking — buyer only
    "BUYER_CHECK_ORDER_STATUS": "BUYER",
    "BUYER_LIST_ORDERS": "BUYER",
    "BUYER_CANCEL_ORDER": "BUYER",
    # Auction Tracking — buyer only
    "BUYER_LIST_AUCTIONS": "BUYER",
    "BUYER_CHECK_AUCTION_STATUS": "BUYER",
    # MARKET / SEARCH — both roles browse
    "MARKET_BROWSE_REQUESTS": "PRODUCER",
    "MARKET_MY_REQUESTS": "BUYER",
    "MARKET_SNAPSHOT": "BOTH",
    "MARKET_SNAPSHOT_ZONAL": "BOTH",
    "SEARCH_PRODUCTS": "BOTH",
    "SEARCH_NEARBY": "BOTH",
    "VALIDATE_PRICE": "BOTH",
    # CROP / AGRO — producer only
    "CROP_START_CYCLE": "PRODUCER",
    "DECLARE_CROP_CYCLE": "PRODUCER",
    "CROP_RECORD_INTERVENTION": "PRODUCER",
    "CROP_RECORD_OBSERVATION": "PRODUCER",
    "CROP_UPDATE_STAGE": "PRODUCER",
    "CROP_UPDATE_SOIL": "PRODUCER",
    "AGRO_GET_CYCLES": "PRODUCER",
    "AGRO_GET_STANDARDS": "BOTH",
    "AGRO_GET_ECONOMICS": "PRODUCER",
    "AGRO_GET_RISKS": "BOTH",
    # FARM
    "FARM_CREATE": "PRODUCER",
    "FARM_UPDATE": "PRODUCER",
    "FARM_GET_MY_LIST": "PRODUCER",
    # FINANCE
    "FINANCE_LOG_EXPENSE": "PRODUCER",
    "FINANCE_GET_SUMMARY": "PRODUCER",
    # PROFILE / SYSTEM
    "PROFILE_SET_GEO": "BOTH",
    "PROFILE_SET_PREFS": "BOTH",
    "PROFILE_SWITCH_ROLE": "BOTH",
    "PROFILE_GET_MCP_USER": "BOTH",
    "PROFILE_GET_TRUST": "BOTH",
    "PROFILE_GET_CONTEXT": "BOTH",
    "DASHBOARD_PRODUCER": "PRODUCER",
    "SYSTEM_REPORT_ANOMALY": "BOTH",
    "SYSTEM_BIND_ZONE": "BOTH",
    "SYSTEM_COMMIT_TRANSACTION": "BOTH",
    "SYSTEM_GET_PENDING": "BOTH",
}


# =======================================================================
# INTENT_DOMAIN — Categorical grouping for unit-normalization rules,
# coaching hints, and analytics. Adds clarity for the unit normalizer
# (e.g. "SAC" of millet ≈ 100kg, "PANIER" of tomato ≈ 25kg).
# =======================================================================
INTENT_DOMAIN = {
    k: (
        "STOCK"
        if k.startswith("STOCK_")
        else "SALES"
        if k.startswith("SALES_")
        else "PROCUREMENT"
        if k.startswith("PROCUREMENT_")
        else "MARKET"
        if k.startswith("MARKET_") or k.startswith("SEARCH_") or k == "VALIDATE_PRICE"
        else "CROP"
        if k.startswith("CROP_") or k.startswith("AGRO_") or k.startswith("DECLARE_")
        else "FARM"
        if k.startswith("FARM_")
        else "FINANCE"
        if k.startswith("FINANCE_")
        else "PROFILE"
        if k.startswith("PROFILE_") or k == "DASHBOARD_PRODUCER"
        else "SYSTEM"
    )
    for k in INTENT_CONFIG
}


# =======================================================================
# INTENT_DISAMBIGUATION — Pairs of intents that frequently overlap in
# user utterances. The semantic_disambiguation node consumes this table
# to render pedagogical AG-UI ListMenu prompts when the LLM confidence
# is low or when multiple plausible intents could match.
#
# (2026-09-08, clôture Bloc 1, mandat §21) : schéma canonique — `options`
# est la SEULE source des intents candidats. Un champ `candidates` séparé
# existait auparavant (liste redondante, toujours identique à l'ordre des
# intents dans `options` sur les 8 entrées de ce catalogue) et a été
# retiré : `nodes/semantic_disambiguation.py::extract_disambiguation_intents`
# est le point UNIQUE qui dérive la liste d'intents depuis `options`,
# consommé aussi bien pour construire le menu que pour peupler
# `intent_competition` (`nodes/cognitive.py`).
#
# Each entry: trigger_key → {
#     "title": str,                        # question shown to user
#     "options": [(intent, label), ...],   # intents candidats + libellés
#     "lexical_hints": [substring, ...],   # raw text triggers (post-normalize)
#     "pedagogical_hint": str,             # optional, extra context line
#     "roles": [str, ...],                 # optional, informational only
# }
# =======================================================================
INTENT_DISAMBIGUATION = {
    # "J'ai 300 poussins / 5 sacs / 100kg de mil" → state declaration
    #
    # RÉDUIT À 2 VOIES (2026-07-20, UX) : l'option "stock privé" (suivi interne,
    # invisible du marché) créait un 3ᵉ choix sans rapport avec la vente et
    # perdait le producteur — retirée de CE menu (le goal STOCK_REGISTER_HARVEST
    # reste utilisable via une commande explicite type "ajouter à mon stock",
    # juste plus proposé ici). Les 2 options restantes couvrent exactement la
    # vraie question qu'un producteur se pose : "c'est prêt à vendre maintenant,
    # ou pas encore ?" — langage produit, aucun jargon de plateforme.
    #
    # 3ᵉ voie ajoutée le 2026-07-17 (historique) : un producteur qui déclare des
    # POUSSINS, veaux, semis, jeunes plants... n'a RIEN de vendable maintenant —
    # c'est une PRODUCTION FUTURE avec une date de disponibilité. Router ça vers
    # STOCK_REGISTER_HARVEST (table Stock, aucune date de dispo) empêche tout
    # acheteur de savoir QUAND ce sera prêt — Stock ne porte pas cette notion,
    # contrairement à MarketOffer (estimated_available_at/expected_harvest_date,
    # preorder_enabled). Voir [[future-production-preorder-loop]].
    "STOCK_OR_SALES_DECLARATION": {
        "title": "C'est prêt à vendre maintenant, ou pas encore ?",
        "pedagogical_hint": (
            "💡 Des poussins, jeunes animaux, semis ou plants en cours de croissance "
            "ne sont PAS encore vendables — choisissez « Prêt plus tard » pour "
            "indiquer une date de disponibilité et permettre les précommandes."
        ),
        "options": [
            (
                "SALES_PUBLISH_PRODUCT",
                "📦 Prêt maintenant (disponible immédiatement, publié sur le marché)",
            ),
            (
                "DECLARE_CROP_CYCLE",
                "⏳ Prêt plus tard (récolte ou production à venir, avec une date)",
            ),
        ],
        "lexical_hints": [
            "j'ai",
            "j ai",
            "récolte",
            "recolte",
            "disponible",
            "en stock",
            "stocké",
            "poussin",
            "poussins",
            "veau",
            "veaux",
            "agneau",
            "agneaux",
            "chevreau",
            "semis",
            "jeune plant",
            "jeunes plants",
            "en cours de croissance",
            # Verbes de vente/publication BRUTS (sans info de disponibilité) :
            # "je veux publier des chèvres", "vendre des tomates"... doivent
            # TOUJOURS demander "prêt maintenant ou plus tard ?" plutôt que de
            # laisser le LLM router seul (il biaise les ANIMAUX vers la
            # production future / précommande — voir DECLARE_CROP_CYCLE, label
            # "élevage... précommande"). Décision produit 2026-08-05 : toujours
            # désambiguïser ce cas. Les entités déjà extraites (quantité/prix)
            # sont sauvegardées par semantic_disambiguation avant le menu, donc
            # aucun tour perdu — juste un choix explicite en 2 boutons.
            "publier",
            "publie",
            "publiez",
            "mettre en vente",
            "mets en vente",
            "mise en vente",
            "vendre",
            "proposer",
            "propose",
            "mettre sur le marché",
            "mettre sur le marche",
        ],
    },
    # "Je veux vendre" / "espace vendeur" — le producteur exprime une intention
    # de VENTE générique, sans produit/quantité/statut déjà précisé. Menu
    # consolidé "Espace Vendeur" (3 voies, création UX 2026-07-21) qui
    # regroupe les 3 vraies portes d'entrée de la vente producteur, chacune
    # déjà existante ailleurs mais jamais présentée ensemble comme point
    # d'entrée unique : publier maintenant, déclarer une future récolte, ou
    # répondre à un appel d'offres déjà ouvert par un acheteur. Hints choisis
    # volontairement multi-mots (jamais le seul mot "vendre") pour ne pas
    # détourner un message déjà précis ("j'ai 300 poussins à vendre") vers ce
    # menu générique — la sélection par hint le plus long (voir
    # semantic_disambiguation._detect_disambiguation_candidates) laisserait
    # sinon STOCK_OR_SALES_DECLARATION perdre face à un "vendre" trop court.
    "SELLER_HUB": {
        "title": "🧑‍🌾 Espace Vendeur — que souhaitez-vous faire ?",
        "options": [
            ("SALES_PUBLISH_PRODUCT", "📦 Publier un produit disponible maintenant"),
            ("DECLARE_CROP_CYCLE", "⏳ Déclarer une récolte ou production à venir"),
            ("MARKET_BROWSE_REQUESTS", "📢 Répondre à un appel d'offres d'un acheteur"),
        ],
        "roles": ["PRODUCER"],
        # Volontairement SANS "je veux vendre" / "vendre mes produits" : ces
        # préfixes matchent aussi un message déjà complet ("je veux vendre
        # 100kg de tomates à 300f maintenant"), qui doit être traité
        # directement plutôt que ré-interrompu par ce menu générique.
        "lexical_hints": [
            "comment vendre",
            "aide pour vendre",
            "aide à la vente",
            "espace vendeur",
            "menu vendeur",
            "options de vente",
            "que puis-je vendre",
            "comment vendre mes produits",
        ],
    },
    # "J'ai vendu 100kg" — already happened
    "STOCK_OR_SALE_RECORDING": {
        "title": "Voulez-vous enregistrer une vente ou une simple sortie de stock ?",
        "options": [
            (
                "SALES_RECORD_DIRECT",
                "💰 Vente : avec montant encaissé (impacte mon chiffre d'affaires)",
            ),
            (
                "STOCK_REMOVE_PARTIAL",
                "📤 Sortie : ajustement du stock sans revenu (perte, autoconsommation)",
            ),
        ],
        "lexical_hints": [
            "j'ai vendu",
            "j ai vendu",
            "donné",
            "donne",
            "perdu",
            "consommé",
        ],
    },
    # "Je cherche du mais" — buy via auction or just look at catalog
    "BUY_VS_BROWSE": {
        "title": "Voulez-vous consulter le catalogue ou lancer un appel d'offres ?",
        "options": [
            (
                "BUYER_REQUEST",
                "🛒 Catalogue : voir ce qui est disponible immédiatement",
            ),
            (
                "PROCUREMENT_CREATE_REQUEST",
                "📢 Appel d'offres : demander à nos producteurs de répondre (gros volumes, rupture de stock)",
            ),
        ],
        "lexical_hints": [
            "je cherche",
            "j'aimerais acheter",
            "il me faut",
            "besoin de",
        ],
    },
    # "Le maïs est à combien" — multiple market lookups
    "MARKET_PRICE_LOOKUP": {
        "title": "Quel type de prix recherchez-vous ?",
        "options": [
            ("MARKET_SNAPSHOT", "📊 Prix actuel près de chez vous"),
            ("MARKET_SNAPSHOT_ZONAL", "🗺️ Tendance dans une zone précise"),
            ("VALIDATE_PRICE", "✅ Vérifier mon prix face au marché"),
        ],
        "lexical_hints": ["combien", "prix du", "prix actuel", "cours du"],
    },
    "RESUME_TUNNEL": {
        "title": "Souhaitez-vous reprendre votre commande en attente ?",
        "options": [
            ("BUYER_PREORDER_INIT", "✅ Finaliser la commande"),
            ("BUYER_VIEW_CART", "🧺 Voir le panier"),
        ],
        "lexical_hints": [
            "reprendre",
            "reprends",
            "reprenons",
            "continuer",
            "continue",
            "continuer ma commande",
            "retour",
        ],
    },
    "ORDER_TRACKING_INTENT": {
        # Refonte double-rôle : "mes commandes" est structurellement ambigu
        # pour un utilisateur qui peut être acheteur ET producteur — sans la
        # 4ᵉ option (commandes REÇUES sur ses produits), un producteur qui
        # tape "mes commandes" se voyait proposer un menu 100% acheteur
        # (statut / liste / annulation d'une commande PASSÉE), sans aucune
        # issue vers ce qu'il cherchait réellement. Voir SALES_LIST_ORDERS
        # (`tool_name=get_producer_orders`, PRODUCER, intent.py).
        "title": "Que souhaitez-vous faire concernant vos commandes ?",
        "options": [
            (
                "BUYER_CHECK_ORDER_STATUS",
                "📋 Voir le statut d'une commande que j'ai passée",
            ),
            ("BUYER_LIST_ORDERS", "📦 Lister toutes les commandes que j'ai passées"),
            ("BUYER_CANCEL_ORDER", "❌ Annuler une commande que j'ai passée"),
            ("SALES_LIST_ORDERS", "🧾 Voir les commandes reçues sur mes produits"),
        ],
        "lexical_hints": [
            "ma commande",
            "mes commandes",
            "où est",
            "statut",
            "status",
            "suivi",
            "suivre",
            "tracking",
            "livraison",
            "annuler commande",
        ],
    },
    # RÉDUIT À 2 VOIES (2026-07-20, UX + bug) : la 3ᵉ option "Parcourir les
    # enchères du marché" pointait vers MARKET_GET_REQUESTS qui, côté ACHETEUR,
    # ne "parcourt" rien — elle montre les appels d'offres du buyer LUI-MÊME
    # (resolve_own_auctions, view_mode="MY_OWN"), donc un doublon trompeur de
    # l'option 1. Vocabulaire harmonisé : "appel d'offres" partout, plus jamais
    # "enchère" (terme réservé en interne, jamais montré à l'utilisateur).
    "AUCTION_TRACKING_INTENT": {
        "title": "Que souhaitez-vous faire concernant vos appels d'offres ?",
        "options": [
            ("BUYER_LIST_AUCTIONS", "📋 Voir mes appels d'offres (tous statuts)"),
            (
                "BUYER_CHECK_AUCTION_STATUS",
                "🔍 Détail d'un appel d'offres et propositions reçues",
            ),
        ],
        "roles": ["BUYER"],
        "lexical_hints": [
            "mes enchères",
            "mes encheres",
            "mon enchère",
            "mon enchere",
            "appel d'offres",
            "appel doffres",
            "appels d'offres",
            "mes appels",
            "mes appel",
            "mon appel",
            "suivre mes appel",
            "suivre mes appels",
            "suivi de mes appels",
            "voir mes appels",
            "mes demandes",
            "offres reçues",
            "offres recues",
            "état enchère",
            "etat enchere",
            "statut enchère",
        ],
    },
}


# =======================================================================
# SCHEMA-DRIVEN ACCESSORS — canonical functions consumed by executor,
# validator, TaskHandler, and flow logic. Single source of truth.
# =======================================================================


def get_intent_config(intent: str) -> dict:
    """Return full config dict for *intent*, or empty dict if unknown."""
    return INTENT_CONFIG.get((intent or "").upper(), {})


def get_required_fields(intent: str) -> list:
    """Return ``required`` field list for *intent*."""
    return list(get_intent_config(intent).get("required", []))


def get_tool_name(intent: str) -> str:
    """Return MCP ``tool_name`` bound to *intent*, or ``""``."""
    return get_intent_config(intent).get("tool_name", "")


def is_write_intent(intent: str) -> bool:
    """True if *intent* is classified as WRITE action_type."""
    return get_intent_config(intent).get("action_type") == "WRITE"


def intent_requires_farm(intent: str) -> bool:
    """True if *intent* requires a farm_id in the payload."""
    return bool(get_intent_config(intent).get("requires_farm"))


__all__ = [
    "INTENT_CONFIG",
    "INTENT_ROLE",
    "INTENT_DOMAIN",
    "INTENT_DISAMBIGUATION",
    "get_intent_config",
    "get_required_fields",
    "get_tool_name",
    "is_write_intent",
    "intent_requires_farm",
]
