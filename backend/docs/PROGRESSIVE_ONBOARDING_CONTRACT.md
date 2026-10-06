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
- Journaux structurés sans PII : `profile_gate_paused` (goal, action, `missing_requirement`, `capability_requested`),
  `profile_gate_completed`, `profile_gate_released`, `region_learned_from_request`, `capability_requested`.

## 8. Limites assumées

- Le profil est demandé quand le flow **va s'exécuter** (après les slots métier) — pas à l'instant où l'intention est détectée.
- Le gate s'applique aux flows passant par `DomainRouter.resolve` (publication, stock, précommande, demande, besoin récurrent…) ;
  le panier/négociation/suivi (DISCOVERY) n'en dépendent pas.
- Pas de migration de schéma : `name`, `zone_id`, `declared_location`, `role='USER'` existaient déjà ; aucune table d'organisation
  créée (le nom d'établissement tient dans `name`).
- La correction de profil (« non je suis dans le Guiriko ») utilise les mécanismes existants ; non refondue ici.
- Les KPI « première valeur avant identité complète » restent à brancher sur les journaux ci-dessus.

## 9. Profile Gate Pause/Resume Contract (V2.1)

Le gate est une **barrière PAUSE/REPRISE**. Il n'est jamais un reset, une reconstruction ni un nouvel onboarding.

```
BUSINESS_ACTIVE ─(profil manquant)─► PAUSED_FOR_PROFILE ─(champ fourni)─► encore un champ ? ─oui─► question suivante
                                                                                   │ non
                                                                                   ▼
                                                                  BUSINESS_RESUMED  (MÊME commande, MÊME tour)
```

1. **Pause** (`core/profile_gate.py::build_gate`) : photographie de la commande — `current_goal`, `detected_intent`,
   `interpreted_event`, `interpreter_confidence`, `transaction_payload`, `extracted_entities`, `status` — dans `profile_gate["command"]`
   (copie profonde, durable). Le panier / `preorder_draft` / `vendor_selection_context` ne sont pas touchés (champs distincts).
2. **Lecture du slot** (`flows/common/profile_slot.py::read_profile_slot`) : le message suivant est lu comme réponse au slot demandé
   (contexte du slot + extraction structurée + validation). Le modèle ne renvoie que des **faits** `{kind, value, confidence}` ;
   `kind ∈ ANSWER | INTERRUPTION | REFUSAL | QUESTION | UNCLEAR`. Jamais une réplique conversationnelle (l'ancien extracteur
   d'inscription et son discours « créer un compte » ne sont plus appelés). Région : résolveur déterministe des 17 régions d'abord.
   - `ANSWER` → enregistrement (`complete_user_profile`) puis question suivante ou reprise ;
   - `INTERRUPTION` (« montre-moi d'abord le prix ») → le gate se relâche, le message repart vers l'interpréteur (B27 inchangé) ;
   - `REFUSAL`/`QUESTION` → explication courte, la demande reste en attente ; `UNCLEAR` ×3 → relâche (jamais prisonnier).
3. **Reprise** (`profile_gate_turn.build_resume_patch`) : la commande capturée est restaurée **à l'identique** (objectif verrouillé via
   `lock_goal`, intention, événement, payload, entités) ; seule la région canonique remplace un repli de zone (« Zone inconnue »).
   Le message de profil (« Mon nom c'est Zouba ») n'est **jamais** utilisé pour reconstruire la commande.
4. **Fail-closed** : sans objectif exploitable → message sûr (`SAFE_RESUME_FAILURE`), panier conservé, log `business_resume_failed`.
   Le gabarit générique de précommande n'annonce plus « pour 0 pour votre demande » (produit/quantité absents → message sûr).
5. **Idempotence** : un message rejoué après reprise ne recrée pas de précommande (le gate est consommé, `message_sid` unique).

**Noms d'affichage** : primitive unique `domain/profile_requirements.py::is_placeholder_display_name` / `display_name_or_none`
(`User_2876`, `Utilisateur`, `Guest`, `Contact_123`, `Client`, `Zone inconnue`…) — utilisée par la salutation, le chargeur de profil,
l'orchestrateur et la sérialisation. Un contact sans nom n'a **pas** de nom affiché (pas de repli technique).

**Sélection naturelle** : « le quatrième m'intéresse » (menu producteurs actif) → micro-prompt STRUCTURED_ACTION → `SELECT_PRODUCER`
résolu en Python vers l'offre réelle, relayé par `buyer_request_resolver` au panier. « je veux 4 litres » reste une quantité (jamais
l'option 4) ; « celui de 0.5 l » reste un choix de palier.

**Rôle d'un contact** : `USER` (aucune capacité) — jamais « producteur » par défaut (`profile_loader`) ; `USER` n'est pas un rôle
métier pour la détection `role_change` de `memory_update` (elle effaçait le payload et le menu à chaque tour).

**Journaux** (sans PII) : `profile_gate_paused`, `profile_gate_requirement`, `profile_gate_field_extracted`,
`profile_gate_completed`, `profile_gate_released`, `business_resume_started`, `business_resume_failed`, `neutral_greeting_guard`.
Métrique cible : `business_resume_failed` = 0.

**Accueil** : neutre (`NEUTRAL_WELCOME` : acheter / vendre / approvisionnement régulier) ; une réponse générée qui demande nom/région/
rôle/compte hors collecte de profil est remplacée par ce texte déterministe (`neutral_greeting_guard`).
