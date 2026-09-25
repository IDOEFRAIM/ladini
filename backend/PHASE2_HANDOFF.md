# PHASE 2 — HANDOFF (reprise par une autre session Claude)

> **STATUT (2026-09-25) : PHASE 2 TERMINÉE.** C1 à C14 sont commités et pushés sur
> cette branche. Le livrable final (25 points, GO/NO-GO) est
> `docs/PHASE2_HARDENING_FINAL_REPORT_2026-09-25.md` — le lire EN PREMIER avant de
> reprendre quoi que ce soit sur ce moteur (MONTHLY, Phase 5, ou tout autre
> chantier) : il contient la recommandation GO/NO-GO et les risques ouverts
> (notamment H5/H7, condition de blocage explicite pour Phase 5). Le reste de ce
> fichier est conservé tel quel comme trace historique de la reprise C6→C14 ; ne
> plus le traiter comme un plan à exécuter.

Branche: `claude/nice-mccarthy-muxhk2` (repo `idoefraim/ladini`, dossier `backend/`).
Mission complète : voir le message utilisateur original (très long, en français,
"PHASE 2: HARDENING PROFOND DU MOTEUR CONVERSATIONNEL LADINI"), qui contient toutes
les règles de méthode, décisions produit pré-validées (A-H), interdits (pas de
`if product == "coq"`, pas de nouvelle liste de mots-clés FR), et le plan de commits
A-N. Ne pas redemander ces règles à l'utilisateur — elles sont actées.

Audit Phase 1 de référence : `backend/docs/CONVERSATIONAL_ENGINE_HARDENING_AUDIT_2026-09-24.md`
(commit `cf57dcd`).

## Baseline à respecter (avant/après CHAQUE commit)

```
cd backend
GROQ_API_KEY=dummy OTEL_SDK_DISABLED=true .venv/bin/python -m pytest -q
```
Doit rester **4290+ passed, 208 skipped, 0 failed** (le total augmente au fil des
commits car on ajoute des tests permanents ; ne doit jamais baisser ni faire échouer
un test existant). Vérifier aussi :
- `.venv/bin/ruff check src tests` → clean
- `.venv/bin/mypy src 2>&1 | tail -1` → le compte d'erreurs ne doit jamais dépasser
  **552** (baseline actuelle après commit 5 ; était 561 au départ). Toujours diffé
  contre le run précédent, pas seulement le total.
- Suite PostgreSQL réelle (si la zone touchée concerne des drafts/persistance) :
  `SCHEMA_TEST_DSN="postgresql://postgres@/postgres?host=/tmp&port=55432" GROQ_API_KEY=dummy OTEL_SDK_DISABLED=true .venv/bin/python -m pytest -q tests/schema`

Règle absolue du mandat : si un commit casse un invariant ou oblige à désactiver un
test existant → **STOP**, expliquer, ne pas forcer un patch (c'est déjà arrivé une
fois avec H5, voir plus bas — résolu par un xfail documenté + revert, pas un patch).

## Commits déjà faits et PUSHÉS (ne pas retoucher, sauf bug découvert)

1. `849e791` — **C1** : harnais canonique (`tests/harness/{conversation,state,recurring}.py`)
   qui fait tourner le VRAI graphe compilé (pas de mock du spine) + characterization
   tests permanents pour les incidents A-O du mandat
   (`tests/integration/test_conversation_characterization.py`) + preuve de fidélité
   du harnais (`tests/architecture/test_conversation_harness_fidelity.py`).
2. `ce94f84` — **C2** : sémantique de suppression explicite sur les canaux `merge_dict`.
   Bug B1 trouvé (fuite du verrou de goal via `.pop()` sur un dict qui alimente un
   `merge_dict` — l'ABSENCE d'une clé dans un patch `merge_dict` veut dire "inchangé",
   pas "supprimé"). Fix : nouveau sentinel `DELETE` dans `agents/reducers.py`, corrigé
   dans 11 fichiers moteur. Garde AST permanente :
   `tests/architecture/test_merge_channel_clears_are_explicit.py` (détecte tout futur
   `.pop()`/`del` avant un patch `merge_dict`, y compris via alias/closures).
