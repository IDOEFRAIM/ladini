# Couche Domaine (`domain/`)

Ce package centralise les modèles de données canoniques et les entités business de l'application. Il sert de "langage commun" (Ubiquitous Language) partagé entre les services de base de données, l'API et les graphes d'agents IA.

## Architecture & Organisation

domain/
├── init.py
├── orm_base.py          # Registre SQLAlchemy partagé (Base) et utilitaires communs
├── models.py            # Agrégateur (Ré-exportation de toutes les entités pour compatibilité)
├── identity/            # Authentification, profils et gestion des acteurs
│   ├── init.py
│   └── models.py        # User, Producer, BuyerProfile, TrustScore, DeliveryAgent...
├── catalog/             # Gestion de l'offre, des fermes et du stock
│   ├── init.py
│   └── models.py        # Farm, Product, Stock, StockMovement, MarketOffer...
├── orders/              # Flux transactionnels, enchères et logistique
│   ├── init.py
│   └── models.py        # Order, OrderItem, Payment, Auction, Bid, Delivery...
├── governance/          # Référentiels sectoriels, prix et zonages coopératifs
│   ├── init.py
│   └── models.py        # CooperativeZone, Category,StandardPrice, QualityNorm...
└── intelligence/        # Données opérationnelles IA et traçabilité des décisions
├── init.py



---

## Cartographie des Modules

### 1. Structure de Base (`orm_base.py`)
Contient la classe de base déclarative SQLAlchemy (`Base`) et les générateurs d'identifiants uniques (`_uuid4`). C'est le point d'ancrage de toutes les entités de l'application.

### 2. Gestion de l'Identité (`identity/`)
Regroupe le cœur du système d'authentification et les profils d'utilisateurs qualifiés.
*   **Classes clés :** `User`, `Account`, `Session`, `Producer`, `Client`, `BuyerProfile`, `DeliveryAgent`, `TrustScore`.
*   **Concepts clés :** Rôles applicatifs, segmentation des acheteurs (grossistes, retail, etc.), notation de confiance.

### 3. Catalogue & Production (`catalog/`)
Gère l'infrastructure physique des producteurs et le cycle de vie des produits avant vente.
*   **Classes clés :** `Warehouse`, `Farm`, `Product`, `Stock`, `StockMovement`, `Batch`, `Expense`, `MarketOffer`.
*   **Concepts clés :** Localisation des fermes, traçabilité des lots (`Batch`), mouvements de stock, valorisation financière des offres marché.

### 4. Commandes & Enchères (`orders/`)
Prend en charge l'intégralité du cycle transactionnel et de la supply chain.
*   **Classes clés :** `Order`, `OrderItem`, `Payment`, `Delivery`, `OrderStatusHistory`, `Auction`, `Bid`, `MarketplaceRating`.
*   **Concepts clés :** Tunnel d'achat, historique des statuts, gestion des enchères descendantes/ascendantes, arbitrages et litiges logistiques.

### 5. Gouvernance & Régulation (`governance/`)
Définit les règles métier collectives, le découpage territorial de la coopérative et les grilles tarifaires de référence.
*   **Classes clés :** `CooperativeZone`, `Category`, `StandardPrice`, `QualityNorm`, `RegulatoryAudit`.
*   **Concepts clés :** Prix planchers, normes de qualité agroalimentaire, audits de conformité.

### 6. Intelligence & Agentic Engine (`intelligence/`)
Stocke les contextes de décision des agents autonomes et la file d'attente des messages asynchrones.
*   **Classes clés :** `Conversation`, `AgentContextMemory`, `OutboxMessage`, `AgentActionLog`, `AgentTask`, `KnowledgeExtraction`.
*   **Concepts clés :** Persistance de la mémoire à long terme des agents, pattern Outbox pour les événements inter-services, audits des décisions IA.

---

## Directives de Développement

*   **Pureté des données :** Les fichiers de ce package ne doivent contenir **aucune entrée/sortie (I/O)** ni requêtes directes à la base de données. Ce sont des définitions de structures pures.
*   **Résolution des Relations :** Toutes les relations inter-fichiers SQLAlchemy (`relationship`) doivent utiliser des **références sous forme de chaînes de caractères** (ex: `relationship("Order", back_populates="items")`) plutôt que des importations directes de classes. Cela évite les importations circulaires.
*   **Évolutions de Schéma :** 
    1. Privilégiez toujours les changements ascendants compatibles (champs optionnels, valeurs par défaut).
    2. Enregistrez les modifications structurelles via une migration Alembic dédiée.
*   **Importations :** 
    *   *Dans les Mixins & Services transverses :* L'import global via l'agrégateur reste valide : `from ladini.domain.models import Order`.
    *   *Dans les modules isolés ou spécifiques (ex: Auth) :* Ciblez l'import précis pour une meilleure lisibilité : `