# Transaction State Lifecycle — MarketCoach

Statut : audit + correctif, 2026-09-28. Périmètre : `ladini.graphs.agents.market_coach`
(LangGraph). Déclenché par un incident WhatsApp réel :

```
User: je veux vendre mes boeufs
Agent: [prêt maintenant ou plus tard ?]
User: 1
Agent: [quel produit ?]
User: ce sont des boeufs
Agent: Vente de 461 000 UNITE de boeufs à 461 000 FCFA/UNITE.
```

`quantity=461000`/`price=461000`/`unit=UNITE` n'avaient jamais été dits dans cette
conversation. Ce document explique le modèle de state existant (qui était déjà,
avant cet incident, très largement conçu pour EMPÊCHER exactement cette classe de
fuite — voir §6), où restait le trou, et ce qui a été corrigé.

## 1. Trois niveaux de mémoire (déjà en place)

Le moteur distingue déjà — pas toujours nommé ainsi, mais structurellement réel —
les trois niveaux que ce document formalise :

| Niveau | Exemples | Durée de vie | Reset |
|---|---|---|---|
| **Conversational memory** | `user_id`, `user_role`, `zone_name`, `user_name` | toute la conversation | jamais (identité) |
| **Flow state** (transaction en cours) | `current_goal`, `transaction_payload`, `pending_interaction`, `expected_candidates`, `missing_fields` | UNE tentative d'action | à la fin de CETTE tentative (§4) |
| **Business fact** (déjà commité) | ligne DB `Product`/`Order`/`Bid`/`RecurringNeed` | permanent | jamais (fait métier) |

`quantity=461000` d'une vente abandonnée n'est ni un fait métier (rien n'a été
publié) ni une mémoire conversationnelle légitime (l'utilisateur ne l'a pas
redonné) — c'est du flow state qui a survécu au-delà de son flow.

## 2. Où vit le flow state