3. `21d1e1e` — **C3 (P0)** : `RecurringNeedDraft` n'avait aucune persistance durable ni
   garantie d'idempotence à l'exécution. Ajout store CAS
   (`services/database/recurring_need_draft_store.py`), registre de confirmation
   durable adossé à la ligne du draft elle-même verrouillée `FOR UPDATE`
   (`services/database/recurring_supply.py::_open_confirmation/_close_confirmation`),
   service de réconciliation (`services/reconciliation/recurring_need_reconciliation_service.py`)
   + cron (`workers/crons/recurring_need_reconciliation.py`). Prouvé contre PostgreSQL
   réel avec `asyncio.gather` concurrent (`tests/schema/test_recurring_need_confirmation_ledger.py`,
   9 tests).
4. `da44630` — **C4** : cycle de vie terminal reject/cancel/correction unifié sur les
   4 drafts. Bug B2 (REJECT était réécrit en CLARIFICATION même quand terminal) fixé.
   Nouvelle fonction `core/state.py::entities_said_this_turn()` pour distinguer
   correction (valeurs dites CE tour → jamais perdues, décision C) vs annulation vs
   correction ambiguë multi-items (→ on clarifie, jamais on devine, décision D).
5. `87510f9` — **C5** : registre déclaratif unique des 4 drafts transactionnels
   (`core/draft_registry.py` : `DRAFT_REGISTRY`, `draft_reset_patch()`), qui remplace
   des déclarations dupliquées. A révélé et corrigé un vrai P1 : `recurring_need_draft`
   manquait de DURABLE dans `core/state_profile.py` (risque de suppression silencieuse
   par le shrink du checkpointer). Décision G (TTL 24h draft abandonné) close pour le
   draft récurrent. Tests : `tests/architecture/test_draft_registry_completeness.py`.

## COMMIT 6 — EN COURS, NON COMMITÉ (à finir en premier)

Titre de tâche : "C6 canonical goal API + interruption ownership (+ is_short fix,
decide_turn shadow)".

### Partie 1 : bug B3 — FAITE, TESTÉE, PAS ENCORE COMMITÉE

