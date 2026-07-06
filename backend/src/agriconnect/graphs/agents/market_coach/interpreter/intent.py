

INTENT_CONFIG = {
    # =======================================================================
    # DOMAINE : GESTION DES STOCKS & RÉCOLTES (WRITE — Producteur)
    # =======================================================================
    "STOCK_REGISTER_HARVEST": {
        "tool_name": "add_stock",
        "required": ["product", "quantity_mentioned", "farm_id"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Mise en stock / Enregistrement d'une nouvelle récolte",
        "label_map": {
            "product": "produit/culture récolté",
            "quantity_mentioned": "quantité récoltée",
            "unit_mentioned": "unité",
            "farm_id": "exploitation source"
        }
    },
    "STOCK_RECORD_MOVEMENT": {
        "tool_name": "add_stock_movement_by_id",
        "required": ["stock_id", "movement_type", "quantity_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Enregistrement d'un mouvement de stock entrant/sortant",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "movement_type": "sens (Entrée/Sortie/Perte)",
            "quantity_mentioned": "quantité bougée",
            "reason": "motif"
        }
    },
    "STOCK_ADJUST": {
        "tool_name": "adjust_stock_by_id",
        "required": ["stock_id", "quantity_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Correction manuelle de l'inventaire physique",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "quantity_mentioned": "nouvelle quantité réelle constatée",
            "reason": "motif"
        }
    },
    "STOCK_REMOVE_PARTIAL": {
        "tool_name": "remove_stock_by_id",
        "required": ["stock_id", "quantity_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Retrait partiel du stock disponible",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "quantity_mentioned": "quantité à retirer"
        }
    },
    "STOCK_DELETE": {
        "tool_name": "delete_stock_by_id",
        "required": ["stock_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Suppression définitive d'une ligne de stock",
        "label_map": {"stock_id": "identifiant stock"}
    },
    "STOCK_UPDATE_LEVEL": {
        "tool_name": "adjust_stock_by_id",
        "required": ["stock_id", "quantity_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "label": "Mise à jour directe du niveau d'un lot",
        "label_map": {
            "stock_id": "identifiant stock",
            "quantity_mentioned": "nouvelle quantité réelle",
            "unit_mentioned": "unité (optionnel)",
            "reason": "motif"
        }
    },

    # =======================================================================
    # DOMAINE : MARCHÉ PRODUCTEUR — VENTES (WRITE — PRODUCER)
    # =======================================================================
    "SALES_PUBLISH_PRODUCT": {
        "tool_name": "create_product",
        "required": ["product", "price_mentioned", "quantity_mentioned"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Mise en vente d'un produit sur le catalogue public",
        "label_map": {
            "product": "nom du produit",
            "price_mentioned": "prix unitaire proposé",
            "quantity_mentioned": "quantité disponible",
            "unit_mentioned": "unité",
            "description": "détails"
        }
    },
    "SALES_RECORD_DIRECT": {
        "tool_name": "record_sale",
        "required": ["product", "quantity_mentioned", "price_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Enregistrement d'une vente directe (Cash / Gré à gré)",
        "label_map": {
            "product": "produit vendu",
            "quantity_mentioned": "quantité",
            "price_mentioned": "montant total de la vente",
            "unit_mentioned": "unité"
        }
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
        "required": ["auction_id", "price_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Proposition de vente face à une demande acheteur existante",
        "label_map": {
            "auction_id": "numéro de l'appel d'offres",
            "price_mentioned": "votre prix proposé",
            "quantity_mentioned": "quantité proposée",
            "message": "note"
        }
    },
    "SALES_ACCEPT_CONTRACT": {
        "tool_name": "commit_staged_transaction",
        "required": ["bid_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Validation définitive des termes du contrat verrouillé",
        "label_map": {"bid_id": "numéro de transaction/offre"}
    },
    "SALES_UPDATE_PRODUCT": {
        "tool_name": "update_product_price_and_qty",
        "required": ["product_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "lifecycle_mode": "UPDATE",
        "label": "Mise à jour du prix ou de la quantité d'un produit publié",
        "label_map": {
            "product_id": "référence produit",
            "price_mentioned": "nouveau prix unitaire",
            "quantity_mentioned": "nouvelle quantité disponible",
            "unit_mentioned": "unité (optionnel)"
        }
    },

    # =======================================================================
    # DOMAINE : MARCHÉ ACHETEUR — APPROVISIONNEMENT (WRITE — BUYER)
    # =======================================================================
    "PROCUREMENT_CREATE_REQUEST": {
        "tool_name": "create_auction",
        "required": ["product", "quantity_mentioned", "price_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Publication d'une demande d'approvisionnement / appel d'offres",
        "label_map": {
            "product": "produit recherché",
            "quantity_mentioned": "quantité totale cherchée",
            "price_mentioned": "prix plafond proposé",
            "unit_mentioned": "unité",
            "zone_name": "région de collecte",
            "deadline": "date limite"
        }
    },
    "PROCUREMENT_SELECT_WINNER": {
        "tool_name": "select_winning_bid",
        "required": ["auction_id", "bid_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Sélection et validation de l'offre gagnante sur mon marché",
        "label_map": {
            "auction_id": "numéro de votre appel d'offres",
            "bid_id": "numéro de la proposition retenue"
        }
    },
    "PROCUREMENT_ACCEPT_OFFER": {
        "tool_name": "accept_bid",
        "required": ["bid_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Achat direct simple d'un produit du catalogue indexé",
        "label_map": {"bid_id": "numéro du produit catalogue"}
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
            "quantity_mentioned": "quantité souhaitée (optionnel)",
            "unit_mentioned": "unité (optionnel)",
        }
    },

    # =======================================================================
    # DOMAINE : TUNNEL TRANSACTIONNEL ACHETEUR ("Grade Entreprise")
    # Panier multi-items → Précommande → Négociation. Le routage de phase
    # est déterministe (preorder_workflow["phase"]) dans le buyer_flow.
    # =======================================================================
    "BUYER_ADD_TO_CART": {
        "tool_name": "add_to_cart",
        "required": ["product", "quantity_mentioned", "unit_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Ajout d'un produit au panier de précommande",
        "label_map": {
            "product": "produit à ajouter",
            "quantity_mentioned": "quantité souhaitée",
            "unit_mentioned": "unité",
        }
    },
    "BUYER_VIEW_CART": {
        "tool_name": "view_cart",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Consultation du panier de précommande en cours",
        "label_map": {}
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
        }
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
        "required": ["product", "price_mentioned"],
        "action_type": "WRITE",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Ouverture d'une négociation de prix avec un producteur",
        "label_map": {
            "product": "produit à négocier",
            "price_mentioned": "prix proposé",
            "quantity_mentioned": "quantité concernée",
        }
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
        }
    },
    "BUYER_LIST_ORDERS": {
        "tool_name": "list_buyer_orders",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "handled_by_flow": True,
        "label": "Consultation du tableau de bord des commandes en cours",
        "label_map": {}
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
        }
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
            "variety": "variété/semence"
        }
    },
    "DECLARE_CROP_CYCLE": {
        "tool_name": "declare_future_production",
        "required": ["farm_id", "production_type", "product", "quantity_mentioned", "estimated_available_at", "price_mentioned"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Déclaration d'un lot futur (culture/élevage) pour précommande",
        "label_map": {
            "farm_id": "identifiant exploitation",
            "production_type": "type (CROP ou LIVESTOCK)",
            "product": "culture ou espèce",
            "quantity_mentioned": "quantité prévue",
            "unit_mentioned": "unité (KG, HEAD...)",
            "estimated_available_at": "date de disponibilité",
            "price_mentioned": "prix unitaire prévu",
            "surface": "superficie (si culture)",
            "breed": "race (si élevage)",
            "preorder_enabled": "précommande active (oui/non)",
            "is_public": "visible catalogue (oui/non)"
        }
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
            "quantity_mentioned": "quantité intrant",
            "details": "observations"
        }
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
            "observation": "notes de suivi"
        }
    },
    "CROP_UPDATE_STAGE": {
        "tool_name": "add_crop_growth_stage",
        "required": ["cycle_id", "stage_name"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Changement formel de stade phénologique",
        "label_map": {
            "cycle_id": "cycle de culture",
            "stage_name": "nom du nouveau stade"
        }
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
            "organic_matter": "taux matière organique"
        }
    },

    # =======================================================================
    # DOMAINE : FINANCES (WRITE — Producteur)
    # =======================================================================
    "FINANCE_LOG_EXPENSE": {
        "tool_name": "add_expense",
        "required": ["price_mentioned", "farm_id"],
        "action_type": "WRITE",
        "requires_farm": True,
        "label": "Enregistrement d'une dépense d'exploitation / charge",
        "label_map": {
            "price_mentioned": "montant dépense",
            "product": "nature/libellé charge",
            "category": "catégorie",
            "farm_id": "exploitation concernée"
        }
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
            "zone": "zone géographique"
        }
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
            "surface": "superficie"
        }
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
        "label_map": {"latitude": "latitude", "longitude": "longitude"}
    },
    "PROFILE_SET_PREFS": {
        "tool_name": "update_communication_prefs",
        "required": ["language"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Configuration langue et notifications",
        "label_map": {"language": "langue", "allow_voice": "notifications vocales"}
    },
    "PROFILE_SWITCH_ROLE": {
        "tool_name": "create_agent_action",
        "required": ["target_role"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Changement de mode d'interface (PRODUCER/BUYER)",
        "label_map": {"target_role": "rôle cible"}
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
            "description": "détails"
        }
    },
    "SYSTEM_BIND_ZONE": {
        "tool_name": "create_agent_action",
        "required": ["zone"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Rattachement territorial (zone agricole)",
        "label_map": {"zone": "nom zone"}
    },
    "SYSTEM_COMMIT_TRANSACTION": {
        "tool_name": "commit_staged_transaction",
        "required": ["staging_id"],
        "action_type": "WRITE",
        "requires_farm": False,
        "label": "Validation définitive d'une transaction verrouillée",
        "label_map": {"staging_id": "identifiant de staging"}
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
        "label_map": {"zone": "filtre zone", "product": "filtre produit"}
    },
    "STOCK_GET_DETAIL": {
        "tool_name": "get_farm_stocks",
        "required": ["farm_id"],
        "action_type": "READ",
        "requires_farm": True,
        "label": "Inventaire détaillé par exploitation",
        "label_map": {"farm_id": "identifiant exploitation"}
    },
    "STOCK_GET_MOVEMENTS": {
        "tool_name": "get_stock_movements",
        "required": ["stock_id"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Grand livre de traçabilité d'un stock",
        "label_map": {"stock_id": "identifiant stock"}
    },
    "SALES_GET_CATALOG": {
        "tool_name": "list_products",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Consultation de mon catalogue de produits en vente",
        "label_map": {"phone": "votre téléphone"}
    },
    "MARKET_GET_REQUESTS": {
        "tool_name": "get_auctions",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Liste des appels d'offres / demandes d'approvisionnement du marché",
        "label_map": {"zone_name": "zone", "product_name": "produit", "status": "statut"}
    },
    "MARKET_GET_REQUEST_DETAIL": {
        "tool_name": "get_auctions_bids",
        "required": ["auction_id"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Consultation des offres reçues sur mon appel d'offres",
        "label_map": {"auction_id": "identifiant enchère"}
    },
    "MARKET_GET_MY_PROPOSALS": {
        "tool_name": "get_my_active_bids",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Suivi de mes propositions de vente envoyées",
        "label_map": {"phone": "votre téléphone"}
    },
    "MARKET_SNAPSHOT": {
        "tool_name": "get_market_snapshot",
        "required": ["zone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Cours et prix actuel du marché local",
        "label_map": {"zone": "zone de cotation"}
    },
    "MARKET_SNAPSHOT_ZONAL": {
        "tool_name": "get_zone_market_overview",
        "required": ["zone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Volumes de transaction et tendances locaux",
        "label_map": {"zone": "zone"}
    },
    "AGRO_GET_CYCLES": {
        "tool_name": "get_crop_cycles",
        "required": ["farm_id"],
        "action_type": "READ",
        "requires_farm": True,
        "label": "Historique des cycles de culture d'un domaine",
        "label_map": {"farm_id": "identifiant exploitation"}
    },
    "AGRO_GET_STANDARDS": {
        "tool_name": "get_crop_requirements",
        "required": ["product"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Besoins biologiques théoriques d'une culture",
        "label_map": {"product": "culture"}
    },
    "AGRO_GET_ECONOMICS": {
        "tool_name": "get_cycle_economics",
        "required": ["cycle_id"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Bilan financier analytique d'une parcelle",
        "label_map": {"cycle_id": "identifiant cycle"}
    },
    "AGRO_GET_RISKS": {
        "tool_name": "get_active_sanitary_risks",
        "required": ["zone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Analyse des risques sanitaires régionaux",
        "label_map": {"zone": "zone"}
    },
    "FARM_GET_MY_LIST": {
        "tool_name": "get_producer_farm",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Liste de mes domaines et exploitations AgriConnect",
        "label_map": {"phone": "votre téléphone"}
    },
    "FINANCE_GET_SUMMARY": {
        "tool_name": "get_expense_summary",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": True,
        "label": "Bilan comptable synthétique d'exploitation",
        "label_map": {"phone": "téléphone producteur", "days": "historique (jours)"}
    },
    "PROFILE_GET_MCP_USER": {
        "tool_name": "get_user_by_phone",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Consultation profil AgriConnect par téléphone",
        "label_map": {"phone": "téléphone de recherche"}
    },
    "PROFILE_GET_TRUST": {
        "tool_name": "get_trust_score",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Note de confiance commerciale",
        "label_map": {"phone": "votre téléphone"}
    },
    "PROFILE_GET_CONTEXT": {
        "tool_name": "get_user_context",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Variables de session NLU/Agent (Zéro MCP)",
        "label_map": {}
    },
    "DASHBOARD_PRODUCER": {
        "tool_name": "get_producer_dashboard",
        "required": ["phone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Tableau de bord d'exploitation AgriConnect",
        "label_map": {"phone": "votre téléphone"}
    },
    "SEARCH_PRODUCTS": {
        "tool_name": "search_products",
        "required": ["product"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Recherche par mot-clé dans le catalogue catalogue",
        "label_map": {"product": "terme recherché"}
    },
    "SEARCH_NEARBY": {
        "tool_name": "get_all_zone_market_overview",
        "required": ["latitude", "longitude"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Recherche infrastructures / offres de proximité GPS",
        "label_map": {"latitude": "latitude", "longitude": "longitude"}
    },
    "VALIDATE_PRICE": {
        "tool_name": "check_price_anomaly",
        "required": ["product", "price_mentioned", "zone"],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Vérification cohérence prix face à la tendance marché",
        "label_map": {"product": "produit", "price_mentioned": "prix proposé", "zone": "marché référence"}
    },
    "SYSTEM_GET_PENDING": {
        "tool_name": "get_pending_actions",
        "required": [],
        "action_type": "READ",
        "requires_farm": False,
        "label": "Consultation des actions système en attente de traitement",
        "label_map": {}
    }
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


_CANONICAL_FIELD_ALIASES = {
    "product_name": "product",
    "produit": "product",
    "quantity_mentioned": "quantity",
    "quantity_for_sale": "quantity",
    "qty": "quantity",
    "unit_mentioned": "unit",
    "unite": "unit",
    "price_mentioned": "price",
    "prix": "price",
    "zone_name": "zone",
    "location": "zone",
}


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
    # MARKET / SEARCH — both roles browse
    "MARKET_GET_REQUESTS": "BOTH",
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
INTENT_DOMAIN = {k: ("STOCK"     if k.startswith("STOCK_")       else
                     "SALES"     if k.startswith("SALES_")       else
                     "PROCUREMENT" if k.startswith("PROCUREMENT_") else
                     "MARKET"    if k.startswith("MARKET_") or k.startswith("SEARCH_") or k == "VALIDATE_PRICE" else
                     "CROP"      if k.startswith("CROP_") or k.startswith("AGRO_") or k.startswith("DECLARE_") else
                     "FARM"      if k.startswith("FARM_") else
                     "FINANCE"   if k.startswith("FINANCE_") else
                     "PROFILE"   if k.startswith("PROFILE_") or k == "DASHBOARD_PRODUCER" else
                     "SYSTEM")
                 for k in INTENT_CONFIG}


# =======================================================================
# INTENT_DISAMBIGUATION — Pairs of intents that frequently overlap in
# user utterances. The semantic_disambiguation node consumes this table
# to render pedagogical AG-UI ListMenu prompts when the LLM confidence
# is low or when multiple plausible intents could match.
#
# Each entry: trigger_key → {
#     "candidates": [intent, ...],        # ≥ 2 alternatives
#     "title": str,                        # question shown to user
#     "options": [(intent, label), ...],   # pedagogical labels
#     "lexical_hints": [substring, ...],   # raw text triggers (post-normalize)
# }
# =======================================================================
INTENT_DISAMBIGUATION = {
    # "J'ai 300 poussins / 5 sacs / 100kg de mil" → state declaration
    "STOCK_OR_SALES_DECLARATION": {
        "candidates": ["STOCK_REGISTER_HARVEST", "SALES_PUBLISH_PRODUCT"],
        "title": "Comment souhaitez-vous enregistrer ce que vous avez ?",
        "options": [
            ("STOCK_REGISTER_HARVEST",
             "📦 Suivi privé : ajouter à mon stock interne (pas visible sur le marché)"),
            ("SALES_PUBLISH_PRODUCT",
             "🛒 Vente publique : publier sur le marché AgriConnect"),
        ],
        "lexical_hints": ["j'ai", "j ai", "récolte", "recolte", "disponible", "en stock", "stocké"],
    },
    # "J'ai vendu 100kg" — already happened
    "STOCK_OR_SALE_RECORDING": {
        "candidates": ["SALES_RECORD_DIRECT", "STOCK_REMOVE_PARTIAL"],
        "title": "Voulez-vous enregistrer une vente ou une simple sortie de stock ?",
        "options": [
            ("SALES_RECORD_DIRECT",
             "💰 Vente : avec montant encaissé (impacte mon chiffre d'affaires)"),
            ("STOCK_REMOVE_PARTIAL",
             "📤 Sortie : ajustement du stock sans revenu (perte, autoconsommation)"),
        ],
        "lexical_hints": ["j'ai vendu", "j ai vendu", "donné", "donne", "perdu", "consommé"],
    },
    # "Je cherche du mais" — buy via auction or just look at catalog
    "BUY_VS_BROWSE": {
        "candidates": ["BUYER_REQUEST", "PROCUREMENT_CREATE_REQUEST"],
        "title": "Voulez-vous consulter le catalogue ou lancer un appel d'offres ?",
        "options": [
            ("BUYER_REQUEST",
             "� Catalogue : voir ce qui est disponible immédiatement"),
            ("PROCUREMENT_CREATE_REQUEST",
             "� Appel d'offres : demander à nos producteurs de répondre (gros volumes, rupture de stock)"),
        ],
        "lexical_hints": ["je cherche", "j'aimerais acheter", "il me faut", "besoin de"],
    },
    # "Le maïs est à combien" — multiple market lookups
    "MARKET_PRICE_LOOKUP": {
        "candidates": ["MARKET_SNAPSHOT", "MARKET_SNAPSHOT_ZONAL", "VALIDATE_PRICE"],
        "title": "Quel type de prix recherchez-vous ?",
        "options": [
            ("MARKET_SNAPSHOT", "📊 Prix actuel près de chez vous"),
            ("MARKET_SNAPSHOT_ZONAL", "🗺️ Tendance dans une zone précise"),
            ("VALIDATE_PRICE", "✅ Vérifier mon prix face au marché"),
        ],
        "lexical_hints": ["combien", "prix du", "prix actuel", "cours du"],
    },
    "RESUME_TUNNEL": {
        "candidates": ["BUYER_PREORDER_INIT", "BUYER_VIEW_CART"],
        "title": "Souhaitez-vous reprendre votre commande en attente ?",
        "options": [
            ("BUYER_PREORDER_INIT", "✅ Finaliser la commande"),
            ("BUYER_VIEW_CART", "🧺 Voir le panier"),
        ],
        "lexical_hints": ["reprendre", "reprends", "reprenons", "continuer", "continue", "continuer ma commande", "retour"],
    },
    "ORDER_TRACKING_INTENT": {
        "candidates": ["BUYER_CHECK_ORDER_STATUS", "BUYER_LIST_ORDERS", "BUYER_CANCEL_ORDER"],
        "title": "Que souhaitez-vous faire concernant vos commandes ?",
        "options": [
            ("BUYER_CHECK_ORDER_STATUS", "📋 Voir le statut d'une commande"),
            ("BUYER_LIST_ORDERS", "📦 Lister toutes mes commandes"),
            ("BUYER_CANCEL_ORDER", "❌ Annuler une commande"),
        ],
        "roles": ["BUYER"],
        "lexical_hints": [
            "ma commande", "mes commandes", "où est", "statut", "status",
            "suivi", "suivre", "tracking", "livraison", "annuler commande",
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
    "INTENT_CONFIG", "INTENT_ROLE", "INTENT_DOMAIN", "INTENT_DISAMBIGUATION",
    "get_intent_config", "get_required_fields", "get_tool_name",
    "is_write_intent", "intent_requires_farm",
]