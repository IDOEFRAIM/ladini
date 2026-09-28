# Commercial Pricing — Phase B2b : Bid conversationnel → certification du prix → attribution

Suite de A (modèle), B1 (`SALES_PUBLISH_PRODUCT`), B2a (contrat de persistance). B2a a rendu la base capable de
stocker la vérité ; **B2b garantit que le flux BID la produit** et que l'acheteur n'attribue jamais sans confirmer
**prix + base du prix + quantité + total**.

> Un bid n'est jamais « 450000 » : c'est *450 000 FCFA par tonne*, *450 000 FCFA par kg*, *12 000 FCFA par caisse de 25 kg* ou
> *4 500 000 FCFA pour l'ensemble*. Base inconnue ⇒ **on demande** ; on ne déduit jamais, on n'attribue jamais.

## 1. État après B2a (vérifié)

- Backend #41 mergé ; CI de `main` avec **vrai PostgreSQL** (`REQUIRE_SCHEMA_DB=1`) : **5860 passés, 2 ignorés** — soit
  exactement les 382 tests PostgreSQL ignorés en local en plus, donc les 19 tests `test_commercial_pricing_persistence_pg.py`
  ont bien tourné et passé (migration, CHECK, triggers, lignes anciennes, TOTAL_LOT).
- **ladinifront#11 (Drizzle) était encore OUVERTE** : la migration `0012` existe dans le contrat backend embarqué
  (`schema_contract/`, qui est ce que le déploiement applique) mais pas dans `master` du dépôt Drizzle. Tout `drizzle-kit generate`
  depuis `master` régénérerait un état sans elle → **dérive**. B2b (migration `0013`) est empilée sur #11 : à merger dans l'ordre.
- Déploiement : migration avant backend (l'ORM mappe les colonnes) ; 0013 est un `CREATE OR REPLACE FUNCTION` (rollback-safe).

## 2. Audit immutabilité OrderItem (étape 2)

Question : une ligne avec `pricing_snapshot_version` peut-elle voir `quantity` / `base_unit_quantity` / `price_at_sale` modifiés
indépendamment du snapshot ? **Oui en B2a** (le trigger ne gelait que les colonnes du snapshot) ⇒ deux vérités économiques possibles.
Audit des écrivains : **aucun `UPDATE` d'`order_items`** côté backend ni côté web — seuls des tests (le mien, qui *affirmait* l'inverse).

**Option A retenue** — migration `0013_order_item_economic_freeze` : une ligne portant un snapshot est gelée sur *tout ce qui
décrit l'achat* (`quantity`, `base_unit_quantity`, `price_at_sale`, `tier_id`, `product_id`, `order_id` + les 11 colonnes du snapshot).
Une correction métier future = une nouvelle ligne, jamais une réécriture. Une ligne sans snapshot reste éditable ; le rattrapage
`NULL → renseigné` reste possible. Tests PostgreSQL dédiés (frozen / legacy éditable / réécriture identique tolérée).

## 3. Flux Bid — avant / après

| Étape | Avant B2b | Après B2b |
|---|---|---|
| question | « Quel prix proposez-vous ? (par unité, en FCFA) » | « Quel prix *par tonne* ? » (la question **fixe** la base) + « X pour tout » possible |
| lecture | `_first_number(text)` / `payload.price` = `offered_price` | `parse_bid_price` (`domain/bid_pricing_flow.py`) : montant + **base** + provenance |
| récap | « Votre prix : 450000 FCFA/{unité de l'enchère} » (**base inventée**) ; plafond comparé au prix brut | « 450 000 FCFA par tonne · 10 tonnes · Total 4 500 000 FCFA » ; plafond comparé au prix **normalisé** |
| écriture | `place_bid(offered_price)` sans base | `place_bid(offered_price, price_basis, price_unit, package…)` ; le serveur **refuse** un nouveau bid sans base |
| correction | nouveau nombre = nouveau prix (même « base » implicite) | montant nu = même base ; « finalement 4 millions pour tout » **change** la base (PER_BASE_UNIT → TOTAL_LOT) |
| liste (producteur / acheteur) | `offered_price CFA`, tri par montant brut | libellé propre à l'offre + total comparable ; tri par total ; offres sans base **après**, signalées |
| attribution | choisir une ligne ⇒ `select_winning_bid` (négociation) ; total `offered_price × quantité` | décision certifiée confirmée puis exécutée ; total selon la **base du bid** |
| commande | `total_amount` seul | + `orders.award_pricing_snapshot` gelé (termes exacts) |

Où la base se perdait : `_first_number` (unité jetée), récap (`FCFA/{auction.unit}`), `place_bid` (aucun champ), `update_bid_price`
(montant seul), `get_auction_bids` (`ORDER BY offered_price`), `select_winning_bid` (`offered_price * auction.quantity`).

## 4. Certification du prix (`domain/bid_pricing_flow.py`)

| Message | Résultat | Provenance |
|---|---|---|
| « 450000 la tonne » / « 450 000 fcfa/tonne » | 450000 · PER_BASE_UNIT · TONNE | USER_EXPLICIT |
| « 4,5 millions pour tout » / « 4 500 000 pour l'ensemble » | 4 500 000 · TOTAL_LOT · sans unité | USER_EXPLICIT |
| « 450000 » après « quel prix par tonne ? » | 450000 · PER_BASE_UNIT · TONNE | QUESTION_CONTEXT_EXPLICIT |
| « 450000 » sans question qui fixe la base (bid ancien à requalifier, deux montants…) | **NEEDS_BASIS** → « 450 000 FCFA, par tonne ou pour l'ensemble des 10 tonnes ? » ; **aucune écriture** | — |
| « 450000 la tonne pour tout » | contradiction → NEEDS_BASIS | — |
| « 12000 la caisse de 25 kg » | 12 000 · PER_PACKAGE · CAISSE 25 KG | USER_EXPLICIT |
| « 12000 la caisse » | NEEDS_PACKAGE_SIZE → « Quelle quantité contient une caisse ? » | — |
| « 500 le litre » sur une enchère en KG | **INVALID**, aucune écriture | — |
| enchère 1000 KG, « 450000 la tonne » | prix commercial 450 000/tonne, normalisé 450/kg, total 450 000 | USER_EXPLICIT |

Règles : le texte et la question décident ; le LLM ne fait que *suggérer* (`llm_hints`, journalisé, jamais autoritaire ; un « TOTAL_LOT »
suggéré sur un montant ambigu ne le résout pas). Le prix d'un bid est lu **sans LLM** (fast-path déterministe `routing.py` 0.46,
`bid_price_reply_expected`) : « 450000 » après « par tonne ? » ne dépend pas d'un classifieur. Un bid n'est écrit que si
`BidPriceParse.snapshot()` passe (base + provenance exécutable + compatibilité d'unité + divisibilité d'un conditionnement).

