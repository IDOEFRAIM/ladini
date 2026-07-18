"""Domain — schémas Pydantic (DTOs) et modèles ORM d'AgriConnect.

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
"""
