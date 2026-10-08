# Campagnes proactives de disponibilités (WhatsApp)

LADINI transforme des disponibilités agricoles **réelles** en messages courts adressés à des acheteurs **consentants**.
L'acheteur répond naturellement ; la réponse est rattachée à la campagne puis suit le parcours d'achat existant.
Un intérêt n'est **jamais** une commande.

## Vue d'ensemble

```
opérateur (API interne) ─ crée ─▶ DRAFT ─ aperçu ─▶ validate ─▶ SCHEDULED ─ tick Beat ─▶ RUNNING ─▶ COMPLETED
                                                                    (WEEKLY / CUSTOM_DAYS : retour à SCHEDULED, date suivante)
DRAFT | SCHEDULED | RUNNING ─ cancel ─▶ CANCELLED          échec sans offre éligible ─▶ FAILED (ONCE)
```

| Composant | Rôle |
|---|---|
| `services/availability_campaigns/eligibility.py` | règle pure d'éligibilité d'une offre + sélection (≤ 5, déterministe) |
| `.../message.py` | rendu pur du message depuis des offres figées, empreinte du contenu |
| `.../targeting.py` | ciblage pur, sans doublon, reproductible |
| `.../campaign_service.py` | cycle de vie (CAS), exécution par lots, statuts de livraison, métriques |
| `.../consent_service.py`, `consent_text.py`, `compliance_gate.py` | consentement / désinscription, détection déterministe, garde d'entrée |
| `.../interest_service.py` | attribution d'une réponse, intérêt idempotent, notification producteur |
| `.../api.py` + `api/routes/availability_campaigns_admin.py` | API interne opérateur |
| `workers/crons/availability_campaign_run.py` | tick Celery Beat (`workers.availability_campaign_run`) |
| `workers/outbox/campaign_hooks.py` | recontrôle avant envoi, report de l'issue (même transaction que l'outbox) |
| `services/database/campaign.py` | outil MCP `record_campaign_interest` (écriture idempotente) |

Réutilisé tel quel : outbox + dispatcher, Celery Beat, mécanisme de réponse aux messages proactifs, parcours d'achat
(`BUYER_REQUEST` → panier → précommande), API interne `X-Internal-Token`. Aucun nouveau routeur conversationnel, aucun
second moteur de commande.

## Données (migration `0016_availability_campaigns`, schéma `intelligence`)

* `availability_campaigns` : définition, planification, contenu figé (`preview`, `content_hash`, `content_version`).
* `availability_campaign_recipients` : **un suivi indépendant par destinataire et par exécution** ;
  `UNIQUE (campaign_id, run_key, phone)` est la garde anti-doublon en base.
* `communication_consents` : `OPTED_IN` / `OPTED_OUT` par (téléphone, sujet), source, preuve (version du texte,
  empreinte du message — jamais le contenu brut), `UNIQUE (phone, topic)`.
* `availability_interests` : intérêt exprimé, écarts offre présentée / vivante, `UNIQUE (recipient_id, message_ref, product_label)`.

Le miroir SQLAlchemy est dans `domain/intelligence/campaign_models.py` ; le contrat Drizzle embarqué
(`schema_contract/`) est à jour.

## Éligibilité d'une offre

Source : `marketplace.products` joint à `marketplace.producers`. Une offre n'est retenue que si **toutes** ces
conditions tiennent ; les raisons d'exclusion sont stables et visibles de l'opérateur :

| Raison | Condition d'exclusion |
|---|---|
| `INACTIVE` | `is_available = false` |
| `EXPIRED` | date d'expiration dépassée (aucune n'existe sur `Product` aujourd'hui : prévu pour une source qui en aurait une) |
| `ZERO_QUANTITY` | quantité disponible (– quantité réservée) ≤ 0 |
| `STALE` | `updated_at` absent ou plus ancien que `AVAILABILITY_OFFER_MAX_AGE_HOURS` (96 h) |
| `PRODUCER_NOT_APPROVED` | statut producteur ∉ {APPROVED, VERIFIED, ACTIVE} |
| `MISSING_FIELD` / `INCOMPATIBLE_UNIT` | nom ou unité absents ; unité inconnue du référentiel |
| `PAST_DELIVERY_DATE` | date de livraison de la campagne déjà passée |
| `PRICE_REQUIRED` | campagne avec `require_price` et prix non fiable |
| `OVER_LIMIT` | éligible mais au-delà du plafond de 5 (information) |