Fichier : `src/ladini/graphs/agents/market_coach/interpreter/goal_planner.py`.
Le garde `is_short` (heuristique de longueur du message) ré-verrouillait l'ANCIEN
goal même quand `cognitive_guard` (seul propriétaire de la décision d'interruption)
avait déjà approuvé une interruption (`interpreted_event` réécrit en `"INTERRUPTION"`).
Un message court ("maïs") pendant un goal actif ne déclenchait alors jamais RULE 4.

Fix (déjà appliqué dans le fichier, condition modifiée) :
```python
if is_short and current_goal and event != "INTERRUPTION":
    ...
```
(avant : `if is_short and current_goal:`). Commentaire explicatif déjà en place dans
le fichier.

Tests déjà écrits et VERTS :
- `tests/architecture/test_interruption_ownership.py` — nouvelle classe
  `TestIsShortNeverOverridesAnApprovedInterruption` (3 tests : cas positif via
  confidence, contrôle négatif sans approbation, cas breakout `NAVIGATION_BREAKOUT_GOALS`).
  Vérifié : **14 passed** pour tout le fichier.
- `tests/integration/test_conversation_characterization.py` — retrait du marqueur
  `xfail(strict=True)` sur `TestL_ShortApprovedInterruption::test_mais_interrupts_and_never_mutates_the_previous_draft`
  (le test passe maintenant réellement). Vérifié : **26 passed, 8 xfailed** pour tout
  le fichier characterization (les 8 xfailed restants sont H1/H2/H5 et d'autres
  reports documentés, PAS des régressions).

Ces 3 fichiers sont modifiés dans l'arbre de travail mais **pas commités** :
- `src/ladini/graphs/agents/market_coach/interpreter/goal_planner.py`
- `tests/architecture/test_interruption_ownership.py`
- `tests/integration/test_conversation_characterization.py`

### Partie 2 : `decide_turn` en mode SHADOW — code écrit, PAS ENCORE TESTÉ/COMMITÉ

Nouveau fichier (non commité, non trackée) :
`src/ladini/graphs/agents/market_coach/core/turn_policy.py` — définit `TurnAction`
(vocabulaire canonique : NEW_TASK/CONTINUE/INTERRUPT/ANSWER_PENDING/CORRECT/CONFIRM/
REJECT/CANCEL/CLARIFY/UNKNOWN) et `classify_turn(...)`, une fonction PURE qui
n'invente RIEN : elle étiquette, dans ce vocabulaire, ce que `cognitive_decision` +
`interpreted_event` + la transition de goal ont DÉJÀ produit. C'est un choix
délibéré et documenté dans le docstring du module : le mandat interdit explicitement
de "tout basculer d'un coup" (§21-22) — donc PAS de réimplémentation indépendante et
risquée de ~400 lignes de logique répartie (interpréteur + cognitive_guard +
goal_planner), mais une fonction d'observabilité pure, non consommée aujourd'hui par
aucun routeur de production. Elle est pensée pour être branchée plus tard sur la
télémétrie de tour (commit 11, `core/turn_telemetry.py`).

Ruff déjà passé clean sur ce fichier (`ruff check --fix` a réordonné les imports).

Fichier de tests déjà écrit (non commité, non tracké) :
`tests/unit/test_turn_policy_classification.py` — couvre tous les cas canoniques
(incidents A/B/H/I du mandat, DISAMBIGUATE/CLARIFY/RECOVER/ABANDON → CLARIFY, event
manquant → CLARIFY sans exception) + **une garde architecturale cruciale** :
`test_turn_policy_is_shadow_only_not_authoritative` qui scanne tout le package par
AST et échoue si un module AUTRE que `turn_policy.py`/`turn_telemetry.py` importe
`classify_turn`/`TurnClassification` — pour qu'un futur commit qui basculerait
`decide_turn` en autoritaire le fasse *explicitement*, jamais par accident.

**CE FICHIER DE TEST N'A JAMAIS ÉTÉ EXÉCUTÉ.** C'est la toute première chose à faire
en reprenant :
```
cd backend
GROQ_API_KEY=dummy OTEL_SDK_DISABLED=true .venv/bin/python -m pytest -q \
  tests/unit/test_turn_policy_classification.py --tb=short
```
Points de vigilance probables à l'exécution :
- Vérifier que `ladini.graphs.agents.market_coach.core` est bien un package avec
  `__init__.py` importable comme fait dans le test (`from ... import core as _core_pkg`).
- Vérifier que le chemin `package_root = core_dir.parent` pointe bien sur
  `market_coach/` (contenant `core/`, `interpreter/`, `flows/`, `nodes/`, `domain/`,
  etc.) et pas plus haut/plus bas — sinon le scan AST rate des fichiers ou en scanne
  trop (peut planter sur des fichiers hors du repo si mal borné).
- Vérifier que `ConversationAction.CONTINUE_ACTIVE_GOAL` existe bien tel quel (utilisé
  dans `test_a_new_task_event_on_the_same_goal_is_a_correction`) — normalement oui
  (vu dans `core/conversation_decision.py`).

### Reste à faire pour clore le commit 6

1. Lancer et faire passer `tests/unit/test_turn_policy_classification.py`.
2. `ruff check --fix` + relecture du fichier de test si besoin.
3. Décision de découpage (PAS ENCORE PRISE) : le mandat interdit de mélanger
   plusieurs sujets indépendants dans un commit. Deux options :
   - (a) commit séparé pour le fix B3 (`is_short`) seul — c'est un fix complet et
     testable indépendamment — puis un second commit pour `turn_policy.py`
     (vocabulaire canonique + shadow) ;
   - (b) un seul commit "C6" regroupant les deux, vu qu'ils portent le même thème
     (ownership de l'interruption / décision de tour).
   Recommandation (pas imposée) : faire **(a)**, cohérent avec la discipline "un
   sujet par commit" suivie sur les 5 commits précédents.
4. **Il manque encore la 3e sous-partie de C6** annoncée dans le titre de tâche :
   une **API canonique de lecture/écriture du goal** (le mandat §18 demande : une
   fonction de lecture canonique — déjà `core/state.py::resolve_current_goal`,
   PRÉ-EXISTANTE, rien à faire là — ET une API canonique pour set/clear/transition du
   goal, remplaçant les ~30 sites qui écrivent `current_goal`/`working_memory.active_goal`
   directement, + un test/scan architectural ciblé sur les modules critiques). **Ce
   travail n'a pas commencé.** Ne PAS migrer les 30 sites d'un coup (interdiction de
   big-bang) — probablement : ajouter un helper d'écriture canonique dans
   `core/state.py` (ex. `set_current_goal(...)`, `clear_current_goal(...)`) + un test
   architectural qui vérifie que les nœuds les plus critiques (`goal_planner`,
   `cognitive.py`) l'utilisent, sans forcer une migration totale immédiate.
5. Gate complet (suite complète 4290+, ruff, mypy diff ≤552, schema si pertinent).
6. Commit(s) + push sur `claude/nice-mccarthy-muxhk2` avec message au format
   CAUSE/INVARIANT/AVANT/APRÈS/FICHIERS/TESTS/RISQUES/COMMIT (voir les 5 commits
   précédents comme modèle de style de message).

## Commits restants après C6 (ordre du mandat, à respecter sauf dépendance réelle)

- **C7** : registre/cycle de vie `PendingInteraction` (TTL 30 min configurable —
  décision F). Gap connu : `weekly_days` absent de `core/slots.py::_EXPECTED_INPUT_MAP`.
  Bug déjà identifié et documenté en xfail : **H2** (réponse à une clarification
  structurée `ambiguous_quantity` classée UNKNOWN par le chemin RECOVER de
  `cognitive_guard` au lieu d'atteindre le flow propriétaire) — probablement résolu ici.
- **C8** : normalisation unité/item canonique. Gap connu : incohérence TETE/UNITE.
  Bug xfail **H1** (résumé de confirmation générique n'affiche que le premier item,
  mauvaise unité, pas de fréquence) — partiellement ici.
- **C9** : sérialisation par conversation + idempotence transport (décision E : deux
  messages quasi-simultanés → sérialiser avec attente bornée, jamais 2 tours
  concurrents sur la même conversation). Bug connu : partage de `_session_workspaces`.
- **C10** : snapshot atomique de tour / récupération après échec partiel.
- **C11** : télémétrie structurée + parité de canal (WhatsApp/web). C'est ICI que
  `turn_policy.classify_turn` devrait enfin être branché en observation réelle
  (logué à chaque tour, toujours pas autoritaire).
- **C12-14** : matrice de tests property/state-machine, durcissement CI (pytest/OTel/
  LLM), nettoyage code mort.

Bug xfail restant et EXPLICITEMENT différé au traitement de `decide_turn` /
l'unification de l'ownership de décision (donc probablement C6-suite ou C9) :
**H5** — un fast-path déterministe de correction à valeur unique
(`interpreter/routing.py::_interpret_fast_path`) peut voler un nombre isolé d'une
NOUVELLE demande sans rapport pendant la confirmation d'un AUTRE draft. Tentative de
fix locale déjà essayée et **abandonnée** (régressait 3 tests légitimes) — voir
`git log` commit message si besoin de détails, le code actuel de `routing.py` est
resté INCHANGÉ (revert fait), avec un test `xfail(strict=True)` documentant le bug
dans le fichier characterization.

## Livrable final de Phase 2 (pas encore commencé)

Rapport en 25 points (architecture avant/après, sources de vérité supprimées,
machine à états finale, politique `decide_turn` finale, cycles de vie
goal/PendingInteraction/draft, sémantique DELETE, politique correction/reject/cancel,
stratégie idempotence, stratégie concurrence, matrice des fenêtres d'échec, unité
canonique, parité de canal, télémétrie, nouveaux tests archi/multi-tour/concurrence/
PostgreSQL, baseline finale, migrations éventuelles (aucune à ce jour), code mort
supprimé, risques ouverts restants, commits dans l'ordre, recommandation GO/NO-GO
avant reprise de MONTHLY/Phase 5). À ne produire qu'une fois C7-C14 terminés.

## Rappels de méthode (ne pas re-négocier avec l'utilisateur)

- Gel de fonctionnalités : pas de MONTHLY, pas d'acceptation/commandes récurrentes
  Phase 5, pas de nouvelle feature marketplace, pas de nouvelle capacité LLM.
- Interdiction absolue de patchs ad hoc (`if product == "coq"`, listes de mots-clés
  FR figées qui court-circuitent la classification LLM).
- Avant tout nouveau champ d'état : vérifier s'il existe déjà ailleurs, qui le
  possède, comment il est effacé, s'il est durable, comment il expire, quels tests
  garantissent son cycle de vie. Préférer supprimer une représentation concurrente
  plutôt qu'en ajouter une sixième.
- Aucune migration sans justification démontrée (additive-only, rollback, test
  PostgreSQL réel). Aucune migration créée à ce jour.
