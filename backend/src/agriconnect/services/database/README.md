# `services/database` — Couche d'accès aux données AgriConnect

Ce dossier est **l'unique point d'accès à la base de données** dans tout le backend AgriConnect. Aucun autre module ne doit ouvrir de session SQLAlchemy directement. Voici l'architecture complète, fichier par fichier, pattern par pattern.

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Arborescence des fichiers](#2-arborescence-des-fichiers)
3. [Infrastructure de connexion](#3-infrastructure-de-connexion)
   - [core/database.py](#coredatabasepy--moteur-unique)
   - [engine.py](#enginepy--facade-de-compatibilité)
4. [Gestion des sessions et transactions](#4-gestion-des-sessions-et-transactions)
   - [base_service.py — ContextVar + @transactional](#base_servicepy--contextvar--transactional)
   - [d.py — AgriDatabaseService (dispatcher central)](#dpy--agridatabaseservice-dispatcher-central)
5. [Mixins de domaine](#5-mixins-de-domaine)
   - [base.py — Résolution d'identité](#basepy--résolution-didentité-basemixin)
   - [auth.py — Mutations d'identité](#authpy--mutations-didentité-authmixin)
   - [producer.py — Producteurs, fermes, stocks](#producerpy--producteurs-fermes-stocks-producermgmtmixin)
   - [marketplace.py — Stock direct et ventes](#marketplacepy--stock-direct-et-ventes-marketplacemixin)
   - [buyer.py — Acheteurs et commandes](#buyerpy--acheteurs-et-commandes-buyermixin)
   - [auction.py — Enchères et offres](#auctionpy--enchères-et-offres-auctionmixin)
   - [product.py — Catalogue producteur](#productpy--catalogue-producteur-productmixin)
   - [category.py — Catalogue public](#categorypy--catalogue-public-publicproductmixin)
   - [delivery.py — Logistique livraison](#deliverypy--logistique-livraison-deliverymixin)
   - [buyer_verification.py — Vérification admin](#buyer_verificationpy--vérification-admin-buyerverificationmixin)
   - [moderation.py — Anti-abus](#moderationpy--anti-abus-moderationmixin)
   - [utils.py — Utilitaires métier](#utilspy--utilitaires-métier-utilsmixin)
6. [Services standalone (@transactional)](#6-services-standalone-transactional)
   - [order_service.py](#order_servicepy--orderservice)
   - [product_service.py](#product_servicepy--productservice)
   - [user_context_service.py](#user_context_servicepy--usercontextservice)
7. [Modules transversaux](#7-modules-transversaux)
   - [common.py — Validation et constantes DDL](#commonpy--validation-et-constantes-ddl)
   - [search.py — Recherche floue pg_trgm](#searchpy--recherche-floue-pgtrgm)
   - [errors.py — Barrière d'erreurs](#errorspy--barrière-derreurs)
8. [Patterns clés](#8-patterns-clés)
9. [Tables référencées](#9-tables-référencées)
10. [Schéma de flux complet](#10-schéma-de-flux-complet)

---

## 1. Vue d'ensemble

L'architecture repose sur **deux approches de gestion de transaction** qui partagent le même `ContextVar` :

| Approche | Classe / décorateur | Utilisé par |
|---|---|---|
| **Dispatcher MRO** | `AgriDatabaseService.__getattribute__` | Tous les outils MCP et agents LangGraph |
| **Décorateur explicite** | `@transactional` sur `BaseService` | `OrderService`, `ProductService`, `UserContextService` |

Les deux lisent `db_session_ctx` (défini dans `base_service.py`) pour partager une session ouverte par l'appelant racine — **zéro session imbriquée, un seul pool**.

---

## 2. Arborescence des fichiers

```
services/database/
│
├── __init__.py              ← API publique (lazy-import AgriDatabaseService + get_db)
│
│── Infrastructure
├── core/
│   └── database.py          ← SEUL propriétaire du moteur async SQLAlchemy
├── engine.py                ← Façade pour imports legacy (délègue à core/)
│
│── Transaction management
├── base_service.py          ← db_session_ctx (ContextVar) + @transactional + BaseService
├── d.py                     ← AgriDatabaseService : dispatcher central, composition MRO
│
│── Mixins de domaine (montés dans AgriDatabaseService via MRO)
├── base.py                  ← BaseMixin : résolution identité phone→User/Producer/Buyer
├── auth.py                  ← AuthMixin : création/update utilisateur
├── producer.py              ← ProducerMgmtMixin : fermes, stocks, offres futures
├── marketplace.py           ← MarketplaceMixin : mouvements de stock, ventes directes
├── buyer.py                 ← BuyerMixin : commandes, enchères, précommandes
├── auction.py               ← AuctionMixin : cycle complet enchère/offre
├── product.py               ← ProductMixin : catalogue producteur (édition)
├── category.py              ← PublicProductMixin : catalogue public (lecture)
├── delivery.py              ← DeliveryMixin : logistique, OTP, distance Haversine
├── buyer_verification.py    ← BuyerVerificationMixin : admin trust badge
├── moderation.py            ← ModerationMixin : anti-abus, strikes, termes interdits
├── utils.py                 ← UtilsMixin : conversion unités agricoles, anomalie prix
│
│── Services standalone (@transactional, non-mixins)
├── order_service.py         ← OrderService : cycle commande complet
├── product_service.py       ← ProductService : lecture/publication produits
├── user_context_service.py  ← UserContextService : mémoire agent, trust score
│
│── Modules transversaux (pas de session propre)
├── common.py                ← normalize_phone, clean_text, PERFORMANCE_INDEX_DDL
├── search.py                ← fuzzy_match() + similarity_rank() via pg_trgm
└── errors.py                ← SafeDatabaseError, scrub_error_result (anti-leak)
```

---

## 3. Infrastructure de connexion

### `core/database.py` — Moteur unique

**C'est le seul fichier qui crée et possède le moteur SQLAlchemy async.** Tout le reste délègue à lui.

#### Singletons

```python
_async_engine: Optional[AsyncEngine] = None
_AsyncSessionLocal: Optional[async_sessionmaker] = None
_engine_lock = threading.Lock()   # double-checked locking thread-safe
```

#### `init_db()`

Appelé **une fois** au démarrage (via `startup` FastAPI). Actions :

1. Parse `settings.DATABASE_URL`, normalise le schéma (`postgresql+asyncpg://`).
2. Configure SSL selon `settings.DB_SSL_MODE` :
   - `disable` → pas de SSL
   - `require` → `ssl=True`
   - `verify-full` → `ssl.create_default_context()` avec `check_hostname=True`
3. Désactive le cache de statements asyncpg (`statement_cache_size=0`) — **critique** sur DigitalOcean Managed DB qui invalide les plans côté serveur sans en avertir le client, déclenchant `InvalidCachedStatementError`.
4. Configure le pool :
   - `pool_size = DB_POOL_SIZE` (défaut 5)
   - `max_overflow = DB_POOL_MAX_OVERFLOW` (défaut 10)
   - `pool_timeout = DB_POOL_TIMEOUT` (défaut 30s)

#### `get_db()` — Context manager session

```python
@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _AsyncSessionLocal() as session:
        yield session
        # Le caller commit explicitement — get_db() ne committe pas
```

Utilisé directement dans les routes FastAPI pour des opérations isolées hors AgriDatabaseService.

#### `close_db()` — Shutdown propre

Pattern snapshot-and-null : nullifie les globals **avant** d'appeler `dispose()` pour éviter qu'un autre coroutine récupère un moteur en cours de fermeture (thundering herd).

```python
async def close_db():
    engine, _async_engine = _async_engine, None   # snapshot atomique
    if engine:
        await engine.dispose()
```

#### Fonctions de diagnostic

| Fonction | Usage |
|---|---|
| `check_connection()` | 3 tentatives avec backoff exponentiel |
| `check_connection_detailed()` | Retourne `(bool, reason_str)` |
| `check_connection_aggressive()` | Valide connexion + `gen_random_uuid` + pgvector + écriture table temp |
| `ensure_extensions()` | Crée `pg_trgm`, `vector`, `pgcrypto`, `uuid-ossp` idempotent |

---

### `engine.py` — Façade de compatibilité

**Ne crée rien.** Délègue 100% à `core/database.py`. Existe uniquement pour les imports legacy `from ...engine import engine`.

```python
class DatabaseEngine:
    def engine(self)      -> AsyncEngine:       return _core_get_engine()
    def sessionmaker(self) -> async_sessionmaker: return _core_get_sessionmaker()
    def dispose(self)     -> Coroutine:          return _core_close_db()

engine = DatabaseEngine()   # singleton importable
```

> **Ne pas créer de nouveau moteur ici.** L'erreur historique consistait à double-pooler (deux `create_async_engine`), épuisant les connexions sur un cluster Managed DB.

---

## 4. Gestion des sessions et transactions

> **Révision architecturale (refactoring "Zero-Compromise") :** `@transactional`
> (dans `base_service.py`) est désormais le **SEUL** cerveau transactionnel du
> backend, pour les deux points d'entrée (dispatcher MRO et services standalone).
> Il ignore totalement le domaine métier : le commit/rollback est piloté à
> 100% par le flux d'exécution Python (exception levée ou non), jamais par un
> parsing de dict (`{"status": "error"}`). Un échec métier doit être signalé en
> **levant `BusinessRuleException`** (voir `errors.py`), jamais en retournant
> un dict d'erreur — c'est le contrat désormais imposé aux mixins d'écriture.

### `base_service.py` — ContextVar + @transactional (gestionnaire unique)

#### `db_session_ctx`

```python
db_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar("db_session_ctx", default=None)
```

Ce `ContextVar` est **le fil rouge** de toute la couche database, partagé entre
le dispatcher (`d.py`) et les services standalone. Quand une session est
ouverte, elle est écrite ici. Tous les appels imbriqués la lisent et la
**réutilisent** — un seul pool, une seule transaction.

#### `@transactional(write: bool)` — logique complète

```
Si db_session_ctx déjà rempli (appel imbriqué) :
    → réutilise la session existante, NE committe PAS (le parent gère)
Sinon (appel racine) :
    → ouvre une session depuis get_sessionmaker()
    → set ContextVar (token sauvé pour reset)
    → exécute la méthode
    → AUCUNE exception levée :
        → si write=True : commit()
        → scrub_error_result() en filet (défense en profondeur, n'influence
          jamais le commit/rollback)
    → BusinessRuleException / ValueError / KeyError (business-safe) :
        → rollback() PUIS re-raise intact (type + message traversent jusqu'à
          l'agent, qui doit savoir précisément pourquoi l'action a échoué)
    → Exception technique (IntegrityError, asyncpg, driver ORM…) :
        → rollback(), stack loggée côté serveur, SafeDatabaseError
          (message générique) levée à la place
    → CancelledError (timeout MCP) :
        → rollback() puis re-raise
    → perte de connexion DB détectée :
        → close_db() + réouverture (1 retry unique), puis reprise du flux
    → finally: reset ContextVar token
```

Le décorateur ne connaît ni le concept de "status", ni celui d'"erreur
métier" — il délègue entièrement cette classification à
`is_safe_business_exception()` (`errors.py`), qui, elle, connaît les types
(`BusinessRuleException`, `ValueError`, `KeyError`).

`_safe_rollback(session)` absorbe les erreurs de rollback sur session déjà fermée.

#### `BaseService`

Classe de base légère :

```python
class BaseService:
    _logger = logging.getLogger(__name__)

    @property
    def session(self) -> AsyncSession:
        return db_session_ctx.get()
```

---

### `d.py` — AgriDatabaseService (dispatcher central)

**C'est la classe utilisée par tous les outils MCP et les nœuds LangGraph.**

#### Composition MRO

```python
class AgriDatabaseService(
    AuthMixin, UtilsMixin,
    MarketplaceMixin, PublicProductMixin, BuyerMixin, BuyerVerificationMixin,
    ProducerMgmtMixin, ProductMixin, AuctionMixin, ModerationMixin
):
```

Python résout les méthodes dans cet ordre via MRO. Quand deux mixins ont une méthode du même nom, c'est le premier dans la liste qui gagne. Les commentaires `# MRO: wins over XMixin` documentent ces choix dans le code.

#### `_READ_ONLY_METHODS`

Frozenset d'environ 25 noms de méthodes qui **ne commitent pas** après exécution (lectures pures). Exemple :

```python
_READ_ONLY_METHODS = frozenset({
    "get_user_by_phone", "search_products", "get_auctions",
    "get_producer_orders", "get_account_status", ...
})
```

#### `__getattribute__` — proxy pur, zéro logique transactionnelle

Depuis le refactoring, `d.py` **ne contient plus aucune logique de session,
commit, rollback ou classification d'erreur** — tout est délégué à
`@transactional` (`base_service.py`). Le rôle du dispatcher se limite à :

1. Décider `is_write` (méthode absente de `_READ_ONLY_METHODS`).
2. Récupérer la méthode **brute** (non liée) depuis le MRO.
3. La décorer une seule fois avec `transactional(write=is_write)` — mise en
   cache **au niveau classe** (`_DISPATCH_CACHE`), clé `"nom:is_write"`, pour
   ne jamais re-décorer la même méthode à chaque appel.
4. Lier cette fonction décorée à l'instance via `types.MethodType` — l'idiome
   standard de Python pour binder une fonction à un objet, mis en cache **au
   niveau instance** (`self.__dict__["_bound_dispatch"]`) pour qu'un appel
   répété sur la même instance ne recrée ni closure ni lambda.

```
Appel : db_service.finalize_multi_order(items, phone)
                        ↓
    __getattribute__ intercepte
                        ↓
    Nom "_"-préfixé ou bypass ? → accès direct (pas de wrap)
                        ↓ non
    Bound method déjà en cache instance (_bound_dispatch) ?
    ├── OUI → retourne directement le bound method caché
    └── NON → MRO lookup → transactional(write=is_write)(raw_fn)
              (caché au niveau classe) → types.MethodType(wrapped, self)
              (caché au niveau instance) → retourne le bound method
```

Aucune closure ni fonction `async def proxy(...)` n'est plus créée à chaque
appel — seul le premier accès à un nom de méthode, par instance, construit et
met en cache le binding.

#### `ensure_performance_indexes()`

Exécute les DDL de `PERFORMANCE_INDEX_DDL` (de `common.py`) dans des savepoints isolés. Les erreurs par index sont silencieusement ignorées (l'index peut déjà exister). Peut être appelé avec ou sans session externe.

---

## 5. Mixins de domaine

Tous les mixins héritent de `BaseMixin` et accèdent à `self.session` (résolu via `db_session_ctx`).

---

### `base.py` — Résolution d'identité (`BaseMixin`)

**Le point d'entrée de toute opération.** Toutes les autres opérations commencent par résoudre un numéro de téléphone en entités ORM.

#### `_fetch_user_entities(phone)`

Requête polymorphique unique :

```sql
SELECT User, Producer, BuyerProfile, DeliveryAgent, Zone
FROM users u
LEFT OUTER JOIN producers p ON p.user_id = u.id
LEFT OUTER JOIN buyer_profiles b ON b.user_id = u.id
LEFT OUTER JOIN delivery_agents da ON da.user_id = u.id
LEFT OUTER JOIN zones z ON z.id = u.zone_id
WHERE u.phone = :phone
```

Retourne un tuple `(User, Producer, BuyerProfile, DeliveryAgent, Zone)` ou `None`.

#### `_serialize_user_entities(row)`

Convertit le tuple en dict standardisé :

```python
{
    "id": user.id,
    "name": user.name,
    "phone": user.phone,
    "role": user.role,
    "zone": {"id": zone.id, "name": zone.name},
    "longitude": user.longitude,
    "latitude": user.latitude,
    "profile_ids": {
        "producer": producer.id if producer else None,
        "buyer": buyer.id if buyer else None,
        "delivery": delivery.id if delivery else None,
    },
    "status": {
        "producer_status": producer.status if producer else None,
        "identity_verified": user.identity_verified,
        "whatsapp_enabled": user.whatsapp_enabled,
        "onboarding_completed": user.onboarding_completed,
    },
    "permissions": {
        "can_sell": producer is not None,
        "can_buy": True,
        "can_deliver": delivery is not None,
        "is_admin": user.role == "ADMIN",
    }
}
```

#### `get_user_by_phone(phone)` — Point critique

Retourne le dict sérialisé **ou** `{"status": "NEW_USER"}`. 

> **Piège connu** (documenté en mémoire projet) : si une migration est manquante, la requête polymorphique échoue silencieusement et retourne `None`, ce qui peut être interprété comme "NEW_USER". Vérifier `data.id` avant de considérer l'utilisateur comme chargé.

#### `get_buyer_profile(phone)` — Auto-création

Si le `BuyerProfile` est manquant pour un `User` existant, il est **créé et flush** (pas commit — le dispatcher s'en charge). Cela garantit que tout user peut acheter sans étape d'initialisation explicite.

#### `get_zone_by_name(name)`

Utilise `fuzzy_match` de `search.py` sur `Zone.name`. La recherche floue permet aux utilisateurs WhatsApp de taper "Ouagdougou" ou "waga" et d'obtenir "Ouagadougou".

---

### `auth.py` — Mutations d'identité (`AuthMixin`)

#### `identify_or_create_user(phone, name, zone_id, initial_role)`

Onboarding idempotent :

1. Vérifie l'existence via `get_user_by_phone`.
2. Si `NEW_USER` : crée `User` + éventuellement `Producer` satellite (si `role=PRODUCER`).
3. Gère `IntegrityError` par re-fetch (condition de course sur inscription simultanée).

#### `verify_user_identity(user_id, cnib_number)`

Stocke le numéro CNIB (carte nationale d'identité burkinabè) et lève `identity_verified=True`.

---

### `producer.py` — Producteurs, fermes, stocks (`ProducerMgmtMixin`)

Le mixin le plus volumineux. Gère le cycle complet du côté producteur.

#### `get_or_create_farm(producer_id/phone, farm_name, zone_id)`

Retourne la première ferme existante ou en crée une par défaut. Accepte `producer_id` (UUID) **ou** `phone` (résolution automatique). Utilisé massivement pour éviter le bug "farm_id manquant" (documenté en mémoire projet).

#### `get_stocks(phone/producer_id)` — Requête complexe

Retourne une structure multi-niveau :

```python
{
    "farms": {
        "farm_uuid": {
            "stocks": [...],          # Stock rows avec mouvements récents
            "upcoming_cycles": [...]   # MarketOffer futures
        }
    },
    "catalog": [...],                 # Tous les produits du producteur
    "upcoming_cycles": [...]           # Agrégat global
}
```

Inclut un fallback de résolution de ferme via JOIN `phone` si la ferme n'est pas trouvée directement.

#### `declare_future_production(payload, phone/producer_id)`

Crée un `MarketOffer` pour une récolte future. Valide la propriété de la ferme. `_normalize_offer_payload` impose `estimated_available_at` pour les offres futures. C'est l'entrée du backbone "future production → preorder" (voir mémoire projet `future-production-preorder-loop.md`).

#### `add_stock_movement(phone, stock_id, mtype, quantity, reason)`

Chargement avec `with_for_update` :

```python
SELECT Stock
JOIN Farm JOIN Producer JOIN User
WHERE Stock.id = :stock_id
FOR UPDATE
```

Vérifie la propriété (phone du User doit matcher). Applique IN ou OUT, crée `StockMovement`. Protège contre la manipulation de stock d'un autre producteur.

#### `_normalize_offer_payload(payload, is_future)`

Validation et normalisation d'un payload `MarketOffer`. Champs validés : `product_label`, `quantity_available`, `price_per_unit`, `unit`, `species` (facultatif), `estimated_available_at` (obligatoire si `is_future=True`).

---

### `marketplace.py` — Stock direct et ventes (`MarketplaceMixin`)

#### `add_stock(farm_id, item_name, quantity, unit, ...)`

Upsert sur `Stock` :

```python
SELECT Stock
WHERE func.lower(Stock.name) == item_name.lower()
AND Stock.farm_id == farm_id
FOR UPDATE
```

Si trouvé : incrémente `quantity`. Sinon : crée nouveau `Stock`. Crée toujours un `StockMovement(type=IN)`.

#### `remove_stock(farm_id, item_name, quantity, reason)`

Vérifie suffisance avant décrémentation. Crée `StockMovement(type=OUT)`.

#### `record_sale(phone, product_name, quantity, total_price, unit, client_name)`

Journalise une vente directe (hors plateforme) :
- Auto-crée un `Product(is_available=False)` si non trouvé (fantôme de vente pour historique).
- Crée `Order(status=COMPLETED, type=DIRECT_SALE)` + `OrderItem`.

---

### `buyer.py` — Acheteurs et commandes (`BuyerMixin`)

#### `search_products(product, phone, limit)` — Recherche unifiée

Retourne `DIRECT` (catalogue stock disponible) + `FUTURE` (offres précommande) dans la même requête. Score géographique :

```python
case(
    (User.zone_id == buyer_zone_id, 1),          # même zone → priorité 1
    (Zone.parent_id == buyer_zone.parent_id, 2),  # zone parente → priorité 2
    else_=3                                        # national → priorité 3
)
```

Tri final : `(priority, source_type, price)`.

#### `finalize_multi_order(items, phone)` — Commande atomique

```
Valider que chaque product_id est un UUID bien formé (AVANT tri, sinon
    BusinessRuleException — un product_id invalide casserait l'ordre
    lexicographique et donc la garantie anti-deadlock ci-dessous)
Trier items par product_id (ordre déterministe)
Pour chaque item (dans l'ordre trié) :
    SELECT Product WHERE id = item.product_id FOR UPDATE
    Vérifier quantity_for_sale >= item.quantity
    Si insuffisant → raise BusinessRuleException (rollback géré par @transactional)
    Décrémenter quantity_for_sale
    Créer OrderItem

Créer Order(status=PENDING, type=MARKET)
```

Le `with_for_update()` par produit évite les surventes concurrentes. Le **tri
par `product_id`** avant la boucle de verrouillage évite un deadlock
classique : deux transactions achetant les mêmes produits dans un ordre
différent formeraient sinon un cycle d'attente sur les verrous `FOR UPDATE`.
Le tri garantit que toutes les transactions acquièrent les verrous dans le
même ordre.

#### `cancel_pending_order(order_id, phone, reason)`

1. Vérifie `status == PENDING`.
2. Pour chaque item : `with_for_update` sur Product, restock `quantity_for_sale`.
3. `status = CANCELLED`, log reason.
4. Appelle `_enforce_cancellation_limit`.

#### `_enforce_cancellation_limit(session, buyer_profile_id, user_obj)`

Compte les annulations buyer-initiated. Si `> MAX_CANCELLATIONS (3)` : `account_status = BLOCKED`. Protection anti-abus documentée dans `abuse-moderation-system.md`.

#### `reserve_future_offer(buyer_phone, market_offer_id, quantity, desired_price)`

```
SELECT MarketOffer WHERE id = :offer_id FOR UPDATE
Vérifier preorder_enabled == True
Vérifier (available_quantity - reserved_quantity) >= quantity
Créer Order(type=PREORDER, status=PENDING)
Incrémenter reserved_quantity
→ _notify_producer_reservation (outbox, même transaction)
```

#### `validate_stock_availability_atomic(product_id, quantity, unit, buyer_phone)`

Si stock insuffisant, requête automatique de 3 alternatives via `fuzzy_match` sur `Product.name` dans la même subcategory. Retourne `{"available": false, "alternatives": [...]}`.

---

### `auction.py` — Enchères et offres (`AuctionMixin`)

#### Cycle de vie complet

```
Acheteur crée Auction (status=OPEN)
    ↓
Producteurs placent des Bid (status=PENDING)
    ↓
Acheteur choisit un Bid → select_winning_bid()
    ├── Bid gagnant → status=WINNING
    ├── Autres bids → status=LOST
    ├── Auction → status=CLOSED
    └── Order(status=CONFIRMED) créé
         └── Outbox: notification AUCTION_WON_PRODUCER
```

#### `place_bid(auction_id, phone, offered_price, message)`

Pattern UPSERT pour éviter `IntegrityError` sur la contrainte `bids_auction_producer_unique` :

```python
# Si le producteur a déjà un bid PENDING sur cette enchère :
UPDATE Bid SET offered_price = :new_price WHERE auction_id = :id AND producer_id = :prod
# Sinon :
INSERT INTO Bid ...
```

#### `get_producer_auctions(phone, scope, ...)` — Scope MATCHABLE

Quand `scope=MATCHABLE`, filtre les enchères dont la `sub_category_id` est dans le catalogue du producteur. Si le producteur n'a aucun produit en catalogue, fallback automatique vers `scope=ALL`.

#### `_derive_bid_status(bid_status, is_winner, auction_status)` — Statut effectif

Un bid peut avoir `status=PENDING` dans la DB mais être effectivement perdu si l'enchère est `CLOSED`. Cette méthode calcule le statut **réel** visible à l'utilisateur.

#### `check_and_expire_auctions()`

Bulk UPDATE :

```sql
UPDATE auctions SET status='EXPIRED'
WHERE deadline <= NOW() AND status='OPEN'
```

Appelé par le worker Celery Beat (voir `orchestration-proactive-workers.md`).

---

### `product.py` — Catalogue producteur (`ProductMixin`)

#### `delete_product(phone, product_id)`

Logique soft vs hard delete :

```
Si Product a des Order(status in PENDING/CONFIRMED/SHIPPED) :
    → HARD DELETE interdit
Si Product a des orders historiques (COMPLETED, etc.) :
    → SOFT DELETE : quantity_for_sale=0, is_available=False
Si aucun historique :
    → HARD DELETE
```

Protège l'intégrité référentielle des commandes passées.

---

### `category.py` — Catalogue public (`PublicProductMixin`)

#### `get_public_categories()`

```sql
SELECT Category WHERE EXISTS (
    SELECT 1 FROM products p
    WHERE p.sub_category_id IN (
        SELECT id FROM sub_categories WHERE category_id = Category.id
    ) AND p.quantity_for_sale > 0
)
```

Ne retourne que les catégories avec stock réel — évite les catégories vides dans le menu WhatsApp.

#### `search_by_proximity(lat, lng, radius_km)`

Formule planaire (approximation suffisante pour les distances agricoles) :

```python
distance_km = sqrt(
    pow((User.latitude - lat) * 111.12, 2) +
    pow((User.longitude - lng) * 111.12 * cos(radians(lat)), 2)
)
WHERE distance_km <= radius_km
```

#### `bind_user_to_zone(user_id, lat, lng)`

Trouve la zone la plus proche par distance planaire, met à jour `User.zone_id`, `latitude`, `longitude`.

---

### `delivery.py` — Logistique livraison (`DeliveryMixin`)

#### `claim_delivery(delivery_id, user_id)`

Atomic via `UPDATE ... RETURNING` :

```sql
UPDATE deliveries
SET agent_id = :user_id, status = 'ASSIGNED'
WHERE id = :delivery_id AND agent_id IS NULL AND status = 'PENDING'
RETURNING order_id
```

Si `rowcount == 0` : un autre agent a pris la livraison en parallèle — retourne erreur sans lock.

#### `confirm_delivery_with_otp(delivery_id, otp_code, user_id)`

Valide le code OTP généré à la création (`_generate_delivery_otp()` → 6 chiffres). Set `Delivery.status=DELIVERED`, `Order.status=DELIVERED`, libère l'agent (`DeliveryAgent.status=AVAILABLE`).

#### `get_delivery_status_tracking(order_id)`

Retourne pourcentage de progression :

| Statut | % |
|---|---|
| PENDING | 25 |
| ASSIGNED | 50 |
| IN_TRANSIT | 75 |
| DELIVERED | 100 |

---

### `buyer_verification.py` — Vérification admin (`BuyerVerificationMixin`)

#### `verify_buyer_profile(buyer_profile_id, admin_user_id, verification_type)`

Types de badge : `VERIFIED_ID` (CNIB), `VERIFIED_COMMERCE` (registre commerce), `VERIFIED_INSTITUTION`.

Pour `VERIFIED_ID` :
- `User.identity_verified = True`
- `TrustScore.compliance_index = 1.0`
- `TrustScore.global_score += 0.1`

Écrit un `AuditLog` pour traçabilité admin.

#### `revoke_buyer_trust_badge(buyer_profile_id, admin_user_id, reason)`

- Efface le badge
- `TrustScore.global_score -= 0.3`
- `AuditLog` avec reason

---

### `moderation.py` — Anti-abus (`ModerationMixin`)

#### Constantes

```python
MAX_CANCELLATIONS = 3          # annulations avant blocage compte
MAX_MODERATION_STRIKES = 3     # strikes avant bannissement
DEFAULT_PROHIBITED_TERMS = {   # ~50 termes en ASCII folded (drogues, armes, etc.)
    "cocaine", "drogue", "arme", "kalachnikov", ...
}
```

#### `get_prohibited_terms()`

Fusionne les termes de la table `prohibited_terms` (DB) avec `DEFAULT_PROHIBITED_TERMS` (hardcoded). Cache process-wide TTL 300s dans `_TERMS_CACHE`.

#### `record_moderation_strike(phone, matched_term, excerpt, kind)`

1. Crée `ModerationEvent`.
2. Compte tous les strikes pour ce phone+kind.
3. Si `> MAX_MODERATION_STRIKES` : `User.account_status = BANNED`, `event.action_taken = BANNED`.

#### `record_demand_signal(phone, raw_query, normalized_term, zone_id)`

Upsert sur `DemandSignal.normalized_term` (incrémente `occurrences`). Capture la demande non satisfaite pour analyse marché.

---

### `utils.py` — Utilitaires métier (`UtilsMixin`)

#### `normalize_unit(quantity, unit)`

Conversion vers kg-équivalent pour les unités agricoles locales :

| Unité | Équivalent kg |
|---|---|
| sac | × 100 |
| tine | × 18 |
| plat | × 2.5 |
| kg | × 1 |

#### `check_price_anomaly(product_name, proposed_price, zone_id)`

Compare au prix de référence de la zone :
- `> 3x` → anomalie HIGH
- `< 0.3x` → anomalie LOW

---

## 6. Services standalone (@transactional)

Ces classes héritent de `BaseService` et utilisent `@transactional` explicitement. Elles ne sont **pas** montées dans `AgriDatabaseService` et sont appelées directement depuis des workers ou des routes FastAPI.

---

### `order_service.py` — `OrderService`

Cycle complet de commande avec historique de statuts.

#### `create_order(session, order: OrderModel)`

Transaction d'écriture :
1. Crée `Order`.
2. Pour chaque item : crée `OrderItem`.
3. Crée `OrderStatusHistory(status=PENDING)`.

#### `_transition(session, order, status_type, field, to_status, actor_id, note)`

Helper interne qui :
- Set `order.<field> = to_status` (ex: `payment_status`)
- Crée `OrderStatusHistory` avec actor et note

#### `advance_order_status` vs `advance_delivery_status`

Deux méthodes distinctes pour distinguer le statut de traitement (`CONFIRMED`, `SHIPPED`) du statut de livraison (`IN_TRANSIT`, `DELIVERED`). Chacune appelle `_transition` sur le champ correspondant.

#### `due_reminders(session, limit)`

```sql
SELECT OrderReminder
WHERE status = 'SCHEDULED' AND scheduled_at <= NOW()
LIMIT :limit
```

Utilisé par le worker Celery pour envoyer les rappels de commande.

---

### `product_service.py` — `ProductService`

#### `reserve_offer_quantity(session, offer_id, quantity)`

UPDATE conditionnel — l'opération la plus critique :

```sql
UPDATE market_offers
SET reserved_quantity = reserved_quantity + :quantity
WHERE id = :offer_id
AND (available_quantity - reserved_quantity) >= :quantity
```

Retourne `True` si `rowcount == 1` (succès atomique), `False` sinon. Évite les surréservations sans SELECT préalable.

---

### `user_context_service.py` — `UserContextService`

#### `resolve_by_phone(session, phone)`

Remplaçant propre de `BaseMixin._fetch_user_entities`. Retourne un `UserContextModel` (Pydantic DTO) au lieu d'un tuple ORM.

#### `set_memory(session, user_id, key, value, source, market_offer_id)`

Upsert PostgreSQL natif :

```python
pg_insert(AgentContextMemory).values(...).on_conflict_do_update(
    index_elements=["user_id", "context_key"],
    set_={"context_value": value, "updated_at": now}
)
```

Stocke la mémoire contextuelle des agents LangGraph (ex : "l'utilisateur cherche du maïs en sac de 100kg").

---

## 7. Modules transversaux

### `common.py` — Validation et constantes DDL

#### Fonctions de validation

| Fonction | Rôle |
|---|---|
| `normalize_phone(phone)` | Supprime espaces, tirets, parenthèses ; garde `+` |
| `clean_text(value, field, required, max_length)` | Trim, contrôle chars, troncature |
| `escape_like(term)` | Échappe `%`, `_`, `\` pour ILIKE (anti-DoS wildcard) |
| `normalize_uuid(value)` | Minuscules string |
| `positive_float(value, field, allow_zero)` | Valide numérique et signe |
| `clamp_limit(value, default, maximum)` | Borne pagination |

> `clean_text` ne protège **pas** contre l'injection SQL — c'est l'ORM qui s'en charge via paramètres bindés. Cette fonction gère uniquement la qualité des données.

#### `PERFORMANCE_INDEX_DDL`

Tuple de DDL SQL pour indexes GIN trigram :

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_products_name_trgm
    ON marketplace.products USING gin (name gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_market_offers_label_trgm
    ON marketplace.market_offers USING gin (product_label gin_trgm_ops);

-- + sub_categories(name), zones(name)
-- + CREATE EXTENSION IF NOT EXISTS pg_trgm
```

Exécutés via `AgriDatabaseService.ensure_performance_indexes()` au démarrage.

---

### `search.py` — Recherche floue pg_trgm

#### `fuzzy_match(column, term, threshold=0.22)`

Combine trois prédicats via OR :

```python
or_(
    func.similarity(column, term) >= threshold,    # similarité trigram
    column.ilike(f"%{escape_like(term)}%"),        # contient
    column.ilike(f"{escape_like(term)}%"),         # commence par
)
```

**Seuil 0.22** (vs défaut PostgreSQL 0.3) : plus tolérant pour les termes agricoles souvent abrégés ou mal orthographiés par les utilisateurs WhatsApp.

Si `term` est vide : retourne un prédicat `false` (jamais de match) pour éviter un full-scan.

#### `similarity_rank(column, term)`

```python
func.similarity(column, term).desc()
```

Utilisé en `ORDER BY` pour classer les résultats par pertinence après `fuzzy_match`.

---

### `errors.py` — Barrière d'erreurs

**Objectif de sécurité** : empêcher les détails techniques de la DB d'atteindre le modèle LLM, qui pourrait les relayer mot-pour-mot aux utilisateurs WhatsApp.

#### `_LEAK_MARKERS`

```python
_LEAK_MARKERS = (
    "constraint", "violates", "psycopg", "traceback",
    "sqlalchemy", "asyncpg", "detail:", "column", "relation"
)
```

#### `BusinessRuleException` — échec métier explicite

```python
class BusinessRuleException(Exception):
    def __init__(self, message: str, *, reason: str | None = None, **extra) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.extra = extra
```

Remplace le pattern `return {"status": "error", "message": ...}` dans les
mixins d'écriture (`buyer.py`). Les mixins **lèvent** cette exception au lieu
de retourner un dict — `@transactional` n'a alors plus besoin de parser aucun
contenu applicatif pour décider d'un rollback : une exception levée suffit.
`reason` (code machine) et `**extra` (payload structuré, ex: `fallback=[...]`,
`details=[...]`) restent disponibles pour l'appelant qui souhaite les
exploiter, sans que la couche transactionnelle ait à les connaître.

#### `SafeDatabaseError`

Exception avec `safe_message` garanti agent-safe. Message générique : `"Une erreur technique s'est produite. Veuillez réessayer."`.

#### `is_safe_business_exception(exc)` / `sanitize_error_message(exc)`

- `BusinessRuleException`, `ValueError`, `KeyError` (exceptions business) → message passthrough **si** ne contient pas de leak markers ; type ET message traversent intacts jusqu'à l'agent.
- Exception technique (ORM, driver) → message générique, stack loggée côté serveur.

#### `scrub_error_result(result)`

Filet de défense en profondeur, **orthogonal** au contrôle commit/rollback : passe sur le dict retourné par un mixin (succès ou legacy) et remplace les `message` qui contiendraient malgré tout des leak markers.

---

## 8. Patterns clés

### Pattern 1 — Session unique par requête (ContextVar), pilotée par les exceptions

```
Appel MCP → AgriDatabaseService.__getattribute__ (proxy pur)
    → transactional(write=is_write)(raw_fn) bindé à l'instance
        → ouvre session → ContextVar = session
            → méthode A appelle méthode B (interne)
                → B lit ContextVar → réutilise session
            → fin B (pas de commit, pas d'exception : le parent gère)
        → fin A → AUCUNE exception ? commit(). Exception ? rollback().
        → reset ContextVar
```

**Pas de session imbriquée, pas de double pool.** Le commit/rollback ne dépend
que du flux d'exécution — jamais du contenu d'un dict retourné.

### Pattern 2 — Row-level locking (`with_for_update`)

Utilisé sur toutes les opérations modifiant un solde :
- Décrémentation stock (`finalize_multi_order`, `remove_stock`)
- Réservation offre future (`reserve_future_offer`)
- Restock annulation (`cancel_pending_order`)
- Claim livraison (`claim_delivery`)
- Mise à jour bid (`place_bid` upsert)

### Pattern 3 — Outbox dans la même transaction

Les notifications WhatsApp sont enqueued via `outbox_repo.enqueue(...)` **dans la même transaction** que l'opération business. Garantie : si la transaction rollback, la notification ne part pas. Si elle commit, la notification sera envoyée par le worker Outbox.

```python
# Dans reserve_future_offer :
session.add(Order(...))
MarketOffer.reserved_quantity += quantity
await self._notify_producer_reservation(offer, qty, total)  # même session
# → commit global fait les deux atomiquement
```

### Pattern 4 — Barrière d'erreurs, 100% pilotée par exceptions

```
BusinessRuleException / ValueError / KeyError (métier)
    ↓
@transactional : rollback() → re-raise INTACT (type + message)
    ↓
Agent LLM reçoit l'exception métier telle quelle (peut guider l'utilisateur)

Exception ORM/driver (technique)
    ↓
@transactional : rollback() → SafeDatabaseError (message générique)
    ↓
Agent LLM reçoit un message safe, la vraie cause est loggée côté serveur
```

Aucune de ces branches n'inspecte un dict de retour — la classification
métier/technique se fait exclusivement sur le **type** de l'exception levée.

### Pattern 5 — Soft delete conditionnel

```python
if has_active_orders:
    raise ValueError("Impossible de supprimer")
elif has_any_past_orders:
    product.quantity_for_sale = 0
    product.is_available = False   # soft delete
else:
    session.delete(product)        # hard delete
```

### Pattern 6 — Retry connexion sur perte

Dans `@transactional` (`base_service.py`), désormais unique point d'implémentation :

```python
try:
    result = await fn(self, session, *args, **kwargs)
except Exception as exc:
    await _safe_rollback(session)
    if _is_connection_lost(exc) and attempt == 0:
        await close_db()               # dispose + null engine
        session_factory = get_sessionmaker()   # réinit
        continue                        # 1 retry, nouvelle session
    ...
```

Un seul retry — si le second échoue, l'exception remonte.

---

## 9. Tables référencées

| Table | Schéma | Mixins principaux |
|---|---|---|
| `users` | `auth` | `BaseMixin`, `AuthMixin`, `BuyerMixin` |
| `producers` | `marketplace` | `ProducerMgmtMixin`, `AuctionMixin` |
| `buyer_profiles` | `marketplace` | `BuyerMixin`, `AuctionMixin` |
| `delivery_agents` | `marketplace` | `BaseMixin`, `DeliveryMixin` |
| `zones` | `governance` | `BaseMixin`, `PublicProductMixin` |
| `farms` | `marketplace` | `ProducerMgmtMixin`, `MarketplaceMixin` |
| `stocks` | `marketplace` | `MarketplaceMixin`, `ProducerMgmtMixin` |
| `stock_movements` | `marketplace` | `MarketplaceMixin` |
| `products` | `marketplace` | `ProductMixin`, `BuyerMixin`, `PublicProductMixin` |
| `market_offers` | `marketplace` | `ProducerMgmtMixin`, `BuyerMixin`, `ProductService` |
| `orders` | `marketplace` | `BuyerMixin`, `ProducerMgmtMixin`, `AuctionMixin`, `OrderService` |
| `order_items` | `marketplace` | `BuyerMixin`, `OrderService` |
| `order_status_history` | `marketplace` | `OrderService` |
| `payments` | `marketplace` | `OrderService` |
| `order_reminders` | `marketplace` | `OrderService` |
| `deliveries` | `marketplace` | `DeliveryMixin` |
| `auctions` | `marketplace` | `AuctionMixin` |
| `bids` | `marketplace` | `AuctionMixin` |
| `categories` | `governance` | `PublicProductMixin`, `AuctionMixin` |
| `sub_categories` | `governance` | `AuctionMixin`, `ProducerMgmtMixin` |
| `clients` | `marketplace` | `ProducerMgmtMixin` |
| `expenses` | `marketplace` | `MarketplaceMixin` |
| `trust_scores` | `auth` | `BuyerMixin`, `BuyerVerificationMixin` |
| `audit_logs` | `auth` | `BuyerVerificationMixin` |
| `agent_context_memory` | `auth` | `UserContextService` |
| `prohibited_terms` | `governance` | `ModerationMixin` |
| `moderation_events` | `governance` | `ModerationMixin` |
| `demand_signals` | `governance` | `ModerationMixin` |
| `buyer_types` | `marketplace` | `BuyerMixin` |

---

## 10. Schéma de flux complet

```
┌─────────────────────────────────────────────────────────────────────┐
│  Outil MCP / Nœud LangGraph                                         │
│  ex: tool_finalize_order(items, phone)                              │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  AgriDatabaseService.__getattribute__          [d.py] — PROXY PUR    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ Bound method déjà en cache instance (_bound_dispatch) ?      │    │
│  │ OUI → retourne le bound method caché (aucune allocation)     │    │
│  │ NON → MRO lookup → transactional(write=is_write)(raw_fn)     │    │
│  │        (cache classe _DISPATCH_CACHE) → types.MethodType(…)  │    │
│  │        (cache instance) → retourne le bound method           │    │
│  └─────────────────────────────────────────────────────────────┘    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  @transactional(write=True)                    [base_service.py]    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ db_session_ctx vide ?                                       │    │
│  │ OUI → ouvre session, set ContextVar                        │    │
│  │ NON → réutilise session existante (appel imbriqué)         │    │
│  └─────────────────────────────────────────────────────────────┘    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  BuyerMixin.finalize_multi_order()             [buyer.py]           │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ normalize_phone(phone)                [common.py]           │    │
│  │ get_buyer_profile(phone)              [base.py]             │    │
│  │   └─ _fetch_user_entities(phone)    ← requête polymorphique │    │
│  │                                                             │    │
│  │ Valide product_id (UUID) AVANT tri (sinon BusinessRuleExc.) │    │
│  │ Trie les items par product_id (anti-deadlock)               │    │
│  │ Pour chaque item (ordre trié):                              │    │
│  │   SELECT Product FOR UPDATE                                 │    │
│  │   Stock insuffisant → raise BusinessRuleException           │    │
│  │   quantity_for_sale -= item.quantity                        │    │
│  │                                                             │    │
│  │ INSERT Order + OrderItems                                   │    │
│  └─────────────────────────────────────────────────────────────┘    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ résultat (dict succès OU exception levée)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  @transactional : post-traitement piloté par exceptions [base_service.py] │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ Aucune exception ? → COMMIT (si write=True)                 │    │
│  │ BusinessRuleException/ValueError/KeyError ? → ROLLBACK,      │    │
│  │   puis re-raise INTACT (type + message vers l'agent)         │    │
│  │ Exception technique ? → ROLLBACK, SafeDatabaseError générique│    │
│  │   [errors.py], stack loggée côté serveur                     │    │
│  │ scrub_error_result(res) sur tout dict retourné (défense en   │    │
│  │   profondeur, n'influence jamais commit/rollback) [errors.py]│    │
│  │ reset ContextVar                                             │    │
│  └─────────────────────────────────────────────────────────────┘    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
              Résultat (ou exception) safe pour l'agent LLM
```