**Prix** : affiché seulement s'il est fiable (`product_pricing_view` : `CERTIFIED`, ou libellé de paliers). Sinon
« prix à confirmer » ; jamais déduit d'une autre offre, jamais d'unités différentes comparées.

**Fraîcheur** : le schéma n'a **aucune** colonne « disponibilité confirmée le … ». La seule preuve est
`Product.updated_at`. Le renouvellement = le producteur met son produit à jour. Le message dit « déclarées au <date>, à
reconfirmer à la commande » : une disponibilité n'est jamais présentée comme garantie.

## Cycle de vie et exécution

1. **Créer** (brouillon) : audience (régions, `audience_kind` ALL/KNOWN/NEW), filtre d'offres (produits, catégories,
   régions, `max_offers` ≤ 5), `send_at`, `frequency` (ONCE / WEEKLY / CUSTOM_DAYS + `interval_days`),
   `response_window_hours`, `delivery_date`.
2. **Aperçu** : offres retenues, offres exclues avec raisons, message rendu, empreinte, taille de l'audience.
3. **Valider** : refusée sans offre éligible ; si l'opérateur fournit l'empreinte vue à l'aperçu et que le contenu
   vivant a changé, la validation est refusée (409). Fige offres + message + empreinte ; passe en `SCHEDULED`.
4. **Tick** (toutes les `AVAILABILITY_CAMPAIGN_TICK_SECONDS`, 60 s) : réclame les campagnes dues
   (`FOR UPDATE SKIP LOCKED`). À chaque exécution les offres sont **revalidées** : une campagne `ONCE` n'envoie que
   les offres validées par l'opérateur (quantités/prix revérifiés) ; une campagne récurrente re-sélectionne selon son
   filtre. Aucune offre éligible → `FAILED` (ONCE) ou saut à la date suivante, rien n'est envoyé.
5. **Lots** de `AVAILABILITY_CAMPAIGN_BATCH_SIZE` destinataires : `PREPARED → QUEUED` **et** insertion outbox dans la
   même transaction. Une campagne récurrente avance sa date après complétion, sans rattrapage des occurrences manquées.
6. **Annuler** : plus aucun lot ne démarre, les `PREPARED` deviennent `SKIPPED(campaign_cancelled)`, le dispatcher
   refuse d'envoyer ce qui est déjà en file. Idempotent ; impossible une fois terminée.

## Consentement et désinscription

* Avoir déjà écrit à LADINI **n'est pas** s'être inscrit : seul un `OPTED_IN` avec preuve autorise l'envoi.
* Sources d'opt-in : demande explicite (« je veux recevoir les disponibilités »), « oui » à la question de consentement
  envoyée dans les 48 h, import administrateur avec preuve (`evidence` obligatoire).