**Politique packages** : supportés *à contenu explicite* (`preferred_packaging` existe sur les enchères). Contenu absent → question ;
famille d'unité incompatible ou quantité non divisible (1000 kg / caisses de 30 kg) → refus ; jamais converti en prix/kg sans contrat.

## 5. Modification d'un bid

`update_bid_price(new_price, price_basis?, …)` : base fournie ⇒ snapshot recalculé, **la base peut changer** et un bid ancien est
**requalifié** ; omise ⇒ même base (bid certifié) ou base toujours inconnue (bid ancien — jamais déduite). Côté conversation,
la base est toujours transmise. Le montant nu garde la base affichée dans le récap ; un bid ancien pose la question de base.

## 6. Attribution — `CertifiedAwardDecision` (`domain/bid_award.py`)

Objet figé : `auction_id, bid_id, producer_id, buyer_id, pricing (snapshot), auction_quantity/unit, award_total, decision_version`,
**empreinte SHA-256** des termes canoniques et **clé d'idempotence** `award:{auction}:{bid}:{empreinte[:16]}` (mêmes termes ⇒ même clé ;
un terme changé ⇒ autre clé).

1. `get_auction_bids` renvoie, par offre : `pricing_label`, `price_basis`, `comparable_total`, `normalized_label` (affiché **en second**),
   `requires_requalification`, `pricing` (snapshot), `award_decision` (attribuable uniquement).
2. L'acheteur choisit une ligne ⇒ **confirmation** : producteur · offre + base · quantité · total ; la décision est gelée dans
   `working_memory.pending_award` (négociation : `negotiation_context.pending_award`).
3. Au « oui » (+ étape GPS existante) : revalidation contre l'état réel (empreinte) — termes changés ⇒ **nouvelle confirmation**,
   aucune exécution silencieuse ; offre retirée ⇒ refus ; offre devenue sans base ⇒ blocage.
