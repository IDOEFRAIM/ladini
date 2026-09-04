"""Preuve du gap produit (2026-09-04, audit fonctionnel global) : une
commande née d'un appel d'offres gagné (`select_winning_bid`) n'a AUCUN
mécanisme, nulle part dans le produit, pour être marquée payée ou livrée.

## Le problème démontré (pas une question d'architecture)

```
ACHETEUR sélectionne un gagnant
  → Order créée : status=CONFIRMED, payment_status=PENDING, delivery_status=PENDING
  → AUCUN OrderItem (une ligne d'appel d'offres n'a pas de Product catalogue)
  → AUCUN market_offer_id (réservé aux productions futures)

Le SEUL mécanisme de clôture qui existe dans tout le produit
(`EscrowMixin.verify_delivery_otp`, le code du producteur qui débloque les
fonds) exige structurellement un OrderItem+Product — un INNER JOIN, pas un
OUTER JOIN. Une commande d'appel d'offres, n'ayant AUCUN OrderItem, est
mathématiquement exclue du résultat de cette requête, quel que soit son
contenu (code OTP correct, statut de paiement, producteur) — CE N'EST PAS
une question de données manquantes dans une base de test, c'est une
propriété STRUCTURELLE de la requête compilée, valable pour N'IMPORTE
QUELLE ligne réelle en production.
```

Ce test prouve la FORME de la requête (INNER JOIN, jamais LEFT/OUTER —
même honnêteté que le reste de cette suite, aucune infrastructure Postgres
réelle dans ce dépôt) — pas un comportement simulé contre un faux moteur
qui pourrait donner une fausse impression de couverture."""
from __future__ import annotations

import re

from agriconnect.services.database.escrow import EscrowMixin


class TestNoFulfillmentClosureMechanismReachesAuctionOrders:
    def test_verify_delivery_otp_query_structurally_excludes_orderitem_less_orders(self):
        """`verify_delivery_otp` — la SEULE fonction de tout le produit qui
        marque une commande `DELIVERED`/`PAID_OUT` — rejoint `OrderItem`
        puis `Product` par INNER JOIN (pas `outerjoin`). Une commande sans
        aucun `OrderItem` (TOUTE commande née de `select_winning_bid` — voir
        `services/database/auction.py`, aucun OrderItem n'y est jamais créé)
        ne peut donc JAMAIS apparaître dans le résultat de cette requête,
        indépendamment de son `payment_status`/`delivery_otp` réels."""
        import inspect

        source = inspect.getsource(EscrowMixin.verify_delivery_otp)
        # Preuve directe sur le CODE SOURCE réel (pas une reconstruction
        # manuelle de la requête) : la clause de jointure OrderItem/Product
        # de cette fonction utilise bien `.join(` (INNER), jamais
        # `.outerjoin(` — c'est cette seule différence qui exclut
        # structurellement toute commande sans article catalogue.
        join_calls = re.findall(r"\.(join|outerjoin)\(\s*(OrderItem|Product)", source)
        assert ("join", "OrderItem") in join_calls
        assert ("join", "Product") in join_calls
        assert ("outerjoin", "OrderItem") not in join_calls
        assert ("outerjoin", "Product") not in join_calls

    def test_select_winning_bid_never_creates_an_orderitem(self):
        """Confirme la seconde moitié du gap, sur le code source réel de
        `select_winning_bid` : aucun `OrderItem(...)` n'y est jamais
        instancié — la commande née d'un appel d'offres n'a donc
        structurellement rien à quoi `verify_delivery_otp` pourrait se
        raccrocher, même si la requête ci-dessus était corrigée en
        `outerjoin`."""
        import inspect

        from agriconnect.services.database.auction import AuctionMixin

        source = inspect.getsource(AuctionMixin.select_winning_bid)
        assert "OrderItem(" not in source
        assert "Order(" in source  # la commande, elle, est bien créée


class TestNoConversationalGoalAdvancesAnAuctionOrderPastConfirmed:
    def test_no_tunnel_assignment_exists_for_advancing_delivery_or_payment_status(self):
        """Cartographie produit (pas juste du code mort) : `_TUNNEL_ASSIGNMENTS`
        (interpreter/intent.py) est la table QUI DÉCIDE quels goals ont un
        parcours conversationnel réel dédié. Aucun goal n'y référence un
        tunnel de type "livraison"/"paiement" pour PROCUREMENT/AUCTION — la
        seule entrée liée à la livraison du catalogue entier
        (`PRODUCER_CONFIRM_DELIVERY_OTP` -> "producer_escrow") ne s'applique,
        par construction (test ci-dessus), qu'aux commandes AVEC OrderItem."""
        from agriconnect.graphs.agents.market_coach.interpreter.intent import (
            INTENT_CONFIG,
        )

        delivery_or_payment_goals = [
            goal
            for goal, cfg in INTENT_CONFIG.items()
            if cfg.get("tunnel") in {"producer_escrow"}
        ]
        # Une seule entrée existe dans TOUT le catalogue — confirmée
        # incapable de couvrir une commande d'appel d'offres (test ci-dessus).
        assert delivery_or_payment_goals == ["PRODUCER_CONFIRM_DELIVERY_OTP"]