* Désinscription : mots d'arrêt nus (« stop », « arrêt », « désinscription »…) ou phrases visant les **envois**
  (« arrêtez de m'envoyer… »). « Arrêtez la commande » n'est jamais une désinscription. Traitée par
  `compliance_gate` **avant le graphe**, sans LLM, idempotente (rejouable), confirmation immédiate.
* Une désinscription n'est jamais renversée par un import ; seule la personne peut se réinscrire.
* **Le consentement est recontrôlé deux fois** : à la mise en file (verrou `FOR SHARE` : une désinscription concurrente
  attend la fin du lot) et juste avant l'appel fournisseur (`before_send`). La désinscription gagne toujours.
* Nouvel acheteur inconnu : peut recevoir une campagne de découverte (`audience_kind: NEW`) **s'il a consenti**.

## Règles WhatsApp

* Meta Cloud : `send_template` ajouté. Message de campagne = **modèle approuvé** si `WHATSAPP_CAMPAIGN_TEMPLATE_NAME`
  est défini (corps à une variable `{{1}}`) ; sinon texte libre **uniquement** si la fenêtre de service de 24 h est
  ouverte ; sinon **refus explicite** (`SKIPPED / template_required`). Jamais de repli sur du libre hors fenêtre
  (Twilio : le repli libre est désactivé pour les campagnes).
* Fenêtre de service : proxy = dernier tour traité (`agri_workspaces.updated_at`), recalculée juste avant l'envoi.
* Débit : lots bornés + dispatcher d'outbox (50 messages / 30 s, pause entre envois) ; backoff 1/5/15/60/180 min.
* Statuts : `delivered` / `read` / `failed` reçus par webhook (Meta et Twilio) et appliqués par référence fournisseur,
  **transitions monotones** (jamais de recul, un échec tardif n'écrase pas « lu »). On ne prétend connaître que ce que
  le fournisseur remonte ; « envoyé » signifie « accepté par le fournisseur ».
* Message toujours terminé par « Répondez STOP pour ne plus recevoir ces messages. »

## Idempotence et reprise

| Situation | Garantie |
|---|---|
| Campagne exécutée deux fois | `UNIQUE (campaign_id, run_key, phone)` + `dedupe_key` outbox : rien de plus |
| Deux workers | lot réclamé en `SKIP LOCKED`, campagne en `SKIP LOCKED` : aucun doublon |
| Worker arrêté entre deux lots | la campagne reste `RUNNING`, le tick suivant reprend sans renvoyer le déjà mis en file |
| Insertion outbox faite, état local non écrit | la ligne existante est rattachée, jamais réinsérée |
| Annulation entre deux lots | lots suivants refusés, déjà en file bloqués à l'envoi |
| Worker mort **pendant** l'envoi | issue inconnue : `FAILED(send_outcome_unknown)` après 15 min, **jamais renvoyé** automatiquement |
| Envoi réussi mais état non écrit | idem : traité comme issue inconnue (aucun doublon, mais statut `FAILED` visible à l'opérateur) |
| Rejeu du même message entrant | intérêt et notification producteur créés une seule fois |

## Réponses conversationnelles

Le contexte de campagne est une **couche d'attribution** sur le parcours `BUYER_REQUEST` existant
(`flows/buyer/cart.py`), pas un nouveau flux : dès que la recherche a trouvé des producteurs, l'outil
`record_campaign_interest` rattache la demande à la dernière campagne reçue (dans `response_window_hours`).

* La recherche utilise toujours les données **vivantes** : une réponse tardive est donc revalidée (statut, quantité,
  prix). Si l'offre a changé, une note courte le dit (« il reste 120 kg (au lieu de 500 kg) »).
* Plusieurs offres correspondantes → le menu producteurs existant impose le choix ; l'intérêt est enregistré
  `ambiguous` sans produit et **sans** notification producteur. Jamais de choix arbitraire.
* Corrections (« plutôt les oignons », « c'est trop cher », « finalement 200 kg ») : traitées par le moteur existant ;
  chaque message distinct est tracé séparément, une correction n'est jamais une confirmation.
* Toute suite (panier, précommande, confirmation) = parcours existant et ses protections. Un simple intérêt ne crée
  ni commande, ni réservation, ni paiement.

## Intérêt et retour aux producteurs

`availability_interests` conserve produit, quantité, unité, écarts, état (`OPEN` → `PRODUCER_NOTIFIED`). La
notification producteur (outbox, `dedupe_key = availability_interest:<id>`) contient produit, quantité, acheteur
(nom), zone (région), état « intérêt exprimé, pas encore une commande » — jamais le téléphone. Pas de notification si
l'offre n'est plus disponible. Un intérêt enregistré dont la notification a échoué reste `OPEN` et est repris par
`notify_open_interests`.

## Indicateurs (dénominateurs explicites)

`GET /campaigns/{id}/results` : préparés, écartés, échoués, **acceptés fournisseur**, livrés, lus, répondus, intérêts
par type, désinscriptions après envoi.
* `delivery_rate = livrés / acceptés`.
* `reply_rate = répondus / livrés` (ou `/ acceptés` si la livraison n'est pas mesurable — indiqué).
* Un intérêt n'est pas une vente ; aucun taux de conversion en vente n'est affiché.

## API interne (`X-Internal-Token`, écritures : `actor_id` ADMIN re-vérifié et audité)

```bash
BASE=http://localhost:8000/internal/availability-campaigns; H='X-Internal-Token: <token>'
curl -H "$H" -H 'Content-Type: application/json' -d '{"actor_id":"<admin-uuid>","name":"Légumes semaine 41","frequency":"WEEKLY","send_at":"2026-10-12T08:00:00","offer_filter":{"products":["tomate","oignon"],"max_offers":5},"audience":{"regions":["Guiriko"]},"response_window_hours":48}' $BASE/campaigns
curl -H "$H" $BASE/campaigns/<id>/preview
curl -H "$H" -H 'Content-Type: application/json' -d '{"actor_id":"<admin-uuid>","content_hash":"<hash vu à l aperçu>"}' $BASE/campaigns/<id>/validate
curl -H "$H" -H 'Content-Type: application/json' -d '{"actor_id":"<admin-uuid>"}' $BASE/campaigns/<id>/cancel
curl -H "$H" $BASE/campaigns/<id>/results
curl -H "$H" -H 'Content-Type: application/json' -d '{"actor_id":"<admin-uuid>","evidence":"formulaire papier 12/09","phones":["+22670000001"]}' $BASE/consents/import
curl -H "$H" -H 'Content-Type: application/json' -d '{"actor_id":"<admin-uuid>","phones":["+22670000002"]}' $BASE/consents/request
```

Autres routes : `GET /campaigns?status=`, `GET/PATCH /campaigns/{id}`, `GET /campaigns/{id}/recipients`,
`POST /offers/eligible`.

## Réglages

`WHATSAPP_CAMPAIGN_TEMPLATE_NAME`, `WHATSAPP_CAMPAIGN_TEMPLATE_LANGUAGE`, `AVAILABILITY_OFFER_MAX_AGE_HOURS` (96),
`AVAILABILITY_CAMPAIGN_BATCH_SIZE` (50), `AVAILABILITY_CAMPAIGN_TICK_SECONDS` (60).

## Tests

* `tests/unit/test_availability_campaigns_pure.py` : éligibilité, message, ciblage, planification, détection d'arrêt,
  comparaison offre présentée / vivante.
* `tests/schema/test_availability_campaigns_pg.py` (**PostgreSQL réel**, `SCHEMA_TEST_DSN`) : unicité en base,
  exécution répétée, trois workers concurrents, reprise, doublon outbox, annulation entre lots, désinscription avant/
  après/en concurrence, garde de conformité, statuts monotones, intérêt rejoué, notification échouée puis reprise,
  réponse tardive/ambiguë/hors fenêtre, métriques.

## Limites connues

* Pas de colonne « disponibilité confirmée le » : fraîcheur = `updated_at`. Pas de quantité réservée sur `Product`
  (la quantité réservée n'est prise en compte que si une source la fournit). Offres futures (`MarketOffer`) hors périmètre v1.
* Le **modèle WhatsApp** doit être créé et approuvé côté Meta Business ; sans lui, hors fenêtre de 24 h l'envoi est
  refusé volontairement (`template_required`).
* Le **miroir Drizzle TypeScript** (`ladinifront`) reste à créer ; le contrat embarqué ici est à jour.
* Fenêtre de service = proxy (dernier tour traité), pas l'horodatage exact du dernier message entrant.
* Envoi dont l'issue est inconnue (worker mort pendant l'appel) : marqué `FAILED(send_outcome_unknown)`, jamais renvoyé ;
  le message a pu partir. Les statuts de livraison ne se rattachent pas dans ce cas (pas de référence fournisseur).
* Pas de back-office : API interne uniquement.
* Débit global limité par le dispatcher d'outbox partagé ; pas de limite par palier de messagerie Meta.
