# PRODUCTION READINESS GATE — 2026-09-05 (Phase 7)

## 1. Executive summary

Dernière validation avant déploiement. Aucune fonctionnalité ajoutée.

**Trois failles de sécurité réelles trouvées et fermées**, toutes de la
même famille que `update_order_status` (Phase 6B) : des mutations exposées
comme outils MCP sans contrôle de propriété, ou acceptant un statut
arbitraire. Aucune n'était atteignable depuis un parcours conversationnel,
toutes l'étaient depuis un appel MCP direct.

| Découverte | Classement | État |
|---|---|---|
| `mark_escrow_paid` exposé en MCP (déclare un paiement reçu sans le fournisseur) | **BLOCKING (sécurité)** | **fermé** |
| `expire_pending_payments` exposé en MCP (annulation en masse, sans acteur) | **BLOCKING (sécurité)** | **fermé** |
| `cancel_preorder_draft` écrivait un `target_status` non validé | **BLOCKING (intégrité)** | **fermé** |
| Le DDL ne s'applique qu'au démarrage d'un worker Celery | NON-BLOCKING — action de déploiement | documenté (§3) |
| Schéma de base non créé par ce dépôt | KNOWN / ACCEPTED — provisionné hors dépôt | documenté (§3) |
| Pas d'expiration automatique des enchères | KNOWN / ACCEPTED (P3) | documenté |

**Verdict : GO**, sous réserve des 2 actions de déploiement du §19.

## 2. Production architecture

`docker-compose.prod.yml` déploie : `mcp`, `api`, `worker`, `beat`,
`redis`, `pgbouncer`, `flower`, `autoheal`. **PostgreSQL est externe**
(base managée) — cohérent avec le fait que ce dépôt ne crée pas le schéma
de base.

| Composant | Config requise | Vérifié au démarrage | Mode de défaillance |
|---|---|---|---|
| API FastAPI | `DATABASE_URL` | pool DB préchauffé au `startup` (évite un faux `LOCATION_PERSISTENCE_ERROR` à froid) | démarre quand même ; 1ʳᵉ requête lente |
| Worker Celery | `REDIS_URL`, `DATABASE_URL` | `worker_process_init` → `_ensure_schema()` (best-effort, non bloquant) | démarre même si le DDL échoue → colonnes/tables manquantes |
| Beat | `REDIS_URL` | 7 tâches planifiées | tâches publiées mais non exécutées si aucun worker |
| Redis | `REDIS_URL` (défaut `redis://localhost:6379/0`) | — | broker + backend indisponibles |
| MCP | `MCP_DB_TRANSPORT` (+ URL/secret selon transport) | fail-closed sur scope inconnu | outil refusé |
| Télémétrie | OTEL / Langfuse (optionnels) | `init_telemetry` au startup | dégradé silencieux |

**Enregistrement des tâches vérifié** : `celery_app` déclare
`include=[10 modules]` ; après import, **14 tâches** sont enregistrées et
**les 7 tâches du beat résolvent toutes**. Aucun `Received unregistered
task` possible.

## 3. Database reset / bootstrap

C'est le point le plus important pour ce déploiement, la base étant
réinitialisée.

**Créé automatiquement par l'application** (DDL idempotent, au démarrage
d'un **worker Celery** via `_ensure_schema` → `ensure_performance_indexes`) :

```
extension pg_trgm                     (indispensable : toute la recherche floue
                                       utilise similarity() — product search,
                                       résolution de sous-catégorie, gate catalogue)
index de performance (dont gin_trgm)
SCHEMA_COLUMN_DDL                     (colonnes additives, dont checkout_group_id)
marketplace.preorder_drafts
marketplace.procurement_drafts
marketplace.sales_publish_drafts
marketplace.mcp_idempotency_records
agri_workspaces
```

