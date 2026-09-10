# Audit de sécurité — agent `market_coach` — 2026-09-10

Second volet, ciblé sur l'agent IA lui-même (le premier portait sur la surface
HTTP : `SECURITY_AUDIT_2026-09-10.md`).

Résultat : **8 failles d'autorisation corrigées** (5 réellement atteignables,
3 latentes), **19 tests d'invariants** ajoutés. Suite complète : **3504 tests,
0 échec** (base 3485 → aucune régression).

---

## Le fil conducteur : un identifiant n'est pas une autorisation

Le premier audit avait établi que l'identité vient d'un **numéro de téléphone
reçu sur le réseau**. Celui-ci révèle la faille jumelle, un niveau plus bas :

> Dans cet agent, l'identité de l'appelant était correctement épinglée à la
> session — mais **les identifiants de RESSOURCE ne l'étaient pas**. Plusieurs
> outils recevaient un `farm_id` ou un `stock_id` comme **seul critère de
> ciblage**, sans jamais vérifier à qui la ressource appartient.

Et `farm_id` est un **slot déclaré** (`core/slots.py`) : sa valeur peut donc
atteindre `transaction_payload` par extraction LLM ou par `form_data` — c'est-
à-dire, in fine, **depuis le texte de l'utilisateur**. `select(Farm).where(
Farm.id == farm_id)` trouve n'importe quelle exploitation de la plateforme.

La règle qui manquait, désormais verrouillée par des tests :

> **Tout identifiant de ressource venu du payload est une entrée utilisateur.**
> La seule identité digne de confiance est celle du tour
> (`state["user_phone"]`, issue du webhook signé).

---

## P0-1 · IDOR sur `farm_id` — écriture chez le concurrent

Cinq outils utilisaient `farm_id` sans contrôle de propriétaire.

| Outil | État avant | Atteignable ? |
|---|---|---|
| `add_stock` | vérifiait l'EXISTENCE de la ferme, jamais son propriétaire | **oui** |
| `add_expense` | aucune vérification du tout | **oui** |
| `update_farm` | « prend farm_id en priorité » = `Farm.id == farm_id` sans filtre | **oui** |
| `remove_stock` | ne vérifiait même pas l'existence — ciblait `Stock.farm_id` | latent |
| `adjust_stock` | délègue aux deux précédents | latent |

« Latent » = défini mais absent de `MCP_EXPOSED_TOOLS`, donc non appelable
aujourd'hui — **exploitable dès qu'on l'y ajoute**. Corrigés quand même : la
sécurité ne doit pas dépendre du fait qu'un outil reste non exposé.

Impact réel : un producteur pouvait ajouter du stock fantôme chez un
concurrent, lui imputer des dépenses, altérer son exploitation (nom, zone,
surface — `producer_id` était protégé, donc pas de vol de propriété). Et, dès
`remove_stock` exposé, **détruire son inventaire vendable**.

### La cause racine, en amont des outils

`flows/producer/farm_logic.py::ensure_farm_node` **court-circuitait** dès
qu'un `farm_id` était présent :

```python
payload = dict(state.get("transaction_payload") or {})
if payload.get("farm_id"):
    return {}            # ← valeur ni validée, ni écrasée, ni contrôlée
