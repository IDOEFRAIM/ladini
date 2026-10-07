# BUSINESS EDIT & CORRECTION — contrat

> La conversation INTERPRÈTE la correction. Le domaine POSSÈDE la mutation.

Le modèle ne produit jamais un identifiant, jamais un prix recalculé, jamais un total. Il émet une intention structurée
(`cart_edit`, `is_correction`, `remove`) ; le domaine valide, revalide (stock, minimum de commande), applique avec un contrôle de
version et répond par un statut explicite.

## 1. Panier acheteur (`domain/cart_edit.py`)

| Élément | Règle |
|---|---|
| Identité d'une ligne | `line_id` stable (pas l'index affiché, pas le nom) ; lignes anciennes migrées à la lecture (`with_line_ids`) |
| Champs éditables | `QUANTITY`, `PACKAGE_COUNT`, `REMOVE` |
| Quantité vs nombre de paquets | jamais confondus : sur une ligne à paliers, un nombre nu = nombre de paquets ; une unité de mesure égale à l'unité du contenu est refusée (« Combien de sachet… ? ») |
| Valeurs refusées | 0, négatif, NaN/inf, unité incompatible (`REJECTED`, panier inchangé) |
| Totaux | recalculés par le domaine ; jamais repris du modèle |
| Concurrence | `expected_version` (version vue par l'utilisateur) ≠ version courante → `CONFLICT`, aucune mutation |
| Rejeu | même valeur → `UNCHANGED` (idempotent, pas de bump de version) |
| Ligne ambiguë | plusieurs candidates → `AMBIGUOUS` + question ; jamais la première ligne |
| Ligne introuvable | `NOT_FOUND` |
| Stock / minimum | revérifiés avant `commit_edit` (I/O hors de la fonction pure `plan_edit`) |

Statuts : `APPLIED`, `UNCHANGED`, `CONFLICT`, `REJECTED`, `AMBIGUOUS`, `NOT_FOUND`. Échec = fermé (rien n'est muté).

### Pré-commande (`PreorderDraft`, Postgres CAS)

* Brouillon `DRAFT` : une correction du panier recompose le brouillon (nouvelle version, même `draft_id`), l'ancienne confirmation
  est périmée (`STALE_TARGET`) → nouveau récapitulatif, nouvelle confirmation. « oui mais mets 10 » = édition d'abord, jamais
  confirmation de l'ancienne valeur.
* Commande engagée (`EXECUTING`, `COMPLETED`, …) : jamais éditée en silence (« déjà engagée »).
* Routage : `BUYER_EDIT_CART` pendant une pré-commande active est remappé vers le goal de pré-commande avec l'évènement `UPDATE`
  (le planificateur de goals n'efface donc pas le brouillon).

## 2. Brouillon vendeur (`domain/sales_publish_draft.py`)

* Brouillon versionné immuable, modifiable seulement en `DRAFT`.
* Correction de quantité / prix : seul ce champ change, version +1.
* Correction du PRODUIT (`is_correction`) : les champs physiques compatibles (`quantity`, `unit`) sont conservés ; les champs liés au
  produit (prix, paliers, offre commerciale certifiée) sont purgés puis redemandés (« il me manque le prix »). Jamais la question
  « annuler et commencer la vente ? » pour une correction.
* Une valeur redite dans la correction n'est jamais écrasée.
* Le validateur laisse passer une correction vers la porte de confirmation même si elle rend la charge incomplète, pour que le brouillon
  actif (propriétaire de l'état) soit mis à jour avant que le champ manquant soit demandé.
* L'enrichissement textuel ne fabrique pas de 2ᵉ champ : « ah non c 350 le prix » corrige le prix, pas la quantité.

## 3. Contraintes de recherche acheteur (`domain/search_constraints.py`)

* Poser / remplacer / RETIRER (`remove: ["max_price"|"packaging"]`) ; « le prix n'importe plus » supprime réellement le plafond.
* La liste est recalculée à partir du pool des résultats d'origine (`search_pool`), jamais une relaxation silencieuse ni un plafond factice.
* Sans pool disponible, retirer une contrainte est refusé avec une explication.

## 4. Matrice de mutabilité

| Entité | État | Éditable ? | Mécanisme |
|---|---|---|---|
| Ligne de panier | présente | oui | `edit_cart_line`, CAS version |
| Panier | vu périmé | non (CONFLICT) | version |
| Pré-commande | `DRAFT` | oui (recomposition, version +1) | `bootstrap_preorder_draft` |
| Pré-commande | engagée / exécutée / terminée | non | garde « déjà engagée » |
| Brouillon de vente | `DRAFT` | oui | `UpdateSalesPublishDraft` |
| Brouillon de vente | publié / annulé | non | statut |
| Contraintes de recherche | menu actif | oui (poser/remplacer/retirer) | `merge_constraints` |
| Brouillon récurrent | modifiable seulement hors exécution | voir B25/B26 — non étendu par cette phase |
| Producteur / palier d'une ligne existante | — | NON (hors périmètre : exige un snapshot d'offre vivant) ; l'utilisateur est invité à rechercher à nouveau |

## 5. Observabilité

Journaux : `business_edit_resolved | continuation_of=…`, `business_edit_applied | entity=search_constraints | before=… | after=… | kept=…`,
`business_edit_resolved | entity=<draft> | route=confirmation_gate`. Aucune donnée personnelle dans les messages.

## 6. Preuves rouges (code cassé → test qui échoue)

| # | Cassure | Test qui devient rouge |
|---|---|---|
| R1 | contrôle de version du panier désactivé | `TestVersionConflict` |
| R2 | passage validateur → porte de confirmation retiré | correction de produit vendeur (e2e) |
| R3 | prix reporté sur le nouveau produit | `test_a_product_correction_keeps_the_physical_values…` |
| R4 | drapeau `is_correction` non transmis au domaine | `test_the_correction_flag_reaches_the_domain_action…` |

## 7. Limites déclarées

* Corpus avec le VRAI modèle (53 cas, dont 7 corrections de panier, 8 corrections vendeur, 1 retrait de contrainte) : 46/53 puis 49/53
  sur deux passes complètes ; les cas en échec changent d'une passe à l'autre (variance du modèle : « non 250 kg » lu comme prix,
  « finalement 8 litres » → menu de précision sans mutation, « c'est trop cher » parfois lu comme plafond). Aucun échec n'a muté
  l'état métier de façon erronée dans la dernière passe. Pas de seuil statistique garanti.
* Aucun nouveau test Postgres : le chemin pré-commande réutilise `bootstrap_preorder_draft` (couvert par les tests CI existants) ;
  en local la base est injoignable, la persistance du brouillon vendeur y est testée en mode dégradé.
* Éditions de brouillons récurrents, de producteur ou de palier d'une ligne existante : non traitées (voir matrice).
