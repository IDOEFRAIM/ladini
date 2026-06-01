

INTENT_CONFIG = {
    # =======================================================================
    # DOMAINE : GESTION DES STOCKS & RÉCOLTES (WRITE — Producteur)
    # =======================================================================
    "STOCK_REGISTER_HARVEST": {
        "required": ["product", "quantity_mentioned", "farm_id"],
        "label": "Mise en stock / Enregistrement d'une nouvelle récolte",
        "label_map": {
            "product": "produit/culture récolté",
            "quantity_mentioned": "quantité récoltée",
            "unit_mentioned": "unité",
            "farm_id": "exploitation source"
        }
    },
    "STOCK_RECORD_MOVEMENT": {
        "required": ["stock_id", "movement_type", "quantity_mentioned"],
        "label": "Enregistrement d'un mouvement de stock entrant/sortant",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "movement_type": "sens (Entrée/Sortie/Perte)",
            "quantity_mentioned": "quantité bougée",
            "reason": "motif"
        }
    },
    "STOCK_ADJUST": {
        "required": ["stock_id", "quantity_mentioned"],
        "label": "Correction manuelle de l'inventaire physique",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "quantity_mentioned": "nouvelle quantité réelle constatée",
            "reason": "motif"
        }
    },
    "STOCK_REMOVE_PARTIAL": {
        "required": ["stock_id", "quantity_mentioned"],
        "label": "Retrait partiel du stock disponible",
        "label_map": {
            "stock_id": "référence stock (numéro)",
            "quantity_mentioned": "quantité à retirer"
        }
    },
    "STOCK_DELETE": {
        "required": ["stock_id"],
        "label": "Suppression définitive d'une ligne de stock",
        "label_map": {"stock_id": "identifiant stock"}
    },

    # =======================================================================
    # DOMAINE : MARCHÉ PRODUCTEUR — VENTES (WRITE — PRODUCER)
    # =======================================================================
    "SALES_PUBLISH_PRODUCT": {
        # Mappage strict MCP create_product
        "required": ["product", "price_mentioned", "quantity_mentioned"],
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
        # Mappage strict MCP record_sale
        "required": ["product", "quantity_mentioned", "price_mentioned"],
        "label": "Enregistrement d'une vente directe (Cash / Gré à gré)",
        "label_map": {
            "product": "produit vendu",
            "quantity_mentioned": "quantité",
            "price_mentioned": "montant total de la vente",
            "unit_mentioned": "unité"
        }
    },
    "SALES_PLACE_BID": {
        # Réponse producteur à un appel d'offres acheteur
        "required": ["auction_id", "price_mentioned"],
        "label": "Proposition de vente face à une demande acheteur existante",
        "label_map": {
            "auction_id": "numéro de l'appel d'offres",
            "price_mentioned": "votre prix proposé",
            "quantity_mentioned": "quantité proposée",
            "message": "note"
        }
    },
    "SALES_ACCEPT_CONTRACT": {
        # Acceptation producteur après sélection par l'acheteur
        "required": ["bid_id"],
        "label": "Validation définitive des termes du contrat verrouillé",
        "label_map": {"bid_id": "numéro de transaction/offre"}
    },

    # =======================================================================
    # DOMAINE : MARCHÉ ACHETEUR — APPROVISIONNEMENT (WRITE — BUYER)
    # =======================================================================
    "PROCUREMENT_CREATE_REQUEST": {
        # Création d'un appel d'offres (je cherche X tonnes de Y)
        "required": ["product", "quantity_mentioned", "price_mentioned"],
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
        # Validation d'un acheteur sur une offre producteur reçue
        "required": ["auction_id", "bid_id"],
        "label": "Sélection et validation de l'offre gagnante sur mon marché",
        "label_map": {
            "auction_id": "numéro de votre appel d'offres",
            "bid_id": "numéro de la proposition retenue"
        }
    },
    "PROCUREMENT_ACCEPT_OFFER": {
        # Achat direct sur catalogue producteur
        "required": ["bid_id"],
        "label": "Achat direct simple d'un produit du catalogue indexé",
        "label_map": {"bid_id": "numéro du produit catalogue"}
    },

    # =======================================================================
    # DOMAINE : AGRONOMIE — PILOTAGE DE CULTURE (WRITE)
    # =======================================================================
    "CROP_START_CYCLE": {
        "required": ["farm_id", "product", "surface"],
        "label": "Démarrage d'un nouveau cycle de culture (semis/plantation)",
        "label_map": {
            "farm_id": "identifiant exploitation",
            "product": "culture",
            "surface": "superficie parcelle",
            "variety": "variété/semence"
        }
    },
    "CROP_RECORD_INTERVENTION": {
        "required": ["cycle_id", "intervention_type"],
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
        "required": ["cycle_id", "stage_label", "observation"],
        "label": "Enregistrement d'un suivi de croissance ou diagnostic",
        "label_map": {
            "cycle_id": "cycle de culture",
            "stage_label": "stade observé",
            "observation": "notes de suivi"
        }
    },
    "CROP_UPDATE_STAGE": {
        "required": ["cycle_id", "stage_name"],
        "label": "Changement formel de stade phénologique",
        "label_map": {
            "cycle_id": "cycle de culture",
            "stage_name": "nom du nouveau stade"
        }
    },
    "CROP_UPDATE_SOIL": {
        "required": ["farm_id", "ph"],
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
        "required": ["price_mentioned", "farm_id"],
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
        "required": ["farm_name", "surface", "zone"],
        "label": "Déclaration d'une nouvelle exploitation agricole",
        "label_map": {
            "farm_name": "nom domaine",
            "surface": "superficie totale",
            "zone": "zone géographique"
        }
    },
    "FARM_UPDATE": {
        "required": ["farm_id"],
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
        "required": ["latitude", "longitude"],
        "label": "Mise à jour de votre position GPS réelle",
        "label_map": {"latitude": "latitude", "longitude": "longitude"}
    },
    "PROFILE_SET_PREFS": {
        "required": ["language"],
        "label": "Configuration langue et notifications",
        "label_map": {"language": "langue", "allow_voice": "notifications vocales"}
    },
    "PROFILE_SWITCH_ROLE": {
        "required": ["target_role"],
        "label": "Changement de mode d'interface (PRODUCER/BUYER)",
        "label_map": {"target_role": "rôle cible"}
    },

    # =======================================================================
    # SYSTEME & SÉCURITÉ (WRITE)
    # =======================================================================
    "SYSTEM_REPORT_ANOMALY": {
        "required": ["target_id", "anomaly_type", "description"],
        "label": "Signalement d'une anomalie technique/marché",
        "label_map": {
            "target_id": "cible problème",
            "anomaly_type": "type incident",
            "description": "détails"
        }
    },
    "SYSTEM_BIND_ZONE": {
        "required": ["zone"],
        "label": "Rattachement territorial (zone agricole)",
        "label_map": {"zone": "nom zone"}
    },
    "SYSTEM_COMMIT_TRANSACTION": {
        "required": ["staging_id"],
        "label": "Validation définitive d'une transaction verrouillée",
        "label_map": {"staging_id": "identifiant de staging"}
    },

    # =======================================================================
    # intentions DE LECTURE (READ — Consultations MCP Réelles)
    # Mappage strict sémantique des label_map
    # =======================================================================
    "STOCK_GET_SUMMARY": {
        "required": [],
        "label": "Inventaire global multi-sites (Outil: get_stocks)",
        "label_map": {"zone": "filtre zone", "product": "filtre produit"}
    },
    "STOCK_GET_DETAIL": {
        "required": ["farm_id"],
        "label": "Inventaire détaillé par exploitation",
        "label_map": {"farm_id": "identifiant exploitation"}
    },
    "STOCK_GET_MOVEMENTS": {
        "required": ["stock_id"],
        "label": "Grand livre de traçabilité d'un stock",
        "label_map": {"stock_id": "identifiant stock"}
    },
    "SALES_GET_CATALOG": {
        "required": ["phone"],
        "label": "Consultation de mon catalogue de produits en vente",
        "label_map": {"phone": "votre téléphone"}
    },
    "MARKET_GET_REQUESTS": {
        "required": [],
        "label": "Liste des appels d'offres / demandes d'approvisionnement du marché",
        "label_map": {"zone_name": "zone", "product_name": "produit", "status": "statut"}
    },
    "MARKET_GET_REQUEST_DETAIL": {
        "required": ["auction_id"],
        "label": "Consultation des offres reçues sur mon appel d'offres",
        "label_map": {"auction_id": "identifiant enchère"}
    },
    "MARKET_GET_MY_PROPOSALS": {
        "required": ["phone"],
        "label": "Suivi de mes propositions de vente envoyées",
        "label_map": {"phone": "votre téléphone"}
    },
    "MARKET_SNAPSHOT": {
        "required": ["zone"],
        "label": "Cours et prix actuel du marché local",
        "label_map": {"zone": "zone de cotation"}
    },
    "MARKET_SNAPSHOT_ZONAL": {
        "required": ["zone"],
        "label": "Volumes de transaction et tendances locaux",
        "label_map": {"zone": "zone"}
    },
    "AGRO_GET_CYCLES": {
        "required": ["farm_id"],
        "label": "Historique des cycles de culture d'un domaine",
        "label_map": {"farm_id": "identifiant exploitation"}
    },
    "AGRO_GET_STANDARDS": {
        "required": ["product"],
        "label": "Besoins biologiques théoriques d'une culture",
        "label_map": {"product": "culture"}
    },
    "AGRO_GET_ECONOMICS": {
        "required": ["cycle_id"],
        "label": "Bilan financier analytique d'une parcelle",
        "label_map": {"cycle_id": "identifiant cycle"}
    },
    "AGRO_GET_RISKS": {
        "required": ["zone"],
        "label": "Analyse des risques sanitaires régionaux",
        "label_map": {"zone": "zone"}
    },
    "FARM_GET_MY_LIST": {
        "required": ["phone"],
        "label": "Liste de mes domaines et exploitations AgriConnect",
        "label_map": {"phone": "votre téléphone"}
    },
    "FINANCE_GET_SUMMARY": {
        "required": ["phone"],
        "label": "Bilan comptable synthétique d'exploitation",
        "label_map": {"phone": "téléphone producteur", "days": "historique (jours)"}
    },
    "PROFILE_GET_MCP_USER": {
        "required": ["phone"],
        "label": "Consultation profil AgriConnect par téléphone",
        "label_map": {"phone": "téléphone de recherche"}
    },
    "PROFILE_GET_TRUST": {
        "required": ["phone"],
        "label": "Note de confiance commerciale",
        "label_map": {"phone": "votre téléphone"}
    },
    "PROFILE_GET_CONTEXT": {
        "required": [],
        "label": "Variables de session NLU/Agent (Zéro MCP)",
        "label_map": {}
    },
    "DASHBOARD_PRODUCER": {
        "required": ["phone"],
        "label": "Tableau de bord d'exploitation AgriConnect",
        "label_map": {"phone": "votre téléphone"}
    },
    "SEARCH_PRODUCTS": {
        "required": ["product"],
        "label": "Recherche par mot-clé dans le catalogue catalogue",
        "label_map": {"product": "terme recherché"}
    },
    "SEARCH_NEARBY": {
        "required": ["latitude", "longitude"],
        "label": "Recherche infrastructures / offres de proximité GPS",
        "label_map": {"latitude": "latitude", "longitude": "longitude"}
    },
    "VALIDATE_PRICE": {
        "required": ["product", "price_mentioned", "zone"],
        "label": "Vérification cohérence prix face à la tendance marché",
        "label_map": {"product": "produit", "price_mentioned": "prix proposé", "zone": "marché référence"}
    },
    "SYSTEM_GET_PENDING": {
        "required": [],
        "label": "Consultation des actions système en attente de traitement",
        "label_map": {}
    }
}


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
    # SALES — producer
    "SALES_PUBLISH_PRODUCT": "PRODUCER",
    "SALES_RECORD_DIRECT": "PRODUCER",
    "SALES_PLACE_BID": "PRODUCER",
    "SALES_ACCEPT_CONTRACT": "PRODUCER",
    "SALES_GET_CATALOG": "PRODUCER",
    "MARKET_GET_MY_PROPOSALS": "PRODUCER",
    # PROCUREMENT — buyer
    "PROCUREMENT_CREATE_REQUEST": "BUYER",
    "PROCUREMENT_SELECT_WINNER": "BUYER",
    "PROCUREMENT_ACCEPT_OFFER": "BUYER",
    "MARKET_GET_REQUEST_DETAIL": "BUYER",
    # MARKET / SEARCH — both roles browse
    "MARKET_GET_REQUESTS": "BOTH",
    "MARKET_SNAPSHOT": "BOTH",
    "MARKET_SNAPSHOT_ZONAL": "BOTH",
    "SEARCH_PRODUCTS": "BOTH",
    "SEARCH_NEARBY": "BOTH",
    "VALIDATE_PRICE": "BOTH",
    # CROP / AGRO — producer only
    "CROP_START_CYCLE": "PRODUCER",
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
                     "CROP"      if k.startswith("CROP_") or k.startswith("AGRO_") else
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
        "candidates": ["PROCUREMENT_CREATE_REQUEST", "SEARCH_PRODUCTS"],
        "title": "Voulez-vous lancer un appel d'offres ou consulter le catalogue ?",
        "options": [
            ("PROCUREMENT_CREATE_REQUEST",
             "📣 Appel d'offres : les producteurs proposent leurs prix (recommandé pour gros volumes)"),
            ("SEARCH_PRODUCTS",
             "🔎 Catalogue : voir ce qui est déjà publié à prix fixe"),
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
}


__all__ = ["INTENT_CONFIG", "INTENT_ROLE", "INTENT_DOMAIN", "INTENT_DISAMBIGUATION"]