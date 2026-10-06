# Onboarding PROGRESSIF (V2) — contrat

```
Ladini ne fait pas s'inscrire les gens avant de les comprendre.
On comprend l'intention, on identifie la prochaine action métier réelle, puis on ne demande QUE ce que cette
action exige. La friction est proportionnelle au risque métier.
```

## 1. Premier contact

Un nouveau numéro devient un **CONTACT** (ligne `auth.users` : téléphone, rôle neutre `USER`, **aucun nom, aucune région,
aucun rôle inventé**) puis la conversation démarre normalement : salutation, aide, recherche… Aucune question de profil.
`bonjour` → accueil neutre ; `tomate` → clarification acheter/vendre ; `je cherche 100 kg de tomates` → intention d'achat.

Réglage : `PROGRESSIVE_ONBOARDING_ENABLED` (défaut `true`). `false` rétablit l'ancien parcours (formulaire d'abord) ;
si la création du contact échoue, le système retombe sur l'ancien parcours plutôt que de perdre l'utilisateur.

## 2. Découverte ≠ engagement ≠ vente ≠ financement

| Action | Exemples d'objectifs (`goal`) | Requis |
|---|---|---|
| DISCOVERY | salutation, aide, recherche, prix, disponibilité, panier, suivi de ses propres objets | rien |
| BUY_REQUEST | `BUYER_REQUEST`, `PROCUREMENT_CREATE_REQUEST`, `CREATE_RECURRING_NEED` | région |
| BUY_COMMIT | `BUYER_PREORDER_INIT/CONFIRM`, `BUYER_CREATE_PREORDER` | nom + région |
| SELL | `SALES_PUBLISH_PRODUCT`, `SALES_RECORD_DIRECT`, `STOCK_REGISTER_HARVEST`, `PRODUCTION_DECLARE_FUTURE`, `SALES_PLACE_BID`, `FARM_CREATE` | nom + région |
| FINANCING | scoring / financement (non câblé : aucun flux aujourd'hui) | nom + région + identité vérifiée |

Source unique : `domain/profile_requirements.py` (`REQUIREMENTS`, `action_for_goal`). Aucune règle `if missing_name` ne vit
dans un flow ; l'unique point d'application est `core/profile_gate.py`, appelé par `DomainRouter.resolve` (avant tout flow
acheteur/producteur).

- **Nom** = nom d'affichage : personne **ou établissement** (« Restaurant Wend Konta »). La base n'a qu'un champ `name`,
  utilisé comme `display_name` — **ni prénom ni nom ne sont requis** (aucune colonne `first_name`/`last_name`). Les noms de
  repli système (« Utilisateur », « User_1234 », « Client ») ne comptent jamais comme un nom connu.
- **Région** = l'une des 17 régions (`domain/burkina_regions.py`). Comptée comme connue si `zone_id` OU `declared_location`.
  Jamais de sous-zone. Une région dite avec la demande (« …à Bobo ») est enregistrée (canonique, `Guiriko`) et **pas redemandée**.

## 3. Collecte juste-à-temps, intention préservée

```
« j'ai 300 kg d'oignons à vendre à 250 FCFA le kg »      -> intention SELL comprise (goal + payload dans l'état)
 gate : nom manquant                                      -> « Avant de publier… comment t'appelles-tu ? »  (UNE question)
« Moussa »                                                -> nom enregistré ; région manquante -> « Dans quelle région… ? »
« je suis à Bobo »                                        -> région Guiriko enregistrée ; capacité vendeur ajoutée ;
                                                             l'action REPREND dans le même tour (rien à répéter)
```

Mécanique (aucun nouveau routeur ni moteur) : `profile_gate` (durable) mémorise `{goal, action, missing, event, intent, status}` ;
le tour suivant passe par `onboarding_node` en « mode gate » (`flows/common/profile_gate_turn.py`) qui réutilise l'extracteur
et le résolveur de régions existants ; `_route_after_onboarding` choisit :

- `RESUME` → `context_resolver` (l'action reprend avec l'événement/l'intention d'origine) ;
- `RELEASE` → `input_interpreter` (le message n'était pas une réponse : l'utilisateur change de sujet, il n'est **jamais
  prisonnier** du mini-parcours) ;
- sinon → `response_strategy` (question suivante).

Une question à la fois ; les informations déjà connues ne sont jamais redemandées ; le slot commercial (base du prix, etc.)
est réglé avant l'identité : la question de profil survient au moment de **publier/engager**.

## 4. Capacités cumulatives (multi-rôle)

Un utilisateur peut acheter **et** vendre avec la même identité (jamais un second compte).
- **Acheter** : la ligne `BuyerProfile` est créée à la demande par le service (`get_buyer_profile`, déjà en place).
- **Vendre** : une fois le profil minimum complet, `complete_user_profile(capability="SELL")` ajoute la ligne `Producer` en
  statut **`PENDING`** — jamais « vérifié/approuvé ». La **vérification producteur** (admin, coopérative) n'est pas
  contournée : un profil complet n'est pas un profil vérifié (`CapabilityState.PRODUCER_READY` ≠ `PRODUCER_VERIFIED`).
- États conceptuels **dérivés** (jamais persistés) : `CONTACT`, `BUYER_DISCOVERY`, `BUYER_READY`,
  `PRODUCER_PENDING_PROFILE`, `PRODUCER_READY`, `PRODUCER_VERIFIED` (`capability_state`).

## 5. `onboarding_complete`

Colonne `auth.users.onboarding_completed` **conservée** : indicateur historique (« a terminé l'ancien formulaire »), **plus un
gate universel**. Le seul gate est « cette ACTION a-t-elle ce qu'il lui faut ? » (§2).

## 6. Utilisateurs existants

Aucun ré-onboarding : un acheteur/producteur dont nom + région sont connus n'a **aucune friction nouvelle** (testé). Un
profil ancien sans région sera interrogé UNE fois, seulement s'il engage/vend.

## 7. Sécurité / confiance

- Un anonyme ne publie pas : SELL exige nom + région **et** la capacité ; il n'obtient aucun droit producteur avant.
- L'interpréteur (LLM) comprend ; **les faits de profil viennent de la base** (`load_user_profile`/orchestrateur) ; le LLM ne
  décide jamais qu'un utilisateur est vérifié.
- Journaux structurés sans PII : `profile_gate_triggered` (goal, action, `missing_requirement`, `capability_requested`),
  `profile_gate_completed`, `profile_gate_released`, `region_learned_from_request`, `capability_requested`.

## 8. Limites assumées

- Le profil est demandé quand le flow **va s'exécuter** (après les slots métier) — pas à l'instant où l'intention est détectée.
- Le gate s'applique aux flows passant par `DomainRouter.resolve` (publication, stock, précommande, demande, besoin récurrent…) ;
  le panier/négociation/suivi (DISCOVERY) n'en dépendent pas.
- Pas de migration de schéma : `name`, `zone_id`, `declared_location`, `role='USER'` existaient déjà ; aucune table d'organisation
  créée (le nom d'établissement tient dans `name`).
- La correction de profil (« non je suis dans le Guiriko ») utilise les mécanismes existants ; non refondue ici.
- Les KPI « première valeur avant identité complète » restent à brancher sur les journaux ci-dessus.