```

C'est le nœud censé garantir un `farm_id` valide. Un identifiant fourni par le
payload n'était donc jamais confronté à l'appelant, à aucun étage.

**Correctif** — trois niveaux :

1. `BaseMixin._assert_farm_owned_by()` : nouvelle garde autoritaire
   (`Farm → Producer → User.phone`), fail-closed. Refus identique pour
   « n'existe pas » et « appartient à un autre » — ne pas transformer le refus
   en oracle d'énumération.
2. Les 5 outils exigent désormais `producer_phone` **sans valeur par défaut** :
   le schéma JSON exposé par MCP est dérivé de la signature, donc un appel sans
   identité est rejeté **en amont du handler** (constaté : un test existant a
   commencé à échouer avec `'producer_phone' is a required property` — la
   preuve que le contrôle mord).
3. `ensure_farm_node` n'accepte un `farm_id` du payload que s'il figure **parmi
   les fermes de l'appelant** ; sinon il l'écarte et la résolution normale
   reprend. Fail-closed sur l'incertitude (panne réseau → valeur écartée).

## P0-2 · L'identité du tour retombait sur le payload

Même fichier :

```python
phone = state.get("user_phone") or payload.get("phone")
```

`state["user_phone"]` vient du webhook signé ; `payload["phone"]` vient de
l'extraction LLM. Ce `or` faisait du numéro de téléphone une valeur usurpable
dès que `user_phone` manquait pour une raison quelconque. Le repli est
supprimé.

## P1-1 · Fuite inter-locataire en LECTURE

Trois lectures n'étaient scopées que par un identifiant de ressource :

| Outil | Ce qui fuyait | Atteignable ? |
|---|---|---|
| `get_expense_summary` | totaux de dépenses par catégorie de n'importe quelle ferme | **oui** |
| `get_stock_movements` | historique complet des mouvements de n'importe quel lot | **oui** |
| `get_expenses` | journal détaillé des dépenses | latent |

Ce n'est pas une fuite anodine : sur une place de marché où ces producteurs
s'affrontent en **enchères inversées**, connaître la structure de coûts et la
rotation d'inventaire d'un rival dit exactement **jusqu'où il peut baisser son
prix**. C'est du renseignement économique directement monétisable.

**Correctif** — `producer_phone` obligatoire + `_assert_farm_owned_by` /
nouvelle `_assert_stock_owned_by` (`Stock → Farm → Producer → User.phone`).

## P1-2 · Un paramètre d'autorisation lisible depuis le message qu'il contrôle

Trouvé **en corrigeant P0-1**, et le plus instructif du lot.

`services/mcp/schema_resolver.py::lookup_arg_value` épingle les paramètres
d'identité à la session — mais via une **liste de noms tenue à la main**
(`IDENTITY_ALIASES = {user_phone, phone, user_id, producer_id}`). Tout nom
absent retombe sur la résolution générique :

```python
sources = [initial_args, payload, extracted_entities, stable_entities, working_memory]
```

Donc : le `producer_phone` que je venais d'introduire **comme preuve de
propriété** aurait été résolvable depuis `payload`/`extracted_entities` —
c'est-à-dire depuis le texte de l'utilisateur. Un paramètre d'autorisation
lisible depuis l'entrée qu'il est censé contrôler n'est pas une sécurité,
c'est une décoration : toute la garde P0-1 serait devenue contournable en
faisant apparaître un autre numéro dans le message.

En vérifiant l'étendue du problème, **`buyer_phone` s'est révélé déjà
non épinglé** — alors qu'il est l'identité de l'appelant dans **10 outils**,
dont `confirm_preorder_draft` et `initiate_escrow_payment` (donc de l'argent),
où c'est le **seul** paramètre d'identité et il est **requis**. Tous les sites
d'appel réels le tiraient bien de la session, donc aucune exploitation connue —
mais rien ne l'imposait, et l'exécuteur possède un chemin de **réparation
d'arguments auto-cicatrisante** qui re-dérive les champs manquants depuis le
payload. C'était une faille à un bug de distance.

**Correctif** — `producer_phone` et `buyer_phone` épinglés à la session
(+ ajoutés au masquage PII des logs : ce sont des numéros de téléphone), et
surtout un **test anti-dérive** qui introspecte la signature des 63 outils
exposés et échoue sur tout paramètre d'identité non épinglé. L'invariant est
**sans exception** : la liste de dérogations est vide, et le commentaire dit
pourquoi (`buyer_phone` y avait d'abord été mis par supposition, puis retiré
après vérification signature par signature).

---

## Vérifié et jugé SAIN — la bonne nouvelle

Ces propriétés sont solides, et ce sont elles qui limitaient l'impact de tout
le reste. Elles méritent d'être nommées pour ne pas être cassées par
inadvertance :

**Le LLM ne choisit pas d'outil.** Il classe dans un ensemble **fermé**
d'intentions ; l'outil est résolu depuis ce goal via un registre peuplé par des
décorateurs `@register_action` **à l'import** — donc fixé au déploiement, jamais
au tour de conversation. Les arguments sont construits par des services de
domaine déterministes à partir de slots validés. **Le modèle ne nomme jamais un
outil et ne fournit jamais d'arguments bruts.**

C'est la propriété la plus importante de l'architecture : elle fait qu'une
injection de prompt **ne peut pas devenir une exécution d'outil arbitraire**.
Au pire, elle orienterait le choix d'intention *dans ce que l'utilisateur a
déjà le droit de faire* — et les identifiants de ressource sont désormais
contrôlés. C'est aussi la plus facile à détruire par inadvertance, en
« laissant le modèle décider » un jour. Verrouillée par test.

**Autres points vérifiés :** l'identité par téléphone vient toujours de
`state["user_phone"]` (aucun site d'appel ne la prend ailleurs) ; les outils
`*_by_id` passaient déjà `producer_id` ; `add_auction_photo`/`add_bid_photo`/
`place_bid`/`delete_product`/`update_product_price_and_qty`/
`cancel_pending_order` filtrent correctement par profil propriétaire ;
`TOOL_SCOPE_MAP` est fail-closed ; l'exposition des outils est un allowlist
explicite (`MCP_EXPOSED_TOOLS`).

**Isolation des rôles : ce n'est PAS une faille.** La refonte double-rôle est
délibérée — un même utilisateur vend et achète, le graphe est unifié, et le
blocage par préfixe de rôle est du code mort archivé. « Un acheteur peut
atteindre une intention producteur » est **le produit**, pas un bug. La
frontière réelle est l'identité + la propriété des ressources, c'est-à-dire
exactement ce que corrige cet audit. (La note d'index mémoire qui présentait
encore ces 6 tests comme « un constat de sécurité à arbitrer » était périmée
depuis leur réécriture de 2026-08 ; corrigée.)

## Non corrigé — à arbitrer par toi

**Les motifs de détection d'injection sont faibles.** 4 regex, **en anglais
uniquement** :

```
ignore all/every previous instructions | act as admin/administrator/system
reset the guardrails | disable security/moderation
```

L'agent parle **français** à des producteurs burkinabè : « ignore toutes les
instructions précédentes » passe sans être détecté.

Je ne l'ai **pas** corrigé, délibérément, parce que le bon arbitrage
t'appartient :

- L'impact réel est faible — grâce au registre d'outils fermé ci-dessus, une
  injection non détectée ne donne pas d'exécution arbitraire. Ces regex sont
  de la défense en profondeur, pas la barrière.
- Ajouter des motifs français **bloquerait des utilisateurs légitimes** : la
  contre-épreuve que j'ai écrite montre que « ignore la dernière commande, je
  veux annuler » est une phrase métier parfaitement normale, à un mot près
  d'un motif d'injection.
- Cela irait dans le sens inverse du principe `[[llm-decides-not-frozen-french-lists]]`
  déjà établi dans ce projet.

Mon avis : laisser tel quel, et si tu veux vraiment durcir, faire porter la
détection au LLM de modération (qui existe déjà dans ce nœud) plutôt qu'à des
listes de mots-clés.

## Bug fonctionnel repéré — CORRIGÉ (2026-09-10, dans la foulée)

`get_farm_stocks` était déclaré dans `TOOL_SCOPE_MAP`, `ToolId.GET_FARM_STOCKS`,
`INTENT_CONFIG["STOCK_GET_DETAIL"]` et `StockService.get_detail` — mais **aucune
méthode ne l'implémentait**. Le goal `STOCK_GET_DETAIL` était déjà neutralisé
côté LLM (`interpreter/routing.py::_DEPRECATED_INTENTS`, avec la note « outil
inexistant »), donc pas de chemin utilisateur cassé en pratique — juste de
l'échafaudage mort pointant sur un outil fantôme, plus une entrée
`TOOL_SCOPE_MAP` sans méthode.

**Correctif** — implémenté proprement plutôt que supprimé, car c'est une
capacité réelle (drill-down sur **une** exploitation, vs. la vue multi-sites de
`STOCK_GET_SUMMARY`) :

- `MarketplaceMixin.get_farm_stocks(farm_id, producer_phone)` : lignes de stock
  de la ferme, **enrichies du dernier mouvement par ligne** (date + type) — ce
  que la vue synthétique n'expose pas.
- `producer_phone` **obligatoire** + `_assert_farm_owned_by` : cette lecture
  relevait exactement de la même classe d'IDOR que `get_stock_movements` /
  `get_expenses` (P1-1). L'implémenter sans garde aurait rouvert la fuite.
- Ajouté à `MCP_EXPOSED_TOOLS` ; retiré de `_DEPRECATED_INTENTS` (donc de
  nouveau classable depuis un message) ; `StockService.get_detail` transmet le
  téléphone du tour ; `producer_phone` était déjà épinglé à la session par
  `schema_resolver` (P1-2).
- **Zéro dérive d'allow-list restante** : `MCP_EXPOSED_TOOLS - méthodes` = ∅.

Tests : `tests/unit/test_farm_ownership_idor.py::TestGetFarmStocksIsWiredAndSafe`
(refus d'une ferme tierce, forme du payload détaillé, câblage complet
EXPOSED_METHODS/TOOL_HANDLERS/SCOPE_MAP/allow-list, intent dé-déprécié) +
`get_farm_stocks` ajouté aux paramétrages `producer_phone`-requis. Validés par
réversion.

---

## Tests ajoutés (19)

- `tests/unit/test_farm_ownership_idor.py` (17) — la garde autorise le
  propriétaire, refuse un autre producteur, exige une identité, refuse un id
  malformé, **ne fait pas oracle d'énumération** ; vérifie que la requête
  **joint réellement `User.phone`** (sinon la garde redeviendrait un simple
  test d'existence — la faille d'origine) ; que les 5 outils exigent
  `producer_phone` **sans défaut** ; que la garde est réellement **invoquée**
  (paramètre non décoratif) ; qu'`adjust_stock` **propage** l'identité à ses
  deux délégués ; et que l'agent transmet bien le téléphone du tour.
- `tests/architecture/test_agent_identity_pinning.py` (19 cas) — épinglage des
  4 paramètres d'identité contre payload **et** `extracted_entities` ;
  **anti-dérive** sur les 63 outils exposés ; le LLM ne peut pas nommer
  d'outil ; une injection détectée coupe l'exécution ; contre-épreuve sur du
  français métier légitime.

Les deux fichiers ont été validés **par réversion** : correctif retiré → ils
échouent (5 et 7 échecs respectivement) ; remis → ils passent. Ils ne sont pas
décoratifs.

## Aucune configuration à changer

Contrairement au premier audit, ce volet n'introduit **aucune variable
d'environnement** et **ne modifie aucun comportement pour un utilisateur
légitime** : les flux réels transmettaient déjà le téléphone de la session,
ils continuent d'aboutir à l'identique. Seuls les appels visant la ressource
d'un tiers sont désormais refusés. Deux colonnes sont ajoutées à
`marketplace.orders` par le DDL idempotent du premier audit — rien ici.