4. `execute_award` (seul site d'appel de `select_winning_bid`) envoie `expected_award={fingerprint}` et la clé de la décision ;
   le serveur **revalide sous verrou** (`FOR UPDATE` bid + enchère) : propriété de l'enchère par l'acheteur, bid PENDING de cette
   enchère, snapshot présent, total cohérent, enchère non déjà attribuée, empreinte identique — sinon `award_terms_changed`.
5. Le total de la commande et `orders.award_pricing_snapshot` viennent de la décision (jamais `offered_price × quantité`).
6. Double « oui » : une seule attribution (enchère `CLOSED` + idempotence MCP sur la clé de la décision).

Ce que l'acheteur confirme = ce qui s'exécute : muter l'état brut (`pending_winner_price`, `transaction_payload.price`) n'a aucun effet
(test) ; muter le bid en base entre-temps déclenche une reconfirmation (test).

## 7. Bids anciens (base inconnue)

Stratégie : **blocage explicite + requalification par le producteur** (pas de déduction, pas de « par unité de l'enchère »).
L'acheteur voit « base de prix inconnue — à préciser par le producteur » dans la liste, et un message d'explication s'il tente
de le retenir ; côté serveur `select_winning_bid` refuse (`bid_basis_unknown`). Le producteur requalifie via *mes propositions* :
l'agent lui demande « par tonne ou pour l'ensemble ? ». Pas de notification proactive du producteur (B2c).

## 8. Comparaison

`compare_bid` : total = `snapshot.total_for(quantité, unité de l'enchère)` (PER_BASE_UNIT : montant × quantité convertie ; TOTAL_LOT :
le montant ; PER_PACKAGE : nombre entier de conditionnements × montant, sinon non comparable). Bids mixtes (450000/TONNE, 4 200 000
TOTAL_LOT, ancien 430000 inconnu) : les deux premiers sont classés par total, le troisième « non comparable ».

## 9. LLM

`NewTaskEntities` accepte désormais `price_basis`, `package_type`, `package_content_amount`, `package_content_unit` (facultatifs,
`extra="forbid"` ne rejette plus un LLM qui les émet). **Volontairement absents des prompts** (budget tokens, et parce que le domaine
n'en dépend pas) : *le LLM extrait, le domaine valide* — ici le domaine ne s'appuie que sur le texte et la question.

## 10. MarketOffer & recherche acheteur

- **MarketOffer** : écrit par `declare_future_production` (goal `PRODUCTION_DECLARE_FUTURE`), qui prend `payload["price"]` comme prix
  *par unité* sans base — la même classe de défaut que SALES_PUBLISH avant B1. Ce goal n'est pas dans le périmètre BID→AWARD :
  `pricing_snapshot` reste NULL. **B2c** : adapter `domain/agro.py` au `CommercialOffer` (question de base, TOTAL_LOT), puis écrire
  `market_offers.pricing_snapshot` depuis l'offre certifiée.
- **Recherche acheteur** : ~30 sites formatent `price/unit` (`cart.py`, `success.py`, `confirmation_summary.py`, `producer/flow.py`,
  `buyer.py`…). Migrer vers `product_pricing_view` élargirait trop cette PR ⇒ **B2b.2/B2c** dédié.
- **Web (ladinifront)** : `features/auction/services/auction-bid-submit.ts`, `auction-award.ts`, `auction-settlement.service.ts` sont un
  **écrivain parallèle** — bids sans base, classement par `offeredPrice` brut (le « cheapest wins » du settlement compare des bases
  hétérogènes), `totalAmount = offeredPrice × quantity`, commande + lignes sans snapshot. Ces bids web sont « base inconnue » : le flux
  WhatsApp les traite fail-closed, mais le chemin web garde l'ancien défaut. **B2c (dépôt frontend)**.

## 11. Observabilité

`BID_PRICING_PARSED`, `BID_PRICE_BASIS_RESOLVED`, `BID_PRICE_BASIS_AMBIGUOUS`, `BID_PRICING_CERTIFIED`, `BID_PRICING_PERSISTED`,
`BID_PRICE_BASIS_REQUIRED`, `BID_AWARD_PREPARED`, `BID_AWARD_CERTIFIED`, `BID_AWARD_EXECUTING/EXECUTED`, `BID_AWARD_TERMS_CHANGED`,
`BID_LEGACY_BASIS_UNKNOWN` — `flow_id` (conversation masquée), `auction`, `bid`, `version`, `fingerprint`/`idempotency_key`, jamais le texte.

## 12. Tests

| Fichier | Rôle |
|---|---|
| `tests/unit/test_bid_pricing_flow.py` | lecture du prix, bases, provenance, ambiguïtés, unités incompatibles, packages |
| `tests/unit/test_bid_award_decision.py` | décision figée, empreinte/clé, totaux, comparaison mixte |
| `tests/unit/test_bid_pricing_award_server.py` | `place_bid`/`update_bid_price`/`select_winning_bid`/listes côté serveur |
| `tests/integration/test_bid_pricing_conversation.py` | **vrai graphe** : golden A–F producteur + acheteur, modification, legacy |
| `tests/nodes/test_finalize_winner_stale_recap.py` (réécrit), `test_negotiation_gps_autoattach.py` (réécrit) | revalidation d'empreinte, mutation d'état brut |
| `tests/architecture/test_bid_pricing_award_contract.py` | verrous (un site d'exécution, base transportée, plus de `_first_number`, gel 0013) |
| `tests/schema/test_commercial_pricing_persistence_pg.py` | PostgreSQL réel : gel économique, requalification, snapshot d'attribution |

## 13. Reste pour B2c

1. Production future (`MarketOffer.pricing_snapshot`) et goals PROCUREMENT / PRODUCTION / PREORDER / RECURRING encore sur `reconcile_price_basis`.
2. Chemin web (bid submit / award / settlement) — snapshot + base côté Next.js.
3. Affichage de recherche acheteur via `product_pricing_view`.
4. Notification proactive du producteur pour requalifier un bid sans base ; requalification en masse (audit SQL puis décision humaine).
5. Tests conversationnels d'attribution jusqu'à la commande *avec* étape GPS (la réponse « oui » à la question GPS passe par le LLM par conception).
6. Modes de vente par conditionnement (`TIER_DEPENDENT`).