- `transaction_payload` (`merge_dict`) — collecte BRUTE, avant qu'un draft
  versionné n'existe. Alimentée par `nodes/memory.py`/`services/domain/
  slot_enrichment.py` depuis `extracted_entities`.
- `pending_interaction` (`core/pending_interaction.py`, `replace_value`) — LA
  source canonique de "qu'attend-on ce tour ?" (`InteractionKind`). Porte un TTL
  (`PENDING_INTERACTION_TTL_SECONDS`, 30 min par défaut) : passé ce délai,
  `get_pending_interaction()` renvoie `NONE` même si la valeur persistée existe
  encore — jamais réécrite, juste ignorée en lecture.
- **Les 4 drafts versionnés** (`core/draft_registry.py`, `replace_value`,
  jamais `merge_dict`) : `sales_publish_draft`, `procurement_draft`,
  `preorder_draft`, `recurring_need_draft`. Chacun porte un `draft_id` stable —
  c'est la SEULE identité d'instance de transaction qui existe aujourd'hui dans
  ce moteur (voir §5).
- `working_memory.active_goal`/`step_index` — le verrou de tunnel
  (`core/state.py::lock_goal`/`clear_goal_lock`).

`sales_publish_draft` n'est bootstrap qu'une fois `product`+`quantity`+`price`
TOUS réunis (`nodes/confirmation_gate.py::_resolve_sales_draft_based_confirmation`
→ `SalesPublishDraft.is_complete()`). **Avant ce point, il n'existe aucune
identité d'instance** — seul `transaction_payload` porte les valeurs, à cru,
sans aucune trace de "à quelle tentative appartient ce nombre".

## 3. NEW_ACTION vs CONTINUE_CURRENT_FLOW

Trois couches décident, dans l'ordre, comment un tour est traité :

1. **`nodes/cognitive.py::cognitive_guard`** — seul propriétaire de la décision
   d'INTERRUPTION. Un `NEW_TASK` dont `detected_intent == current_goal` n'est
   **jamais** promu en INTERRUPTION (`detected_intent not in {"UNKNOWN",
   current_goal}` — la garde exclut structurellement ce cas). Ce n'est PAS un
   jugement "même transaction" — juste "le nom du but ne change pas, rien à
   arbitrer".
2. **`interpreter/goal_planner.py`** — machine à états RULE 0bis→5. RULE 5
   (`NEW_TASK` sans tunnel actif) purge **sans condition**
   (`_purge_transaction_state()`, voir §4) : deux tentatives de suite, même but,
   ne partagent jamais un slot. C'était déjà correct.
   RULE 1quater (`NEW_TASK` + tunnel encore actif) ne purgeait, elle, **jamais**
   — c'était le trou : `cognitive_guard` n'ayant jamais promu l'événement en
   INTERRUPTION (règle 1), ce tour arrivait ici et relockait le tunnel PÉRIMÉ
   tel quel.
3. **`nodes/validation.py::validator`** — filet redondant : `strip_structural`
   remet `quantity`/`unit`/`price` à `None` quand `goal_changed` (nom de but
   différent d'un tour à l'autre). Même angle mort que (2) : deux tentatives du
   MÊME but ne déclenchent jamais ce filet.

### Le correctif (RULE 1quater)

Une purge inconditionnelle ici casserait une classe de correction légitime déjà
couverte par la suite existante (`tests/integration/
test_conversation_characterization.py::TestG_CorrectionPolicy` — "non, plutôt 23
boeufs" pendant la confirmation d'un draft "14 coqs" : même but, nouveau
produit ET nouvelle quantité dans le MÊME message, doit rester une correction du
MÊME `draft_id`, jamais un nouveau départ). Il n'existe **aucun signal fiable**
au niveau du texte/de l'intention pour distinguer ce cas de la fuite réelle — les
deux sont un `NEW_TASK` même-but qui ne restate qu'une partie des champs.

Le signal qui les distingue est structurel, pas linguistique : **un draft
versionné (§2) existe-t-il déjà pour ce but ?**

- **Oui** → une instance de transaction a authentiquement déjà été ouverte
  (produit+quantité+prix réunis au moins une fois) — le tunnel encore actif lui
  appartient réellement. `NEW_TASK` même-but reste une **correction** de ce
  draft (comportement inchangé).
- **Non** → il n'y a jamais eu d'instance authentifiée, seulement des valeurs
  éparses dans `transaction_payload` — le tunnel encore actif ne prouve rien de
  plus qu'un but pas encore terminé. `NEW_TASK` même-but est alors traité
  **exactement comme RULE 5** : purge inconditionnelle, puis `memory_update`
  réapplique les entités RÉELLEMENT dites ce tour (rien de légitime n'est
  perdu — même garantie que RULE 5).

C'est exactement le cas de l'incident : la vente "boeufs" abandonnée avait
`quantity`/`price` mais jamais de `product` → jamais `is_complete()` → jamais de
`sales_publish_draft` → la nouvelle tentative purge. Voir
`interpreter/goal_planner.py::_draft_state_key_for_goal` et le commentaire de
RULE 1quater pour l'implémentation exacte.

## 4. La primitive de reset (déjà centralisée)

`interpreter/goal_planner.py::_purge_transaction_state()` — appelée par RULE
0bis (override désambiguïsation), RULE 1 (REJECT hors confirmation), RULE 1quater
(depuis ce correctif, conditionnée par §3), RULE 4 (INTERRUPTION), RULE 4bis
(RESUME), RULE 5 (NEW_TASK). Efface : `transaction_payload`, `draft_payload`,
`stable_entities`, `missing_fields`/`completed_fields`/`last_missing_field`,
`expected_candidates`, `available_mapping`, `confirmation_summary`,
`selected_tool`/`selected_tool_args`, `execution_result`, `retry_count`,
`vendor_selection_context`/`tier_selection_context`, `negotiation_context`,
`preorder_workflow`, `active_form`/`form_step`/`form_data`, **les 4 drafts du
registre** (`core/draft_registry.py::draft_reset_patch()`), et
`pending_interaction`. Conserve : identité (`user_id`, `user_role`, `zone_*`),
`current_goal`/`goal_stack` (gérés séparément, propriété du Goal Planner).

Un nouveau draft transactionnel s'ajoute en UN seul endroit
(`core/draft_registry.py::DRAFT_REGISTRY`) — `_purge_transaction_state`,
`core/state_profile.py` (durabilité checkpoint) et le store Postgres en
dérivent tous, vérifié par `tests/architecture/
test_draft_registry_completeness.py`.

## 5. Provenance des slots — ce qui existe, ce qui n'existe pas

Il n'y a **pas** de provenance par-champ (`flow_id` attaché à chaque valeur de
`transaction_payload`) — ni avant cet audit, ni après. Le registre de drafts
(§2) fournit une provenance **par instance** (le `draft_id`), suffisante pour
trancher §3, mais `transaction_payload` reste un sac de valeurs plates sans
identité tant qu'aucun draft n'existe. C'est une limite connue, documentée ici
plutôt que masquée : voir §8 "Risques résiduels".

## 6. Ce qui fonctionnait déjà (ne pas re-corriger)

Avant cet incident, le moteur avait déjà une histoire d'audits sur EXACTEMENT
cette classe de bug (`RECURRING_NEED_DRAFT` manquant du registre,
`sales_publish_draft` non déclaré dans le state — voir les commentaires 2026-09-08
et 2026-09-09 dans `core/state.py`/`core/draft_registry.py`) :

- **Intent switch** (but différent) — RULE 4 (`INTERRUPTION`) purge déjà
  inconditionnellement, y compris les 4 drafts.
- **Deux tentatives de suite, même but, sans tunnel actif** — RULE 5 purge déjà
  inconditionnellement (commentaire "Purge inconditionnelle... impossible qu'un
  slot mort survive à une nouvelle tâche").
- **Abandon explicite** ("annule"/"laisse tomber") — RULE 1 (`REJECT` hors
  confirmation) purge déjà.
- **`PendingInteraction` expirée (TTL)** — `is_pending_expired` la neutralise en
  LECTURE dès que `now - created_at > TTL`, sans jamais réécrire la valeur
  persistée (garantie "une interaction résolue ne redevient jamais active",
  `tests/architecture/test_pending_interaction_lifecycle.py`).
- **Confirmation gate** — `check_invariants()` vérifie déjà qu'un
  `CONFIRM_ACTION` a un `confirmation_summary`/`transaction_payload` cohérent ;
  `confirmation_summary_payload` invalide tout résumé dont le payload a changé.

Le SEUL trou identifié était RULE 1quater (§3) et deux oublis symétriques (§7).

## 7. Complétion et abandon — oublis symétriques corrigés

- **Fin de flow réussie** (`nodes/cleaner.py::state_cleaner_node`, gate
  `status == "COMPLETED"`) : purgeait déjà `transaction_payload`/
  `stable_entities`/`form_data`/etc., mais **jamais** les 4 drafts du registre.
  Un draft `PUBLISHED`/`EXECUTED` restait posé dans l'état ; la prochaine
  tentative du MÊME but retombait sur `confirmation_gate.py`, qui réutilise
  inconditionnellement tout draft déjà présent. Corrigé : `draft_reset_patch()`
  ajouté, **restreint à `COMPLETED`** — jamais `FAILED`/`ERROR`, où le draft
  DB peut être toujours `DRAFT` (retriable) malgré un nœud qui a levé une
  exception APRÈS le `_persist` domaine mais AVANT le retour LangGraph (fenêtre
  documentée et testée par `tests/integration/
  test_conversation_characterization.py::TestS_MidTurnCrashAfterDomainPersist`)
  — le purger là aurait orphelinné un draft encore bien vivant.
- **Abandon pour max retries** (`core/conversation_reset.py::
  reset_abandoned_conversation_context`, appelé par `cognitive_guard`) : même
  oubli, même correctif (`draft_reset_patch()` ajouté sans restriction — cet
  abandon-ci est toujours définitif, jamais un crash mi-tour).

## 8. Risques résiduels (honnêtes, pas de faux "tout est réglé")

- **Pas de provenance par champ.** Si un utilisateur, DANS le tunnel d'un draft
  déjà ouvert (§3 branche "Oui"), déclare un produit sans rapport avec le draft
  en cours (mais toujours le même but), le moteur le traitera comme une
  correction du draft existant plutôt que comme une nouvelle instance — cette
  ambiguïté existait avant cet audit (voir `core/turn_policy.py::
  decide_active_draft_reply`, qui ne l'arbitre que pour `CREATE_RECURRING_NEED`)
  et n'a pas été résolue ici : la résoudre correctement demanderait de comparer
  le produit nommé au produit déjà connu du draft, un chantier à part (mission
  §9/§10 "slot provenance"), volontairement hors périmètre pour rester un
  correctif ciblé plutôt qu'un big-bang.
- **`transaction_payload` avant bootstrap reste un sac de valeurs plates.** La
  purge conditionnelle (§3) suffit à couper la fuite observée, mais un flow qui
  atteindrait un état intermédiaire différent (ex. `product` seul rempli, sans
  `quantity`/`price`) avant d'être abandonné n'est couvert que si le message
  suivant est bien classé `NEW_TASK` — un `ANSWER`/`UPDATE` sur un but déjà
  verrouillé (RULE 1bis) ne purge jamais, par design (répondre à un champ ne
  doit pas effacer les autres). Si l'interpréteur mésclasse un vrai nouveau
  départ en `ANSWER` (ambiguïté documentée dans le commentaire de RULE 1bis),
  ce correctif ne le rattrape pas.
- **Pas de test Redis/checkpointer réel dédié à ce correctif précis.** La suite
  `tests/integration/test_checkpointer_state_machine.py` couvre déjà la survie
  générale de l'état au checkpoint ; aucun nouveau test n'y a été ajouté
  spécifiquement pour RULE 1quater — le comportement testé (`tests/interpreter/
  test_goal_planner_state_machine.py::TestRule1quaterPurgesStaleTransactionState`,
  `tests/integration/test_sales_publish_cross_flow_state_leak.py`) passe par les
  reducers réels mais pas par un aller-retour JSON de checkpoint réel.

## 9. Tests

- `tests/interpreter/test_goal_planner_state_machine.py::
  TestRule1quaterPurgesStaleTransactionState` — le mécanisme RULE 1quater en
  isolation (3 cas : purge sans draft, pas de purge avec draft déjà ouvert,
  pas de régression hors tunnel).
- `tests/integration/test_sales_publish_cross_flow_state_leak.py` — chaîne
  réelle `input_interpreter → cognitive_guard → goal_planner → memory_update →
  validator`, PRODUCER, reproduit l'incident exact (échoue avant le correctif
  avec `quantity=461000.0`/`price=461000.0` survivants, passe après) + le
  garde-fou symétrique (correction d'un draft déjà ouvert préservée).
- Suite existante inchangée : `tests/integration/
  test_conversation_characterization.py` (corrections/interruptions),
  `tests/integration/test_recurring_need_state_leak.py` (même classe de bug,
  autre but) — zéro régression après le correctif (voir §3 pour pourquoi une
  purge inconditionnelle aurait cassé plusieurs de ces tests).
