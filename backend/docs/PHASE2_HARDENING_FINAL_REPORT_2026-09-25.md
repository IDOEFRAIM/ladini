# PHASE 2 — HARDENING PROFOND DU MOTEUR CONVERSATIONNEL LADINI — RAPPORT FINAL

Branche `claude/nice-mccarthy-muxhk2` (repo `idoefraim/ladini`, dossier `backend/`).
Commits C1 → C14, exécutés un par un, gate complet après chacun (suite complète,
tests d'architecture, Ruff, diff mypy). Aucun big-bang, aucune migration, MONTHLY et
Phase 5 (recurring/orders) non touchés — conformément au mandat.

Audit de référence Phase 1 : `docs/CONVERSATIONAL_ENGINE_HARDENING_AUDIT_2026-09-24.md`
(commit `cf57dcd`). Notes de continuité inter-session : `PHASE2_HANDOFF.md` (statut
mis à jour en tête de ce document : Phase 2 **TERMINÉE**, ce rapport en est le
livrable final).

---

## 1. Architecture avant/après

**Avant (audit Phase 1)** : logique conversationnelle dispersée — goal/pending/draft
lus et écrits par des chemins ad hoc à chaque flow, résumé de confirmation dupliqué
par domaine, aucune trace exploitable d'un tour une fois le nettoyage de fin de tour
passé, suite de tests nécessitant deux variables d'environnement manuelles.

**Après (C14)** : un noyau `core/` (`state.py`, `pending_interaction.py`,
`draft_registry.py`, `turn_policy.py`, `turn_trace.py`) qui centralise CHAQUE
question répétée ("quel est le goal courant ?", "qu'attend-on comme réponse ?",
"quel draft est actif ?", "comment classifier ce tour à des fins d'observation ?",
"qu'est-il arrivé pendant ce tour, avant que le nettoyage l'efface ?"). Les flows
(`flows/buyer/*`, `flows/producer/*`) consomment ce noyau, ne réimplémentent plus
leur propre lecture d'état. La suite de tests tourne sans variable d'environnement
manuelle (`pytest -q` nu).

## 2. Sources de vérité supprimées / centralisées

| Avant (dispersé) | Après (source unique) | Commit |
|---|---|---|
| `state.get("current_goal") or working_memory.get("active_goal")` recopié dans 6+ fichiers | `core/state.py::resolve_current_goal` | C6 |
| Verrouillage/déverrouillage du goal ad hoc par flow | `core/state.py::lock_goal`/`clear_goal_lock` (API canonique d'écriture) | C6 |
| `expected_input` (chaîne libre) lu indépendamment par routage/guard/flows | `core/pending_interaction.py::PendingInteraction`/`get_pending_interaction` (résolveur unique, TTL inclus) | C7 |
| 4 drafts transactionnels déclarés indépendamment (reset, `owning_goal`) | `core/draft_registry.py::DRAFT_REGISTRY` | C5 |
| `TETE` parfois confondu avec `UNITE`, canonicalisation absente sur les items additionnels | `utils.py::canonical_unit_label` appliqué uniformément (item principal ET additionnels) | C8 |
| Télémétrie de tour lue APRÈS `post_response_cleanup` (champs déjà à `None`) | `core/turn_trace.py` (capture AVANT nettoyage), complémentaire — pas un remplacement — de `core/turn_telemetry.py` | C11 |
| `GROQ_API_KEY`/`OTEL_SDK_DISABLED` à poser manuellement pour `pytest -q` | Repli test (`tests/conftest.py`) + arrêt explicite des providers OTel de test | C13 |

Rien n'a été dupliqué à nouveau : chaque nouvelle capacité (TurnTrace, matrice de
transitions) réutilise ces sources plutôt que d'en recalculer une variante.

## 3. Machine à états finale (vue d'ensemble)

Un tour traverse, dans l'ordre : `input_normalizer` → `security_moderation` →
`session_bootstrap` (si ALLOW) → interpréteur (micro-prompts NEW_TASK/ACTIVE_SLOT/
STRUCTURED_ACTION selon `expected_input`) → `cognitive_guard` (SEUL propriétaire de
la décision de transition : `CONTINUE_ACTIVE_GOAL` / `INTERRUPT_ACTIVE_GOAL` /
`START_OR_PLAN_GOAL` / `DISAMBIGUATE` / `CLARIFY` / `recover_active_tunnel` /
`abandon_tunnel_max_retries`) → routeur de domaine (`core/router.py`) → flow
métier → `memory`/persistance → `post_response_cleanup` (capture TurnTrace juste
avant, C11) → réponse.

Vérifié par introspection du graphe COMPILÉ (pas la source) :
`tests/architecture/test_entry_block_topology.py`,
`test_cognitive_decisions_are_consumed_or_removed.py`,
`test_catalog_and_graph.py`.

## 4. `decide_turn`/politique de classification de tour

`core/turn_policy.py::classify_turn` (C6) offre un vocabulaire canonique cible
(`NEW_TASK`/`CONTINUE`/`INTERRUPT`/`ANSWER_PENDING`/`CORRECT`/`CONFIRM`/`REJECT`/
`CANCEL`/`CLARIFY`/`UNKNOWN`) — **strictement SHADOW du début à la fin de Phase
2** : branché sur CHAQUE tour réel via `core/turn_trace.py` (C11, peuple
`TurnTrace.turn_decision`), jamais consommé par un routeur/flow. Garde
architecturale permanente (`tests/unit/test_turn_policy_classification.py`, AST
scan de tout le paquet `market_coach`) : seuls `turn_policy.py` lui-même et
`turn_trace.py` ont le droit de l'importer. Décision explicite du mandat (§21-22) :
le passage en autoritaire est un changement structurel volontairement hors
périmètre de C1-C14, à faire "seulement lorsque la matrice est solide" — c'est
exactement la condition de fermeture documentée pour H5 et H7 (point 23).

## 5. Cycle de vie du goal

`current_goal` : posé par `cognitive_guard`/le flow métier au démarrage d'un
tunnel, lu partout via `resolve_current_goal` (repli sur
`working_memory.active_goal` tant qu'un tunnel/une confirmation est active),
remis à `None` par `post_response_cleanup` SAUF si `_keep_goal_channel` (tunnel/
confirmation encore active) — documenté précisément dans la docstring de
`resolve_current_goal` et vérifié par `tests/integration/test_turn_trace.py::
TestB_GoalBeforeAndAfter` (goal_before/goal_after capturés AVANT ce nettoyage,
donc jamais confondus avec l'état déjà nettoyé). Écriture canonique via
`lock_goal`/`clear_goal_lock` (C6) — plus de mutation directe dispersée.

## 6. Cycle de vie de `PendingInteraction`

Registre `InteractionKind` (11 valeurs) + `PendingInteraction` (dataclass gelée) +
résolveur unique `get_pending_interaction` (C7), priorité : tunnel panier dérivé à
neuf (jamais périmable) > état persisté explicite (soumis au TTL) > `NONE`. TTL 30
min par défaut (`settings.PENDING_INTERACTION_TTL_SECONDS`, décision produit F),
frontière stricte `> ttl` documentée et verrouillée par
`tests/integration/test_state_machine_transition_matrix.py::
TestProperty_PendingInteractionTTLBoundary` (8 valeurs limites). Un pont de
compatibilité `legacy_confirmation_bridge` (antérieur à Phase 2, lié à un
déploiement historique de `pending_interaction` lui-même) reste en place —
volontairement NON touché, voir point 22.

## 7. Cycles de vie des 4 drafts transactionnels

`RecurringNeedDraft`/`ProcurementDraft`/`PreorderDraft`/`SalesPublishDraft` :
mêmes champs de nom (`draft_id`, `version`, `status`), déclarés dans
`DRAFT_REGISTRY` avec leur `owning_goal` (C5, `test_draft_registry_completeness.py`
: exactement un draft actif par goal). `RecurringNeedDraft` est le flow de
RÉFÉRENCE (seul à avoir une doublure MCP dans le harnais de test, voir point 18) :
persistance CAS PostgreSQL (`recurring_need_draft_store.py`, C3), verrou de ligne
`FOR UPDATE` à la confirmation, réconciliation 24h pour tout draft resté DRAFT trop
longtemps. Terminal (`EXECUTED`/`CANCELLED`) ne redevient jamais actif — prouvé par
caractérisation (`TestK_NewRequestAfterCompleted`, `TestJ_RepeatedConfirmation`)
et par la garde de suppression explicite `DELETE` (point 8).

## 8. Sémantique `DELETE` (C2)

Un patch `merge_dict` qui ne mentionne PAS une clé la laisse INCHANGÉE (jamais
"effacée implicitement") — bug B1 (fuite de verrou de goal via `.pop()`) fixé en
introduisant un sentinel `DELETE` explicite (`agents/reducers.py`), corrigé dans 11
fichiers. Garde architecturale permanente
`tests/architecture/test_merge_channel_clears_are_explicit.py` (AST, détecte tout
futur `.pop()`/`del` avant un patch `merge_dict`, y compris via alias/closures).

## 9. Politique correction / reject / cancel

Décision déterministe sur la STRUCTURE du message (`flows/buyer/recurring_need.py::
_is_correction`, même politique répliquée par domaine) : REJECT porteur de valeurs
("non, plutôt 23 bœufs") → correction ; intention reformulée SANS sa propre
fréquence → correction du draft en cours ; sinon → nouvelle demande autonome (clôt
durablement l'ancien draft, jamais orphelin en DRAFT — sauf H6, point 23). Un refus
("non"/"laisse tomber") sur un draft terminal ne le fait jamais ressusciter
(`TestN_RejectionIsTerminal`, C4). Limite connue et documentée : H5/H7 (point 23) —
cette même heuristique structurelle (absence de `recurrence_type`) peut absorber à
tort un message SANS RAPPORT comme correction plutôt que comme nouvelle demande.

## 10. Stratégie d'idempotence

Trois couches distinctes, jamais confondues : (a) idempotence TRANSPORT — un
`message_sid`/retry Celery identique ne repaie jamais un appel LLM déjà réussi
(cache par clé, C9) ; (b) idempotence de CONFIRMATION — même draft/version ne peut
pas s'exécuter deux fois (verrou CAS PostgreSQL, C3) ; (c) idempotence de MESSAGE —
un message WhatsApp rejoué (retry infrastructure) est traité une seule fois
(`TestJ_RepeatedConfirmation::test_the_same_whatsapp_message_replayed_is_
processed_once`). Les 3 couches sont prouvées séparément — jamais supposées
équivalentes.

## 11. Stratégie de concurrence

`core/conversation_lock.py::conversation_turn_lock` (C9, décision E) sérialise TOUT
le tour — de la résolution du Workspace à sa persistance finale — par conversation,
avec attente bornée (`_AGENT_TIMEOUT_SECONDS`). Deux messages quasi simultanés sur
le MÊME numéro ne peuvent plus produire de lost update. Prouvé contre le VRAI
verrou (pas un mock) avec de vrais intervalles `time.monotonic()`
(`TestO_ConcurrentMessages::test_two_simultaneous_messages_never_run_
concurrently`).

## 12. Matrice des fenêtres d'échec

C10 caractérise et BORNE (jamais n'élimine) la fenêtre où la persistance métier
(commit PostgreSQL) réussit mais la persistance du Workspace (checkpoint
LangGraph) échoue avant d'être flushée. Preuves : `test_a_lost_turn_state_after_
commit_never_executes_twice` (jamais de double exécution, projection périmée
auto-corrigée au tour suivant) et `test_a_lost_mcp_response_after_commit_is_
resolved_without_duplicate`. Risque RÉSIDUEL explicitement non éliminé — voir
point 23.

## 13. Unité canonique (C8)

`utils.py::canonical_unit_label` — un seul mapping (`_CANONICAL_UNIT_MAP`),
appliqué à l'item PRINCIPAL et aux items ADDITIONNELS (bug P1 fermé : `TETE`
confondu avec `UNITE`/graphie non canonicalisée sur les items additionnels).
Idempotence prouvée sur 17 valeurs (limites + bruit unicode) par
`test_state_machine_transition_matrix.py::TestProperty_
CanonicalUnitLabelIsIdempotent` (C12).

## 14. Parité de canal (WebChat / WhatsApp)

`Orchestrator.handle()` porte désormais un paramètre `channel` explicite
(`WHATSAPP` par défaut, `WEBCHAT` posé par `api/routes/webchat.py`) — C11.
Différences ACCEPTÉES : canal, métadonnées de transport, rendu final. Différences
NON acceptées, prouvées identiques sur les deux canaux : intent, `turn_decision`,
`goal_after`, statut/version de draft, effets de bord métier
(`test_turn_trace.py::TestF_WebChatWhatsAppParity`, cycle COMPLET
création→confirmation→exécution vérifié par
`test_state_machine_transition_matrix.py::TestWebChatFullLifecycle`, C12).

## 15. Télémétrie (C11)

`core/turn_trace.py::TurnTrace` — instantané immuable capturé à l'entrée de
`post_response_cleanup` (AVANT son propre reset), complémentaire de
`core/turn_telemetry.py` (métriques perf SQL/Redis/LLM persistées en base),
jamais un remplacement. Contrat minimal : turn_id/conversation_id (haché)/
message_id/channel/intent/confidence/goal avant-après/`turn_decision` (shadow)/
pending avant-après/draft (type/id/version/statut)/item_count/flow/outcome/
duration_ms/error_class. Confidentialité par défaut vérifiée
(`TestH_NoRawUserMessageByDefault`, `TestI_ConversationIdentifierIsOpaque`) :
jamais le message brut, jamais le numéro en clair. `error_class` couvre à la fois
les exceptions qui échappent à `Orchestrator.handle()` ET celles qu'un nœud avale
en interne (`_safe_node`, nouveau champ d'état `error_class` déclaré pour
survivre au checkpoint).

## 16. Nouveaux tests d'architecture

`test_turn_policy_classification.py` (garde shadow-only, C6/C11),
`test_merge_channel_clears_are_explicit.py` (C2), `test_draft_registry_
completeness.py` (C5), `test_pending_field_registry_completeness.py`,
`test_entry_block_topology.py`/`test_cognitive_decisions_are_consumed_or_
removed.py` (topologie du graphe compilé — désormais sans dépendance à
`GROQ_API_KEY` ambiant, C13).

## 17. Nouveaux tests multi-tours

`tests/integration/test_conversation_characterization.py` (~50 tests, incidents
réels A-S nommés par le mandat, C1-C10) + `tests/integration/
test_state_machine_transition_matrix.py` (C12, matrice de transitions
raisonnée : confiance faible, extraction malformée, cycle WebChat complet,
propriétés sur fonctions pures) + `tests/integration/test_turn_trace.py` (C11, 16
tests A-I du mandat télémétrie).

## 18. Nouveaux tests de concurrence/retry

`TestO_ConcurrentMessages` (verrou réel, intervalles `time.monotonic()`, C9),
`TestJ_RepeatedConfirmation` (retry transport + confirmation dupliquée, C3/C9),
`test_recurring_need_execution_idempotency.py` (fenêtres A/B/C de C10, verrou CAS
PostgreSQL réel).

## 19. Tests PostgreSQL

`tests/schema/test_recurring_need_confirmation_ledger.py` (9 tests, `asyncio.
gather` concurrent contre PostgreSQL réel, C3) — seul flow (`recurring_need`) à
disposer d'une doublure MCP complète dans le harnais (`tests/harness/
recurring.py`), donc seul flow couvert par la matrice C12 (voir point 22 pour la
limite assumée).

## 20. Baseline finale

`pytest -q` (SANS AUCUNE variable d'environnement, C13) : **exit 0**, 4548
passed, 156 skipped, **6 xfail strict** (4 causes racines distinctes — H1×3, H5,
H6, H7, voir point 23), 0 failed. Ruff (`select = E,F,I,B`) : clean. mypy :
**552 erreurs** (561 au départ de Phase 2, jamais remonté à aucun commit — diffé
systématiquement, pas seulement le total).

## 21. Migrations

**Aucune** créée sur toute la Phase 2 (C1-C14) — conforme au réflexe "pas de
migration par défaut" du mandat. Aucun commit n'a rencontré de cas où une
migration devenait indispensable ; si un futur commit en juge une nécessaire, la
règle reste : STOP, justifier explicitement AVANT de l'introduire.

## 22. Code mort supprimé

Recherche ciblée (grep + preuve d'appelants, jamais une suppression sur
impression) : un seul élément prouvé mort — `core/turn_trace.py::current_builder()`
(introspection de test introduite par C11, jamais appelée par aucun test, aucun
appelant de production) — supprimé en C14 avec son entrée `__all__`.

Deux candidats examinés et EXPLICITEMENT écartés (preuve à l'appui, pas une
suppression réflexe) :
- `core/pending_interaction.py::resolve_pending_interaction()` vs
  `clear_pending_interaction()` : corps identique (`{"pending_interaction":
  None}`) mais **7 call sites de production actifs** pour le premier, sémantique
  distincte documentée (RESOLVED vs CLEARED, invariant 5 de la docstring de
  module) — pas un doublon, deux intentions actuellement identiques mais
  susceptibles de diverger.
- `core/pending_interaction.py::legacy_confirmation_bridge()` : pont de
  compatibilité antérieur à Phase 2 (déploiement historique de
  `pending_interaction`), dont la suppression exige de prouver qu'aucune ligne
  PostgreSQL pré-déploiement ne subsiste — hors de portée de ce qui est
  vérifiable depuis le code seul. Non touché, non renommé, non déplacé.

Le reste du paquet `market_coach` core/ s'est révélé déjà propre (pas de mapping
d'unité dupliqué, pas de second résolveur de goal/pending concurrent) —
conséquence directe de la discipline "un seul point de vérité" déjà appliquée par
C2-C11, pas un oubli de cette recherche.

## 23. Risques ouverts (non résolus, non maquillés)

- **C10 — fenêtre mi-tour (split-brain borné, PAS éliminé)** : un commit
  PostgreSQL métier réussi suivi d'un crash AVANT que le Workspace ne flushe son
  checkpoint peut laisser la PROJECTION LangGraph périmée un tour de plus.
  Fonctionnellement sûr (jamais de double exécution, prouvé) — mais l'utilisateur
  peut voir un état "encore en DRAFT" pendant un tour, potentiellement retaper sa
  confirmation. Aucune garantie SUPPLÉMENTAIRE n'a été ajoutée par C11-C14 sur ce
  point précis : ne pas présenter ce risque comme résolu.
- **H1 (3 tests xfail)** : le récapitulatif générique de confirmation
  (`confirmation_gate.py`) n'affiche ni la fréquence, ni tous les items d'un
  draft multi-produits, ni le message de vérification spécifique après une
  réponse MCP perdue — `RecurringNeedDraft.render_summary()` connaît ces
  informations, le builder générique ne les consulte pas. Cosmétique/UX, jamais
  une pierre d'achoppement de correction métier.
- **H5 (1 test xfail, réévalué au commit 12, INCHANGÉ)** : `interpreter/
  routing.py::_interpret_fast_path::_confirmation_correction` peut voler un
  nombre isolé d'une nouvelle demande sans rapport pendant la confirmation d'un
  AUTRE draft. Condition de fermeture inchangée : `decide_turn`/`classify_turn`
  devient autoritaire (post-Phase 2), ou un signal générique "ce produit est
  étranger au draft courant" apparaît sans lexique figé.
- **H6 (1 test xfail)** : `goal_planner` purge inconditionnellement
  `recurring_need_draft` sur un NEW_TASK sans `PendingInteraction` active, avant
  que `_create_flow` ait pu persister l'annulation de l'ancien draft — orphelin
  en base jusqu'à la réconciliation 24h (jamais perdu, jamais dupliqué, juste pas
  annulé IMMÉDIATEMENT comme partout ailleurs).
- **H7 (1 test xfail, DÉCOUVERT au commit 12, même famille que H5)** :
  `flows/buyer/recurring_need.py::_is_correction` absorbe un message à confiance
  trop faible pour interrompre (< `INTERRUPTION_CONFIDENCE_THRESHOLD`=0.60) comme
  correction du draft actif, même quand le produit nommé n'a aucun rapport —
  écrase silencieusement le draft en attente. Même condition de fermeture que
  H5 : les deux sites (`_confirmation_correction` et `_is_correction`) devront
  être corrigés ENSEMBLE le jour où `decide_turn` devient autoritaire.
- **`legacy_confirmation_bridge`** (point 22) : dette pré-Phase 2 non résolue,
  non aggravée, non dissimulée.

Aucun de ces 6 risques n'a été découvert puis caché : chacun est un test
`xfail(strict=True)` PERMANENT — le moindre passage inattendu (XPASS) casse la
CI, forçant explicitement une décision (retirer le marqueur) plutôt qu'un oubli
silencieux.

## 24. Commits, dans l'ordre

| # | SHA | Titre |
|---|---|---|
| C1 | `849e791` | Harnais canonique + caractérisation des incidents A-O |
| C2 | `ce94f84` | Sémantique de suppression explicite (`DELETE`), bug B1 |
| C3 | `21d1e1e` | `RecurringNeedDraft` durable + idempotence PostgreSQL (P0) |
| C4 | `da44630` | Cycle de vie terminal reject/cancel/correction |
| C5 | `87510f9` | Registre déclaratif des 4 drafts transactionnels |
| C6 | `605dbc7`, `4d21d98`, `cc6114b` | `is_short` fix, `classify_turn` shadow, API canonique du goal |
| C7 | `ac09d96` | Registre/cycle de vie `PendingInteraction`, TTL, H2 |
| C8 | `7eaf288` | Normalisation canonique des unités (TETE ≠ UNITE) |
| C9 | `faec4e6` | Sérialisation par conversation (verrou distribué borné) |
| C10 | `382c503` | Snapshot atomique de tour, fenêtres d'échec caractérisées |
| C11 | `7d8fb3f` | `TurnTrace` + capture avant nettoyage + parité de canal |
| C12 | `59fd2e7` | Matrice de régression état-machine/propriétés, H7 découvert |
| C13 | `d3ce063` | Durcissement environnement de test (sans variable manuelle) |
| C14 | *(ce commit)* | Nettoyage ciblé + rapport final |

`b4379d3` (notes de continuité inter-session) précède C7 chronologiquement mais
n'introduit aucun changement de comportement — documentaire uniquement.

## 25. Recommandation GO/NO-GO

### GO — reprise de MONTHLY

**GO conditionnel.** Les classes de bugs historiques listées par le mandat sont
couvertes : état périmé (goal/pending capturés avant/après, C6-C7, C11), goal
périmé (`resolve_current_goal` centralisé, jamais de resurrection prouvée par
`TestN_RejectionIsTerminal`), pending périmé (TTL strict, C7), item perdu
(`item_count` tracé par TurnTrace, additional_items canonicalisés, C8),
mauvaise unité (C8, idempotence prouvée), duplication de retry (3 couches
d'idempotence distinctes et prouvées séparément, point 10), ambiguïté de
timeout (fenêtres A/B/C caractérisées, C10), tours concurrents (verrou réel
prouvé, C9), persistance partielle de tour (C10, RISQUE RÉSIDUEL borné pas
éliminé — voir point 23), résurrection terminale (jamais observée, C4/C6),
désaccord de routage (`cognitive_guard` seul propriétaire, topologie du graphe
compilé vérifiée, C6). Condition : MONTHLY doit être développé en respectant les
mêmes principes établis ici — jamais un chemin de lecture d'état parallèle à
`resolve_current_goal`/`get_pending_interaction`/`DRAFT_REGISTRY`, jamais un
patch ad hoc de mots-clés.

### NO-GO conditionnel — Phase 5 (acceptation/commandes récurrentes)

**NO-GO tant que H5/H7 ne sont pas fermés.** Les deux racines communes
(`_confirmation_correction`, `_is_correction`) touchent EXACTEMENT le mécanisme
qu'une acceptation/commande récurrente automatisée devrait exploiter le plus
souvent (confirmation d'un draft en cours pendant qu'un événement externe —
notification d'acceptation, rappel de commande — arrive). Étendre Phase 5
AVANT de fermer H5/H7 revient à construire sur un mécanisme de correction connu
pour absorber à tort un message sans rapport. Fermeture recommandée AVANT
extension : rendre `decide_turn`/`classify_turn` autoritaire (déjà instrumenté en
SHADOW depuis C6, déjà observé sur 100% des tours depuis C11 — la donnée
nécessaire pour juger si la bascule est sûre existe déjà, jamais collectée avant
Phase 2). Le risque C10 (point 23) est acceptable en l'état pour un flux déjà en
production (recurring_need) mais mérite d'être RÉÉVALUÉ explicitement pour tout
nouveau flux Phase 5 avant qu'il n'accumule le même volume de trafic.
