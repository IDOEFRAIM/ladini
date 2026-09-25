from ladini.graphs.agents.market_coach.core.slots import (
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
    # (2026-09-14, Deep Intent Architecture Cleanup) : STOCK_RECORD_MOVEMENT/
    # STOCK_ADJUST/STOCK_REMOVE_PARTIAL/STOCK_DELETE/STOCK_UPDATE_LEVEL
    # SUPPRIMÉS — leurs `tool_name` (`*_by_id`) n'ont jamais correspondu à
    # une méthode DB réelle (les vraies méthodes `adjust_stock`/
    # `remove_stock`/`delete_stock`/`add_stock_movement` existent mais sous
    # un autre nom, jamais recâblées) ET aucune entité MCP exposée ne les
    # sert (`infrastructure/mcp/exposure.py` audité, confirmé absent). Ce
    # n'était pas une simple dépréciation d'exposition (l'état antérieur,
    # `_DEPRECATED_INTENTS`) : ces 5 goals n'ont jamais eu de chemin
    # d'exécution possible, ce chantier supprime le faux bouton lui-même
    # plutôt que de continuer à le masquer. Voir le rapport final pour
    # l'audit complet (docs/PRODUCT_INTENT_SCOPE_2026-09-04.md, §5, avait
    # déjà documenté cette absence sans encore trancher la suppression).
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
        # (2026-09-11) Label CONTRASTÉ avec BUYER_LIST_ORDERS — un producteur
        # qui tape "commandes reçues" se faisait classer BUYER_LIST_ORDERS
        # (achats) par le LLM, les deux labels étant trop proches. Première
        # correction (garder EN NOTE, ne pas répéter l'erreur) : simplement
        # AJOUTER le mot "commandes" comme exemple explicite dans les DEUX
        # labels a résolu ce cas mais en a introduit un nouveau — "je veux
        # commandes des poulets" (faute de frappe pour "commander", une
        # intention d'ACHAT neuve, pas une consultation d'historique) se
        # faisait alors classer BUYER_LIST_ORDERS à cause du seul mot
        # "commandes" présent dans le label. Leçon : ancrer sur la SÉMANTIQUE
        # de l'intention (HISTORIQUE d'un fait déjà survenu vs NOUVELLE
        # envie), jamais sur la présence d'un mot-clé isolé — le catalogue
        # LLM (`_build_dynamic_interpreter_prompt`) n'affiche QUE ce `label`,
        # c'est le SEUL levier de classification ici (aucun fast-path par
        # mots-clés fixes dans `routing.py`, le LLM est la seule source de
        # vérité).
        "label": (
            "Producteur/vendeur consulte l'HISTORIQUE des ventes DÉJÀ REÇUES "
            "sur ses propres produits (une transaction déjà conclue par un "
            "acheteur) — PAS ses propres achats, PAS une nouvelle envie "
            "d'achat/vente. Ex: \"commandes reçues\", \"mes ventes\"."
        ),
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
    # (2026-09-14, Deep Intent Architecture Cleanup) : SALES_ACCEPT_CONTRACT
    # SUPPRIMÉ — `commit_staged_transaction` n'existe ni comme outil MCP ni
    # comme méthode DB, aucune entité `Contract`/`StagedTransaction` dans le
    # domaine ; `SYSTEM_COMMIT_TRANSACTION` (même tool_name fictif, ci-
    # dessous) confirme qu'il s'agit d'un reliquat d'un flux "staging"
    # jamais implémenté, remplacé depuis par les Drafts + CAS. Les 3
    # interprétations possibles étaient déjà documentées sans preuve
    # (docs/PRODUCT_INTENT_SCOPE_2026-09-04.md §8) — aucune n'est
    # confirmée par le code, donc pas de fonctionnalité réelle à préserver.
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
    #
    # (2026-09-14, Deep Intent Architecture Cleanup, spec §13/§26) : RENOMMÉ
    # depuis `SALES_UPDATE_PRODUCTION` — namespace `PRODUCTION_*` cohérent
    # avec `PRODUCTION_DECLARE_FUTURE` (même entité `cycle_id`/MarketOffer),
    # jamais `SALES_*` (catalogue de produits déjà disponibles, domaine
    # distinct). Aucun alias créé (spec §3) : l'ancien nom n'a aucune
    # nécessité runtime démontrée à préserver.
    "PRODUCTION_UPDATE_FUTURE": {
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
        # (2026-09-12) Label enrichi d'exemples de PHRASAGE réel — incident
        # réel : un producteur qui vient de voir sa liste "commandes reçues"
        # numérotée (services/database/producer.py, formatted_menu SANS
        # contexte de sélection actif — voir la note dans flow.py juste
        # au-dessus) tape "annuler le 1" pour désigner la commande #1 de
        # cette liste. Le label précédent ("Annulation d'une commande que je
        # ne peux pas honorer") ne mentionnait ni "annuler", ni une
        # référence par NUMÉRO — le LLM classait ce message en UNKNOWN au
        # lieu de PRODUCER_CANCEL_ORDER, malgré un résolveur déjà fonctionnel
        # (`_resolve_order_for_cancellation`) prêt à afficher SON PROPRE menu
        # de sélection dès que ce goal est correctement détecté.
        #
        # (2026-09-14, incident réel confirmé une 2e fois) : même échec sur
        # "annuler" tout SEUL, sans numéro — le cas le PLUS fréquent en
        # pratique : `_producer_sales_block`/`get_producer_orders`
        # (flows/buyer/order_tracking.py, services/database/producer.py)
        # invitent EXPLICITEMENT le producteur à "Tapez *confirmer* ... ou
        # *annuler* ..." quand une seule vente 🟡 est affichée — aucun
        # numéro n'est alors nécessaire (`_resolve_order_for_cancellation`
        # auto-sélectionne déjà la commande unique). Le mot nu, sans le
        # numéro qui servait de seul signal fort dans les exemples
        # précédents, retombait en UNKNOWN.
        "label": (
            "Annulation/refus d'une commande PAR LE PRODUCTEUR — commande pas "
            "encore confirmée par lui (en attente de sa confirmation) OU déjà "
            "confirmée mais qu'il ne peut finalement plus honorer (ex: "
            "\"annuler\" ou \"annuler la commande 1\" en réponse à une liste de "
            "ventes venant d'être affichée, \"annuler le 1\", \"je ne peux pas "
            "honorer cette commande\", \"refuser la commande 2\") — référence "
            "par numéro/position d'une liste déjà vue, ou SANS numéro quand "
            "une seule commande vient d'être montrée — PAS une annulation par "
            "l'acheteur (BUYER_CANCEL_ORDER), PAS l'acceptation "
            "(PRODUCER_CONFIRM_ORDER)"
        ),
        "label_map": {
            "order_id": "numéro de la commande à annuler",
            "reason": "motif (rupture, aléa de production…)",
        },
    },
    "PRODUCER_CONFIRM_ORDER": {
        "tool_name": "confirm_order_by_producer",
        "required": ["order_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        # (2026-09-14, incident réel — même diagnostic que le label miroir
        # PRODUCER_CANCEL_ORDER ci-dessus) : "confirmer" tout SEUL, sans
        # numéro, en réponse à une liste de ventes n'affichant qu'UNE
        # commande 🟡, est le cas le PLUS fréquent — `_resolve_order_for_
        # confirmation` auto-sélectionne déjà cette commande unique, aucun
        # numéro n'est requis pour que la résolution fonctionne.
        "label": (
            "Confirmation PAR LE PRODUCTEUR qu'il peut honorer une commande "
            "reçue et pas encore confirmée (ex: \"confirmer\" ou \"je confirme "
            "la commande 1\" en réponse à une liste de ventes venant d'être "
            "affichée, \"j'accepte\", \"ok pour la commande 2\", \"je peux "
            "honorer\") — référence par numéro/position d'une liste déjà vue, "
            "ou SANS numéro quand une seule commande vient d'être montrée — "
            "PAS une simple consultation (SALES_LIST_ORDERS), PAS l'annulation "
            "(PRODUCER_CANCEL_ORDER), PAS la clôture livraison/paiement "
            "après préparation (PRODUCER_CONFIRM_DELIVERY_PAYMENT)"
        ),
        "label_map": {
            "order_id": "numéro de la commande à confirmer",
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
    # (2026-09-14) : jusqu'ici un acheteur ne pouvait JAMAIS corriger un
    # appel d'offres déjà publié (mauvaise quantité, prix plafond à ajuster,
    # date limite à repousser) — seulement le laisser expirer et en recréer
    # un depuis zéro. Même principe que SALES_UPDATE_PRODUCT/PRODUCTION_
    # UPDATE_FUTURE : `auction_id` est résolu conversationnellement (liste
    # des appels d'offres OUVERTS de l'acheteur, `_resolve_auction_for_
    # update`, flows/buyer/order_tracking.py), jamais demandé comme UUID
    # brut. `tool_name` reste une étiquette symbolique — ce goal est géré
    # par le tunnel `auction_tracking`, qui appelle directement
    # `AuctionGateway.update_auction`.
    "PROCUREMENT_UPDATE_REQUEST": {
        "tool_name": "update_auction_fields",
        "required": ["auction_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        # Comme BUYER_LIST_AUCTIONS/BUYER_CHECK_AUCTION_STATUS (même tunnel
        # "auction_tracking") : entièrement pris en charge par
        # `_resolve_auction_for_update` (flows/buyer/order_tracking.py),
        # jamais par un handler `actions/*.py` générique — sans ce flag,
        # `registry.py::validate_integrity()` exigerait un handler enregistré
        # qui n'existerait jamais (goal flow-handled par construction).
        "handled_by_flow": True,
        "label": (
            "Mise à jour d'un appel d'offres déjà publié par l'acheteur "
            "(quantité, prix plafond, date limite) — ex: \"modifier mon appel "
            "d'offres\", \"changer la quantité de ma demande de riz\", "
            "\"je veux repousser la date limite\" — PAS la création d'un "
            "nouvel appel d'offres (PROCUREMENT_CREATE_REQUEST)"
        ),
        "label_map": {
            "auction_id": "référence de l'appel d'offres",
            "quantity": "nouvelle quantité recherchée",
            "price": "nouveau prix plafond",
            "unit": "unité (optionnel)",
            "deadline": "nouvelle date limite",
        },
    },
    # (2026-09-14, Deep Intent Architecture Cleanup) : PROCUREMENT_SELECT_
    # WINNER/PROCUREMENT_ACCEPT_OFFER SUPPRIMÉS — handlers neutralisés
    # intentionnellement par F4 (anti-bypass sécurité, contournement d'un
    # tunnel de sélection sécurisé) ; `select_winning_bid` reste un outil
    # réel mais son SEUL point d'entrée sûr est désormais le tunnel
    # `BUYER_CHECK_AUCTION_STATUS` → confirmation/finalisation — jamais une
    # classification NEW_TASK directe. `accept_bid` (ACCEPT_OFFER) n'est de
    # toute façon exposé par aucun outil MCP réel. Laisser ces 2 goals
    # classables les rendait ATTEIGNABLES pour ne récolter qu'une erreur
    # technique (ou pire, rouvrir la surface de contournement si le
    # handler neutralisé était un jour "réparé" par inadvertance) — un seul
    # chemin métier vers la sélection du gagnant, jamais deux.
    # =======================================================================
    # DOMAINE : APPROVISIONNEMENT RÉCURRENT (Phase 2, 2026-09) — besoin
    # PERMANENT d'un acheteur ("40 kg de tomate chaque jour"), distinct de
    # PROCUREMENT_CREATE_REQUEST (appel d'offres ponctuel, un seul gagnant)
    # ET de BUYER_REQUEST/BUYER_ADD_TO_CART (achat immédiat). `handled_by_flow`
    # : gère sa PROPRE confirmation via `RecurringNeedDraft` (même pattern CAS
    # que PROCUREMENT_CREATE_REQUEST), jamais confirmation_gate/
    # mcp_tool_executor génériques — voir flows/buyer/recurring_need.py.
    # =======================================================================
    "CREATE_RECURRING_NEED": {
        "tool_name": "create_recurring_need",
        "required": ["product", "quantity", "unit", "recurrence_type"],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": (
            "Besoin d'approvisionnement RÉCURRENT/PERMANENT (ex: \"40 kg de "
            "tomates chaque jour\") — PAS un achat ponctuel (BUYER_REQUEST) "
            "ni un appel d'offres (PROCUREMENT_CREATE_REQUEST)"
        ),
        "label_map": {
            "product": "produit souhaité",
            "quantity": "quantité par occurrence",
            "unit": "unité",
            "recurrence_type": "fréquence (chaque jour, certains jours, chaque semaine, chaque mois, une seule fois)",
            "weekly_days": "jours de la semaine concernés",
            "excluded_weekdays": "jours exclus (ex: sauf le dimanche)",
            "max_price_per_unit": "prix maximum accepté (optionnel)",
        },
    },
    # Modifie un besoin récurrent DÉJÀ CRÉÉ : quantité/fréquence permanente,
    # pause, reprise, annulation, exception ponctuelle — UNE seule action
    # structurée (voir services/database/recurring_supply.py::
    # RECURRING_NEED_ACTIONS), jamais un intent séparé par verbe (mandat
    # Phase 2 §6 : "ne crée pas des intents séparés pour chacune de ces
    # actions"). Le besoin visé est résolu conversationnellement (liste des
    # besoins actifs de l'acheteur), jamais demandé comme UUID brut — même
    # principe que PROCUREMENT_UPDATE_REQUEST.
    "UPDATE_RECURRING_NEED": {
        "tool_name": "update_recurring_need",
        "required": [],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": (
            "Modifie/suspend/reprend/annule un besoin récurrent EXISTANT "
            "(ex: \"passe mes tomates à 25 kg\", \"suspend cette semaine\", "
            "\"demain seulement 10 kg\") — PAS une création (CREATE_RECURRING_NEED)"
        ),
        "label_map": {
            "product": "produit concerné",
            "quantity": "nouvelle quantité",
            "occurrence_date": "date concernée",
        },
    },
    "GET_MY_NEEDS": {
        "tool_name": "list_my_recurring_needs",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Liste mes besoins d'approvisionnement récurrents",
        "label_map": {},
    },
    "BUYER_REQUEST": {
        "tool_name": "search_products",
        "required": ["product"],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        # (2026-09-11) Ex: "je veux commander/acheter des poulets" — une
        # NOUVELLE envie d'acquérir un produit, MÊME si le message contient
        # le mot "commande(s)" (faute de frappe fréquente pour "commander").
        # PAS BUYER_LIST_ORDERS/SALES_LIST_ORDERS (qui consultent un
        # historique déjà existant) — voir leurs labels pour l'incident réel
        # que ce contraste corrige (bug 2026-09-11 : "je veux commandes des
        # poulets" classé à tort comme consultation de commandes passées).
        "label": (
            "Acheteur exprime une NOUVELLE envie d'acquérir un produit "
            "maintenant (recherche catalogue avant appel d'offres) — même si "
            "le message contient le mot \"commande(s)\" (ex: \"je veux "
            "commander des poulets\")"
        ),
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
        # (2026-09-11) Contraste avec SALES_LIST_ORDERS ET avec BUYER_REQUEST
        # — voir le commentaire détaillé sur SALES_LIST_ORDERS (même
        # incident : "je veux commandes des poulets", faute de frappe pour
        # "commander", une NOUVELLE envie d'achat, se faisait classer ICI à
        # cause du seul mot "commandes"). Le label ancre maintenant sur
        # HISTORIQUE (fait déjà survenu) vs NOUVELLE envie, pas sur un mot.
        "label": (
            "Acheteur consulte l'HISTORIQUE de ses achats DÉJÀ passés (une "
            "commande déjà existante) — PAS les ventes reçues sur ses "
            "propres produits, PAS une nouvelle envie d'acheter/commander un "
            "produit maintenant (ça, c'est BUYER_REQUEST). Ex: "
            "\"mes commandes\", \"où est ma commande\"."
        ),
        "label_map": {},
    },
    "BUYER_CANCEL_ORDER": {
        "tool_name": "cancel_order",
        "required": [],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": (
            "Annulation d'une commande en attente PAR L'ACHETEUR (ce qu'il a "
            "commandé) — PAS l'annulation/refus d'une vente reçue PAR LE "
            "PRODUCTEUR (PRODUCER_CANCEL_ORDER)"
        ),
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
    # DOMAINE : PRODUCTION FUTURE (WRITE — Producteur)
    # (2026-09-14, Deep Intent Architecture Cleanup, spec §11/§13) : l'ancien
    # module "agronomie complète" (CROP_START_CYCLE/RECORD_INTERVENTION/
    # RECORD_OBSERVATION/UPDATE_STAGE/UPDATE_SOIL + AGRO_GET_*, plus bas dans
    # ce fichier) est SUPPRIMÉ — audit confirmé (docs/PRODUCT_INTENT_SCOPE_
    # 2026-09-04.md §6) : aucune méthode DB, aucun modèle de suivi cultural,
    # aucune référence produit réelle. Seule capacité agronomique VIVANTE :
    # la déclaration d'une production future pour précommande, ci-dessous —
    # RENOMMÉE depuis `PRODUCTION_DECLARE_FUTURE` (namespace `PRODUCTION_*`, jamais
    # `CROP_*` : ce n'est ni un suivi de culture ni une écriture agronomique,
    # c'est un lot du marketplace pas encore disponible).
    # =======================================================================
    "PRODUCTION_DECLARE_FUTURE": {
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
        # (2026-09-14) Label sémantique, pas un journal d'incident : la
        # frontière avec STOCK_REGISTER_HARVEST (déjà récolté/disponible
        # maintenant) et SALES_PUBLISH_PRODUCT (prêt à vendre maintenant)
        # est PAS ENCORE DISPONIBLE / date future — voir les tests
        # contrastifs dédiés plutôt qu'un paragraphe historique ici.
        "label": (
            "Déclaration d'une production PAS ENCORE disponible (récolte ou "
            "élevage à venir, avec une date future) pour précommande — "
            "jamais un produit déjà prêt à vendre (SALES_PUBLISH_PRODUCT) ni "
            "déjà récolté (STOCK_REGISTER_HARVEST)"
        ),
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
    # =======================================================================
    # DOMAINE : FINANCES (WRITE — Producteur)
    # (2026-09-14, Deep Intent Architecture Cleanup) : re-exposées (retirées
    # du masquage `_DISABLED_INTENT_PREFIXES`) — audit confirmé : capacité
    # RÉELLE et intégralement câblée (`add_expense`/`get_expense_summary`
    # existent comme méthodes DB ET comme outils MCP exposés,
    # `@register_action` enregistrés, rendu de confirmation dédié). Le
    # masquage d'origine (revue de coût LLM, août 2026) répondait à une
    # pression sur le quota Groq que new_task_v2 (Incrément F, ~1400 tokens
    # contre ~3990 pour l'ancien catalogue universel) a largement résorbée
    # — masquer une fonctionnalité VIVANTE pour une raison de coût devenue
    # marginale contredit le principe de ce chantier ("1 intent
    # classifiable = 1 objectif utilisateur réel").
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
    # (2026-09-14, Deep Intent Architecture Cleanup, spec §21) : PROFILE_
    # SWITCH_ROLE SUPPRIMÉ — `create_agent_action` n'existe pas comme outil,
    # et la refonte double-rôle (2026-09-08) a rendu le concept OBSOLÈTE :
    # un même utilisateur vend et achète message par message, sans jamais
    # "changer de mode". `tests/evals/blocked/PROFILE_SWITCH_ROLE.md`
    # confirmait déjà "échafaudé mais branché à rien" avant même cette
    # refonte.
    #
    # (2026-09-14) : le namespace SYSTEM_* WRITE en entier (SYSTEM_
    # REPORT_ANOMALY/BIND_ZONE/COMMIT_TRANSACTION) est SUPPRIMÉ —
    # `report_anomaly`/`create_agent_action`/`commit_staged_transaction` ne
    # sont exposés par AUCUN outil MCP réel (audit `infrastructure/mcp/
    # exposure.py`) ; `bind_user_to_zone` existe comme méthode DB mais
    # n'est lui non plus jamais exposé, et aucune phrase utilisateur
    # crédible ne correspond à ces opérations (spec §23) — ce sont des
    # capacités opérationnelles/internes jamais devenues des intentions
    # utilisateur réelles, pas juste des "faux boutons" à masquer de plus.
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
    # (2026-09-14, Deep Intent Architecture Cleanup) : STOCK_GET_MOVEMENTS
    # SUPPRIMÉ du catalogue classifiable — `get_stock_movements` reste un
    # outil MCP réel et testé (domain/stock.py::StockService.get_movements,
    # CONSERVÉ, pas supprimé), mais un historique de mouvements n'a de sens
    # que si les mouvements sont enregistrables (STOCK_RECORD_MOVEMENT,
    # supprimé ci-dessus, jamais câblé) — décision produit de cohérence,
    # pas une absence d'implémentation.
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
    # maintenance. Remplacé par deux goals explicites, chacun mono-rôle —
    # `MARKET_BROWSE_REQUESTS` (producteur) ci-dessous, `BUYER_LIST_AUCTIONS`
    # (acheteur, voir plus haut) pour l'autre moitié.
    #
    # (2026-09-14, Deep Intent Architecture Cleanup, spec §16/§19) :
    # MARKET_MY_REQUESTS et MARKET_GET_REQUEST_DETAIL SUPPRIMÉS — doublons
    # HIGH-overlap confirmés, jamais de vraie 2e décision à trancher :
    #   - MARKET_MY_REQUESTS : le commentaire déjà présent depuis 2026-07-21
    #     confirmait "fusionné dans list_buyer_auctions/order_tracking.py"
    #     — même `tool_name` (`get_auctions`), même tunnel
    #     (`auction_tracking`) que `BUYER_LIST_AUCTIONS`, jamais retiré du
    #     catalogue malgré la fusion déjà faite côté flow.
    #   - MARKET_GET_REQUEST_DETAIL : `tool_name="get_auctions_bids"` alors
    #     que sa signature réelle (`get_auctions_bids(phone, status)`,
    #     `services/database/auction.py`) n'accepte même pas `auction_id`
    #     (son propre `required`) — contrat interne incohérent, jamais
    #     réellement exécutable tel que déclaré. `BUYER_CHECK_AUCTION_STATUS`
    #     (`get_auction_bids(auction_id, phone)`, la bonne méthode) couvre
    #     déjà exactement ce besoin ("détail d'un appel d'offres et offres
    #     reçues"), rôle BOTH inclus (le filtrage par rôle n'existe plus).
    "MARKET_BROWSE_REQUESTS": {
        "tool_name": "get_auctions",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Parcours des appels d'offres du marché (producteur cherche à répondre)",
        "label_map": {"zone": "zone", "product": "produit", "status": "statut"},
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
    # (2026-09-14, Deep Intent Architecture Cleanup) : MARKET_SNAPSHOT_ZONAL
    # SUPPRIMÉ — `get_zone_market_overview` inexistant, doublon strict de
    # `MARKET_SNAPSHOT` (`get_market_snapshot`, même `required=["zone"]`),
    # qui reste exposé et fonctionnel.
    # (2026-09-14, Deep Intent Architecture Cleanup) : AGRO_GET_CYCLES/
    # STANDARDS/ECONOMICS/RISKS SUPPRIMÉS avec le reste du module agronomie
    # (voir plus haut, "DOMAINE : PRODUCTION FUTURE") — mêmes preuves
    # (aucune méthode DB, aucun outil MCP exposé).
    "FARM_GET_MY_LIST": {
        "tool_name": "get_producer_farm",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Liste de mes domaines et exploitations Ladini",
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
        "label": "Consultation profil Ladini par téléphone",
        "label_map": {"phone": "téléphone de recherche"},
    },
    # (2026-09-14, Deep Intent Architecture Cleanup) : PROFILE_GET_TRUST/
    # DASHBOARD_PRODUCER/SEARCH_NEARBY SUPPRIMÉS — outils inexistants,
    # aucun agrégat/méthode DB équivalent (`get_trust_score`,
    # `get_producer_dashboard`, `get_all_zone_market_overview` absents de
    # `infrastructure/mcp/exposure.py`).
    #
    # PROFILE_GET_CONTEXT SUPPRIMÉ (pas seulement masqué comme avant) —
    # `get_user_context` n'existe nulle part, y compris en usage interne
    # (`get_buyer_context` est la méthode réellement utilisée en interne,
    # sans rapport avec cet intent) : ni une intention utilisateur ni une
    # capability interne vivante, spec §22.
    #
    # SEARCH_PRODUCTS SUPPRIMÉ (spec §17, HIGH overlap confirmé) — MÊME
    # `tool_name` (`search_products`) que `BUYER_REQUEST`, sans
    # `handled_by_flow` ni intégration au flow buyer réel : un doublon
    # générique jamais réellement distinct de la vraie recherche acheteur.
    # Une seule intention canonique retenue : `BUYER_REQUEST`.
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
    # (2026-09-14, Deep Intent Architecture Cleanup) : SYSTEM_GET_PENDING
    # SUPPRIMÉ avec le reste du namespace SYSTEM_* WRITE (voir plus haut) —
    # `get_pending_actions` n'est exposé par aucun outil MCP réel, et aucune
    # phrase utilisateur crédible ne correspond à cette opération purement
    # opérationnelle (spec §23).
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
    # Mise à jour d'un appel d'offres déjà publié — gère sa propre
    # confirmation (corrections vs annulation), jamais confirmation_gate/
    # mcp_tool_executor génériques. Réutilise le tunnel "auction_tracking"
    # (déjà routé vers order_tracking_resolver) : la sélection de l'appel
    # d'offres à modifier réutilise la même liste que BUYER_LIST_AUCTIONS.
    "PROCUREMENT_UPDATE_REQUEST": "auction_tracking",
    # Approvisionnement récurrent (Phase 2) — gère sa propre confirmation
    # (CREATE via RecurringNeedDraft, UPDATE/GET sans draft, résolution
    # conversationnelle), jamais confirmation_gate/mcp_tool_executor
    # génériques. Un seul tunnel pour les 3 : voir flows/buyer/recurring_need.py.
    "CREATE_RECURRING_NEED": "recurring_need",
    "UPDATE_RECURRING_NEED": "recurring_need",
    "GET_MY_NEEDS": "recurring_need",
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
    "PRODUCTION_UPDATE_FUTURE": "producer_update",
    # (2026-09-15, correctif RACINE — incident répété "confirmer"/"annuler"
    # nu jamais compris) : sans cette entrée, ces deux goals ne matchent
    # AUCUNE règle de `DomainRouter` (core/router.py) et retombent sur son
    # `fallback_router` (`make_route_after_validator`), qui route TOUT
    # `PendingInteraction(CONFIRM_ACTION)` porté par un goal à action
    # enregistrée vers `to_confirmation` (confirmation_gate/
    # mcp_tool_executor génériques) — AVANT même que `_resolve_pending_
    # order_action` (flows/producer/flow.py, tunnel auto-suffisant fusionné
    # confirmer/annuler) n'ait la moindre chance de s'exécuter pour le tour
    # de confirmation. Résultat observé : le verrou posé par `flows/buyer/
    # order_tracking.py::list_orders` était bien créé, mais le tour SUIVANT
    # ("confirmer") ne le retrouvait jamais — confirmation_gate exécute
    # correctement CONFIRM (même action que le résolveur), mais REJECT n'y
    # signifie qu'"abandonner", jamais "annuler la commande" (deux actions
    # RÉELLEMENT différentes ici, `confirm_order_by_producer` vs
    # `cancel_confirmed_order` — voir `_finalize_pending_order_action`).
    # Avec cette entrée, `PRODUCER_RESOLVER_GOALS`... (dérivé de ce tunnel,
    # core/goals.py) matche la règle `RouteRule(goals=PRODUCER_UPDATE_GOALS,
    # target="to_resolver")` en PREMIER (`DomainRouter.decide`, AVANT toute
    # bascule vers le fallback) — même garantie de routage que SALES_UPDATE_
    # PRODUCT/PRODUCTION_UPDATE_FUTURE ci-dessus, empiriquement déjà
    # vérifiée fonctionnelle en production.
    "PRODUCER_CONFIRM_ORDER": "producer_update",
    "PRODUCER_CANCEL_ORDER": "producer_update",
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
    # Ex-"MARKET_GET_REQUESTS" (split 2026-07-20) — l'autre moitié
    # (`MARKET_MY_REQUESTS`) a été supprimée le 2026-09-14 (doublon
    # confirmé de `BUYER_LIST_AUCTIONS`, voir Deep Intent Architecture
    # Cleanup) ; elle ne portait de toute façon pas ce privilège de
    # breakout elle-même.
    "MARKET_BROWSE_REQUESTS",
    # (2026-09-13, confirmation explicite producteur) : incident réel
    # double-rôle — un producteur avec le menu "mes achats" (buyer, sa
    # propre commande) encore actif tapait "confirmer"/"annuler" pour agir
    # sur SES VENTES (producteur) ; sans ce privilège de breakout,
    # `cognitive_guard` pouvait exiger un seuil de confiance au lieu de
    # laisser passer directement, alors que `BUYER_CANCEL_ORDER` (l'action
    # miroir côté acheteur) l'a déjà. Mêmes verbes d'action non ambigus,
    # même traitement.
    "PRODUCER_CANCEL_ORDER",
    "PRODUCER_CONFIRM_ORDER",
    # (2026-09-14, incident WhatsApp #8) : `SALES_LIST_ORDERS` (consultation
    # pure des ventes reçues, mode READ) n'avait jamais ce privilège alors
    # que son miroir exact côté acheteur (`BUYER_LIST_ORDERS`, juste
    # au-dessus) l'a depuis le début — asymétrie jamais remarquée. Un
    # producteur avec un tunnel `BUYER_PREORDER_CONFIRM` resté actif (parfois
    # une précommande déjà finalisée par un autre chemin, jamais nettoyée —
    # `expected=LOCATION` figé indéfiniment) tapant "mes commandes reçues"
    # se voyait pourtant CORRECTEMENT classé `NEW_TASK`/`SALES_LIST_ORDERS`
    # par le LLM, mais `goal_planner` gardait le tunnel verrouillé faute de
    # ce privilège — reprenant la confirmation d'une précommande périmée
    # (échec silencieux, message d'erreur incompréhensible) au lieu
    # d'afficher la liste demandée. Une simple CONSULTATION ne devrait
    # jamais rester bloquée derrière un tunnel d'écriture, quel qu'il soit.
    "SALES_LIST_ORDERS",
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
    "STOCK_GET_SUMMARY": "PRODUCER",
    "STOCK_GET_DETAIL": "PRODUCER",
    # SALES — producer
    "SALES_PUBLISH_PRODUCT": "PRODUCER",
    "SALES_RECORD_DIRECT": "PRODUCER",
    "SALES_LIST_ORDERS": "PRODUCER",
    "SALES_PLACE_BID": "PRODUCER",
    "SALES_GET_CATALOG": "PRODUCER",
    "SALES_UPDATE_PRODUCT": "PRODUCER",
    "SALES_UNPUBLISH_PRODUCT": "PRODUCER",
    "PRODUCTION_UPDATE_FUTURE": "PRODUCER",
    "PRODUCER_CONFIRM_DELIVERY_OTP": "PRODUCER",
    "PRODUCER_CONFIRM_DELIVERY_PAYMENT": "PRODUCER",
    "PRODUCER_CANCEL_ORDER": "PRODUCER",
    "PRODUCER_CONFIRM_ORDER": "PRODUCER",
    "MARKET_GET_MY_PROPOSALS": "PRODUCER",
    # PROCUREMENT — buyer
    "PROCUREMENT_CREATE_REQUEST": "BUYER",
    "PROCUREMENT_UPDATE_REQUEST": "BUYER",
    # Approvisionnement récurrent (Phase 2) — buyer only
    "CREATE_RECURRING_NEED": "BUYER",
    "UPDATE_RECURRING_NEED": "BUYER",
    "GET_MY_NEEDS": "BUYER",
    "BUYER_REQUEST": "BUYER",
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
    # MARKET — both roles browse
    "MARKET_BROWSE_REQUESTS": "PRODUCER",
    "MARKET_SNAPSHOT": "BOTH",
    "VALIDATE_PRICE": "BOTH",
    # PRODUCTION FUTURE — producer only
    "PRODUCTION_DECLARE_FUTURE": "PRODUCER",
    # FARM
    "FARM_CREATE": "PRODUCER",
    "FARM_UPDATE": "PRODUCER",
    "FARM_GET_MY_LIST": "PRODUCER",
    # FINANCE
    "FINANCE_LOG_EXPENSE": "PRODUCER",
    "FINANCE_GET_SUMMARY": "PRODUCER",
    # PROFILE
    "PROFILE_SET_GEO": "BOTH",
    "PROFILE_SET_PREFS": "BOTH",
    "PROFILE_GET_MCP_USER": "BOTH",
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
        if k.startswith("MARKET_") or k == "VALIDATE_PRICE"
        # (2026-09-14, Deep Intent Architecture Cleanup) : domaine "CROP"
        # renommé "PRODUCTION" — l'ancien module agronomique (CROP_*/AGRO_*)
        # est supprimé, seule survit la déclaration/mise à jour de
        # production future, désormais namespace `PRODUCTION_*` lui-même.
        else "PRODUCTION"
        if k.startswith("PRODUCTION_")
        else "FARM"
        if k.startswith("FARM_")
        else "FINANCE"
        if k.startswith("FINANCE_")
        else "PROFILE"
        if k.startswith("PROFILE_")
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
                "PRODUCTION_DECLARE_FUTURE",
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
            # production future / précommande — voir PRODUCTION_DECLARE_FUTURE, label
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
            ("PRODUCTION_DECLARE_FUTURE", "⏳ Déclarer une récolte ou production à venir"),
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
    # (2026-09-14, Deep Intent Architecture Cleanup) : "STOCK_OR_SALE_
    # RECORDING" SUPPRIMÉ — son unique alternative à SALES_RECORD_DIRECT
    # (STOCK_REMOVE_PARTIAL, "sortie sans revenu") est supprimée du
    # catalogue (aucun outil MCP réel). "j'ai vendu 100kg" route désormais
    # directement vers SALES_RECORD_DIRECT — plus de désambiguïsation
    # nécessaire, il n'y a plus qu'une seule intention valide pour ce
    # phrasé.
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