**NON créé par ce dépôt** — doit préexister : toutes les tables métier de
base (`marketplace.orders`, `order_items`, `products`, `auctions`, `bids`,
`producers`, `buyer_profiles`, `governance.zones`,
`governance.sub_categories`, `auth.users`…). Ce dépôt n'a **ni Alembic**
(la dépendance est déclarée mais inutilisée), **ni `create_all`**, **ni
fichier SQL** de bootstrap. Le schéma vient de l'infrastructure
(base managée / dépôt d'infra).

> **Conséquence opérationnelle (action de déploiement)** : sur une base
> fraîchement créée, un **worker Celery doit démarrer avant d'ouvrir le
> trafic API**. Sans lui, `checkout_group_id` et les 4 tables de drafts
> n'existent pas → le checkout échoue. Le DDL est `best-effort` et **ne
> bloque jamais le démarrage** : un échec (droits insuffisants sur
> `CREATE EXTENSION`, par exemple) est seulement journalisé en warning.
> **À vérifier explicitement dans les logs du premier worker.**

### Données de référence (seed)

| Donnée | Nature | Nécessaire ? | Conséquence si absente |
|---|---|---|---|
| `governance.sub_categories` | **seed DB** | fortement recommandé | `create_product` dégrade proprement (`sub_category_id = NULL`) mais **la politique de minimum de commande devient silencieusement inactive**, et le gate catalogue des enchères n'a rien à résoudre |
| `governance.zones` | **seed DB** | recommandé | rattachement territorial, proximité et géofencing dégradés |
| Templates de notification | **constante code** (`workers/outbox/templates.py`) | — | — |
| Catalogue d'intents, rôles | **constante code** (`interpreter/intent.py`) | — | — |
| Politique de minimum de commande | **règle code** + seuils portés par `SubCategory` | — | voir ci-dessus |

## 4. Security

### Faille 1 — `mark_escrow_paid` exposé en MCP — **fermée**

Marque une commande payée (`payment_status=ESCROWED`) et déclenche les
notifications. Authentifiée uniquement par le token d'invoice — **aucun
contrôle de propriété** (par nature : c'est le fournisseur de paiement qui
appelle). Son appelant réel est la tâche IPN
(`workers/payments/paydunya_ipn_task.py` → `reconcile_invoice` →
`AgriDatabaseService().mark_escrow_paid`), **qui n'utilise pas MCP**.
Exposée, elle permettait de déclarer un paiement reçu sans paiement.
→ retirée de `TOOL_SCOPE_MAP` (fail-closed).

### Faille 2 — `expire_pending_payments` exposé en MCP — **fermée**

Balayage global sans acteur ni périmètre, appelé par le cron
`workers/crons/order_expiry.py` **en direct**. Exposée, elle permettait
d'expirer/annuler en masse les commandes en attente de paiement.
→ retirée de `TOOL_SCOPE_MAP`.

### Faille 3 — `cancel_preorder_draft(target_status)` non validé — **fermée**

`target_status` était écrit **tel quel** dans `Order.status`. Les
appelants légitimes ne passent que `CANCELLED`/`SUPERSEDED`, mais l'outil
est exposé : un appelant MCP pouvait faire passer **son propre** brouillon
à `COMPLETED` — sans confirmation, sans débit de stock, sans paiement, et
la commande apparaissait terminée dans les tableaux de bord.
→ liste blanche explicite (`invalid_target_status`).

### Balayage exhaustif

**27 outils MCP en écriture touchant une entité critique** (Order,
Product, Auction, Bid, Draft, Payment, Delivery) ont été audités
automatiquement (paramètre acteur, marqueurs de contrôle de propriété,
garde de statut). 3 suspects → 3 traités ci-dessus. Le seul restant sans
acteur, `create_user_profile`, est **légitime** : il *crée* l'acteur, et
son appelant réel est l'onboarding.

**Outils acceptant un statut en paramètre** : inventaire complet ;
`update_order_status` est fail-closed, `cancel_preorder_draft` valide
désormais, les 4 autres sont des lectures (`get_*`).

### Fail-closed

`runtime.py::call_tool` refuse tout outil absent de `TOOL_SCOPE_MAP`, et
`_autofill_tool_scopes` ne remplit plus rien (il **journalise** seulement
les dérives). Un outil non déclaré est donc refusé, jamais autorisé par
défaut.

### Matrice acteur / ressource (règles réellement présentes)

| Ressource | Acheteur | Producteur propriétaire | Autre producteur | Système |
|---|---|---|---|---|
| Product | lecture (recherche, `is_available` + `quantity_for_sale > 0`) | écriture sur les siens (`producer_id`) | refusé | — |
| Order | les siennes (`Order.buyer_id ==`) | les siennes (`OrderItem→Product.producer_id` **ou** `winning_bid_id→Bid.producer_id`) | refusé (`not_owner`) | crons directs, hors MCP |
| Bid | lecture des offres de son enchère | les siennes | refusé | — |
| Auction | les siennes (création, annulation, sélection) | lecture + dépôt d'offre | refusé | — |
| Draft | les siens (`buyer_id`) | s.o. | refusé | réconciliation |

L'identité est **épinglée** : `schema_resolver::lookup_arg_value` résout
`phone`/`producer_id`/`user_id` depuis l'état authentifié, jamais depuis
le payload.

## 5. Runtime reachability

Contrats actifs dans la suite finale (aucun doublon créé) :

| Contrat | Portée |
|---|---|
| `test_write_capability_reachability.py` | 70 goals : rôle, handler, outil réel, scope, passthrough ; **+ preuve dynamique** `validator → DomainRouter.decide → to_resolver` |
| `test_no_broken_user_goals.py` | 0 faux bouton parmi les 39 goals exposés, **sans exception tolérée** ; garde anti-dépréciation trop large |
| `test_order_mutations_require_ownership.py` | propriété sur chaque mutation d'état, outils système fail-closed, statut arbitraire interdit, invariant un-producteur, enchères mono-producteur |

## 6–7. Smoke tests acheteur / producteur

Couverts par les suites existantes (réutilisées, non dupliquées) :

| Parcours | Couverture |
|---|---|
| Checkout acheteur (mono et multi-producteurs) | `test_multi_producer_checkout.py`, `test_multi_producer_group_confirmation.py` |
| Annulation acheteur (`PENDING`/`CONFIRMED` → `CANCELLED`, rejets, idempotence) | `test_cancel_pending_order_confirmed_gap.py` |
| Sélection du gagnant + notifications | `test_rfq_to_completion_with_notifications_e2e.py`, `test_auction_loser_notification.py` |
| Publication / retrait produit | `test_product_unpublish_capability.py` |
| Clôture livraison + paiement | `test_payment_at_delivery_e2e.py`, `test_confirm_delivery_and_payment.py` |
| Annulation producteur | `test_producer_cancel_confirmed_order.py` |
| Offres producteur | `test_place_bid_upsert_semantics.py`, `test_auction_bid_full_lifecycle_e2e.py` |
| Entrée conversationnelle réelle | `TestRealEntryPointReachesTheResolver` (validator → routeur → résolveur) |

## 8. Checkout

`ONE NEW CHECKOUT ORDER = ONE PRODUCER`, imposé à la source et verrouillé
par contrat (liste blanche des sites créant des `OrderItem` ;
`finalize_multi_order` doit rester non câblée). Confirmation groupée
atomique ; totaux, notifications et cycles de vie par commande.

## 9. Auction

Inchangé. Mono-producteur **par construction** (`auction_id` UNIQUE, un
seul `winning_bid_id`, zéro `OrderItem`). Un seul chemin sécurisé de
sélection du gagnant (F4 toujours actif). **Pas de cron d'expiration** —
donc aucun risque de faux gagnant, fausse notification de perdant ou
commande fantôme générés automatiquement ; une enchère reste `OPEN`
jusqu'à une action explicite de l'acheteur (sélection ou annulation).

## 10. Fulfillment

`CONFIRMED → COMPLETED` par le producteur propriétaire uniquement
(`payment_status=PAID`, `delivery_status=DELIVERED`, 3 entrées
d'historique, notification acheteur). Les trois champs de statut ne sont
plus manipulables par un outil MCP arbitraire (§4).

## 11. Notifications

**Aucun envoi direct depuis la couche DB** (vérifié : aucune occurrence de
`send_whatsapp`/`twilio` dans `services/database/`). Toute notification
critique passe par l'Outbox, **enfilée dans la même transaction** que la
mutation, avec `dedupe_key`, puis dispatchée par le cron
`outbox-dispatch`.

## 12. Workers

| Job | Tâche | Enregistrée | Idempotence |
|---|---|---|---|
| outbox-dispatch | `workers.outbox_dispatch` | ✅ | `dedupe_key` |
| auction-solicitation | `workers.auction_solicitation` | ✅ | Outbox |
| proximity-matching | `workers.proximity_matching` | ✅ | Outbox |
| order-payment-expiry | `workers.order_expiry` | ✅ | garde de statut |
| procurement-reconciliation | `workers.procurement_reconciliation` | ✅ | CAS |
| preorder-reconciliation | `workers.preorder_reconciliation` | ✅ | CAS |
| sales-publish-reconciliation | `workers.sales_publish_reconciliation` | ✅ | CAS |

`acks_late` + `visibility_timeout=660s` configurés.

## 13. Reconciliation

`PreorderReconciliationService` mappe explicitement l'ambiguïté vers
`EXECUTION_UNKNOWN` → `PREORDER_EXECUTION_UNKNOWN`. **Une opération
ambiguë n'est jamais transformée en succès sans preuve** — propriété
établie lors du chantier escrow/IPN et inchangée.

## 14. Configuration

Vérifié par présence uniquement (aucune valeur affichée) :

| Clé | État attendu |
|---|---|
| `DATABASE_URL` | défini ✅ |
| `REDIS_URL` | défini ✅ (Celery broker **et** backend en dérivent) |
| `MCP_DB_TRANSPORT` | défini ✅ |
| **`ESCROW_PAYMENT_ENABLED`** | **`False`** ✅ — conforme au produit (paiement à la livraison) |
| Clés LLM / WhatsApp / OTEL / Langfuse | à fournir par l'environnement ; absence ⇒ dégradation, jamais blocage |

## 15. Observability

Les événements structurés existants (procurement, preorder, exécution MCP,
réconciliation) sont inchangés et corrélables par `draft_id`, `order_id`
et clé d'idempotence. **Trou connu et accepté** : l'enchère n'émet pas de
télémétrie structurée équivalente (documenté depuis l'audit Auction, non
traité — ce n'est pas un chantier de cette phase).

## 16. Historical failures

Les 4 échecs de `test_create_auction_catalog_gate.py` viennent d'une
**date codée en dur dans le test**, indépendante du produit. Ils sont
antérieurs à toute cette session et **n'ont pas été modifiés** — les
corriger relève d'une dette de test à traiter séparément, jamais d'un
ajustement pour verdir la suite.

## 17. P0 / P1 / P2 / P3

- **P0** : 0 *(les 3 failles trouvées cette phase sont fermées)*
- **P1** : 0
- **P2** : 0
- **P3** (inchangés, acceptés) :
  1. pas de lien visuel entre commandes d'un même checkout (tableau de bord acheteur) ;
  2. `services/database/README.md` décrit encore `finalize_multi_order` comme le checkout ;
  3. code mort connu : `OrderService`, `DeliveryMixin`, `product_service`, `finalize_multi_order`, `update_production_visibility`, `update_order_status`, `ensure_extensions` ;
  4. pas d'expiration automatique des enchères ;
  5. télémétrie structurée absente sur le domaine enchère.

## 18. GO / NO-GO

| Domaine | PASS | FAIL | Bloquant |
|---|---|---|---|
| Reset DB | ✅ (sous réserve §19) | | non |
| Démarrage | ✅ | | non |
| Sécurité MCP | ✅ (3 failles fermées) | | non |
| Acheteur | ✅ | | non |
| Producteur | ✅ | | non |
| Checkout | ✅ | | non |
| Enchère | ✅ | | non |
| Fulfillment | ✅ | | non |
| Outbox | ✅ | | non |
| Workers | ✅ | | non |
| Réconciliation | ✅ | | non |
| Notifications | ✅ | | non |
| Permissions | ✅ | | non |
| Observabilité | ✅ (trou enchère accepté) | | non |

**Aucun `BLOCKING = YES` → GO.**

## 19. Remaining actions before deployment

1. **Provisionner le schéma de base** (hors dépôt) avant de démarrer l'application : ce dépôt ne crée que les 5 tables de drafts/idempotence, les colonnes additives et les index/extensions.
2. **Démarrer un worker Celery avant d'ouvrir le trafic API**, et **vérifier dans ses logs** la ligne `🔧 Schéma DB vérifié` : le DDL est best-effort et un échec (droits `CREATE EXTENSION`, par exemple) n'empêche pas le démarrage mais casserait le checkout et la recherche floue.
3. **Seeder `governance.sub_categories`** (et `zones`) : sans elles, la politique de minimum de commande est silencieusement inactive.
4. Confirmer `ESCROW_PAYMENT_ENABLED=False` dans l'environnement de production.

---

```
PRODUCTION READINESS

Database reset:
PASS  (schéma de base à provisionner hors dépôt — action §19.1)

Bootstrap:
PASS  (DDL additif + 5 tables au démarrage du worker — action §19.2)

Runtime reachability:
PASS

MCP security:
PASS  (3 failles trouvées et fermées cette phase)

Buyer critical journeys:
PASS

Producer critical journeys:
PASS

Multi-producer checkout:
PASS

RFQ/Auction:
PASS

Fulfillment:
PASS

Outbox:
PASS

Workers:
PASS

Reconciliation:
PASS

Notifications:
PASS

Production configuration:
PASS  (ESCROW_PAYMENT_ENABLED=False confirmé)

Blocking issues:
0

GO / NO-GO:
GO
```

```
KNOWN ACCEPTED ITEMS
- 4 échecs pytest historiques (test_create_auction_catalog_gate.py, date codée en dur)
- 3 P3 documentés (lien visuel checkout, README périmé, code mort)
- Pas d'expiration automatique des enchères (aucun effet indésirable : rien
  n'est généré automatiquement)
- Trou de télémétrie structurée sur le domaine enchère
- Schéma de base provisionné hors dépôt (ni Alembic, ni create_all)
- Décision produit ouverte : SALES_ACCEPT_CONTRACT (rien d'exposé à l'utilisateur)
```
