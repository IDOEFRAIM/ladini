"""Domain — schémas Pydantic (DTOs) et modèles ORM d'Ladini.

    models.py       ORM SQLAlchemy — source de vérité physique du schéma DB.
    identity/dto.py  User, Producer, BuyerProfile, TrustScore, UserContext
    catalog/dto.py   Farm, MarketOffer, Product
    orders/dto.py    Order, OrderItem, Payment, Auction, Bid, Delivery...

Chaque `dto.py` est un socle Pydantic v2 (`BaseMarketplaceModel`,
`base_model.py`) utilisé par les services `services/database/*_service.py`
pour valider/sérialiser à la frontière ORM ↔ appelant (`.model_validate(row)`
en lecture, DTO typé en paramètre en écriture). Un fichier n'a sa place ici
que s'il est réellement consommé par ces services — pas de couche théorique
en anticipation d'un besoin futur.

RÈGLES MÉTIER PARTAGÉES (2026-09-05, Phase 9)
---------------------------------------------
    quantity_unit.py   unités, conversions, quantités composées
    order_policy.py    seuil minimum de commande (politique PLATEFORME)
    pricing_tiers.py   paliers de prix, calcul de ligne, débit de stock

Ces trois modules étaient sous `graphs/agents/market_coach/` alors qu'ils
sont utilisés par la couche transactionnelle (`services/database/{buyer,
producer,product,escrow}.py`) — une inversion de dépendance : la
persistance dépendait de l'orchestration conversationnelle.

Ils sont PURS : aucune dépendance à LangGraph, à l'état conversationnel,
au LLM, au routage ou au rendu. Contrat vérifié par
`tests/architecture/test_service_does_not_depend_on_graph_domain.py` :

    ladini.domain  ne doit importer NI graphs, NI api, NI workers,
                        NI infrastructure, NI protocols.

Ce paquet est donc importable aussi bien par `services/` que par
`graphs/`, sans créer de cycle.
"""
