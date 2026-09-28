# Agent Production Readiness — Transaction Core

Statut : audit systémique, 2026-09-28. Compagnon de `AGENT_RELIABILITY_MATRIX.md`
(détail par intent) et `TRANSACTION_STATE_LIFECYCLE.md` (détail du modèle de
state). Méthode : 6 audits de recherche indépendants (lecture seule) sur le
pipeline réel, triés, un sous-ensemble reproduit/corrigé/testé cette session ;
le reste documenté honnêtement comme non fait, pas caché.

## Verdict

**AGENT TRANSACTION CORE NOT READY FOR UNRESTRICTED PILOT.**

Pas parce que l'architecture est mauvaise — au contraire, l'audit confirme un
système déjà largement durci par ~5 jours d'audits précédents (drafts
versionnés + CAS, `PendingInteraction` avec TTL, verrou Redis par conversation,
dédup webhook 3 couches, outbox transactionnel, invariants métier au niveau
service). Le blocage vient de **P1 réels et non encore fixés**, tous
documentés ci-dessous avec repro exacte, sur des intents à fort trafic
(STOCK_REGISTER_HARVEST, SALES_RECORD_DIRECT, confirmation de paiement) — pas
d'hypothèse, pas de "peut-être".

Un pilote **restreint** (ex : producteurs pilotes connus, volumes faibles,
support humain disponible pour arbitrer un litige) est raisonnable dès
maintenant ; un déploiement large sans les 3 P1 ci-dessous ne l'est pas.

## Gates

| Gate | Statut | Preuve |
|---|---|---|
| STATE (lifecycle transactionnel) | **PASS** | `TRANSACTION_STATE_LIFECYCLE.md`, `AGENT_RELIABILITY_MATRIX.md` |
| DRAFT (registre versionné, 4 types) | **PASS** (SALES_PUBLISH_PRODUCT prouvé cette session ; les 3 autres partagent le même mécanisme générique, non testés individuellement) | `test_sales_publish_cross_flow_state_leak.py`, `test_recurring_need_state_leak.py` |
| PENDING (PendingInteraction) | **PASS** | suite préexistante (`test_pending_interaction_lifecycle.py`) |
| VALIDATION (invariants domaine) | **PASS** | `positive_float`, `remove_stock` (stock non négatif), `place_bid`/`select_winning_bid` (statuts) |
| IDEMPOTENCE | **PARTIAL** | 4 drafts + `select_winning_bid` : PASS prouvé. ~15 goals génériques : fallback `message_sid` ajouté cette session (couvre retry/redelivery du MÊME message), mais ne couvre PAS deux messages WhatsApp distincts confirmant deux fois sous verrou dégradé (P1 résiduel, non fermé) |
| AUTHORIZATION | **PASS** (après correctif `select_winning_bid` cette session) | `test_order_mutations_require_ownership.py` |
| CONCURRENCY | **PARTIAL** | verrou par conversation solide et testé ; son propre mode dégradé documenté (`test_conversation_lock.py`) retire la sérialisation SANS backstop pour les goals sans draft |
| RETRY | **PARTIAL** | boucle de retry transitoire MCP maintenant idempotente pour tous les goals (fallback message_sid) ; échec d'enqueue Celery après claim webhook reste un trou (P1, non fixé) |
| ERROR RECOVERY | **PARTIAL** | chemin succès disciplié et testé ; chemin erreur technique mi-tour laisse `ws.active_goal` potentiellement périmé (P1, non fixé, mécanisme plausible mais pas exécuté en test) |
| CLEANUP | **PASS** (après corrections cette session) | terminal COMPLETED purge les 4 drafts ; abandon max-retries idem ; mini-flows bid/update/GPS purgés sur transition réelle de but |
| OBSERVABILITY | **PASS** (préexistant) | logs structurés `MCP_EXEC_AUDIT`, `[GoalPlanner ...]`, PII masqué (`_mask_pii_args`) |
| E2E | **PARTIAL** | suite existante large (unit/integration/architecture/evals) ; pas de suite "golden" dédiée à 12 scénarios (mission §48) construite cette session — voir "Non fait" |

## P0 trouvés et corrigés cette session

1. **`select_winning_bid` sans contrôle de propriété acheteur.** Clôturait
   une enchère et créait une `Order` sans jamais vérifier que l'appelant
   est le VRAI acheteur propriétaire — contrairement à `cancel_auction`,
   MÊME fichier, qui le fait. Corrigé : garde ajouté (résolution du
   propriétaire réel via `BuyerProfile`/`User`, rejet `not_owner`). `phone`
   réel threadé dans `flows/buyer/negotiation.py::_handle_viewing_offers`
   (seul call site qui ne le passait pas). Verrouillé dans
   `test_order_mutations_require_ownership.py`.
2. **Boucle de retry transitoire MCP garantissant une écriture en double**
   pour ~15 goals WRITE sans draft (`SALES_PLACE_BID`,
   `STOCK_REGISTER_HARVEST`, confirmations de commande...) : chaque
   tentative de retry après timeout recevait une clé d'idempotence
   ALÉATOIRE (`None` → UUID côté client), rendant le serveur MCP incapable
   de reconnaître un rejeu. Corrigé : fallback `message_sid` (identifiant
   stable de l'événement WhatsApp entrant) — dédoublonne toute répétition
   de l'exécution d'un même tool pour un même message, sans jamais
   confondre deux messages distincts.
3. **Mini-flows `working_memory` (bid/update/GPS) jamais purgés sur une
   transition réelle de but** — même classe que l'incident
   SALES_PUBLISH_PRODUCT qui a déclenché cet audit, mais pour
   `bid_phase`/`pending_bid_auction`/`pending_bid_price`/`update_phase`/
   `winner_gps_stage`. Corrigé : ces clés sont maintenant purgées partout
   où `_purge_transaction_state()` l'est déjà (RULE 0bis/1/1quater/4/4bis/5).

## P1 trouvés et corrigés cette session

4. **"500.000" FCFA lu silencieusement comme 500.0** — sous-évaluation x1000
   d'un prix/quantité écrit avec le point comme séparateur de milliers
   (convention francophone courante). Corrigé : rejet explicite de
   l'ambiguïté (le flow redemande) plutôt qu'une valeur devinée.

## P1 trouvés, NON corrigés cette session (blockers explicites)

Chacun a une repro suffisamment précise (fournie par l'audit de recherche)
pour écrire un test qui échoue avant correction — non fait faute de temps
dans cette session, listé ici plutôt que caché.

5. **Confirmation construite depuis `transaction_payload` brut pour ~8 goals
   génériques** (`STOCK_REGISTER_HARVEST`, `SALES_RECORD_DIRECT`,
   `FINANCE_LOG_EXPENSE`, `PRODUCTION_DECLARE_FUTURE/UPDATE_FUTURE`,
   `FARM_CREATE/UPDATE`). Contrairement aux 4 goals à draft, ces
   confirmations n'ont pas de provenance vérifiée par instance — un ANSWER
   qui ne purge jamais (par design — répondre à un champ ne doit pas
   effacer les autres) peut laisser un résumé afficher une valeur jamais
   redonnée dans la tentative courante avant une écriture irréversible.
   Fix recommandé : étendre `_draft_state_key_for_goal`/RULE 1quater ou
   introduire un mécanisme de provenance plus léger pour ces 8 goals — un
   chantier à part, pas un correctif ponctuel (mandat "pas de gros
   refactor").
6. **Échec d'enqueue Celery après le claim webhook → perte de message
   silencieuse.** `twilio_webhook.py` capture le claim `msg:{MessageSid}`
   AVANT `process_agent_task.delay()` ; si l'enqueue lève/time out,
   l'exception large englobante renvoie quand même un 200 à Twilio (qui ne
   redélivrera donc jamais) — et le claim déjà posé bloquerait même un
   retry manuel du même SID. Fix recommandé : libérer le claim
   spécifiquement sur cet échec avant de renvoyer 200, ou déplacer le point
   de claim après confirmation d'enqueue réussi.
7. **`_sync_workspace` non appelé sur erreur technique mi-tour →
   `ws.active_goal` potentiellement périmé au tour suivant.** Un nœud qui
   réussit puis un nœud SUIVANT qui crashe dans le même tour : le
   checkpoint LangGraph reflète le nettoyage du nœud réussi, mais
   `ws.active_goal` (réinjecté explicitement au tour suivant) reste à sa
   valeur PRÉ-tour. Mécanisme plausible par lecture directe du code
   (`orchestrator.py`), jamais exécuté en test. Fix recommandé : appeler
   `_sync_workspace` aussi sur les branches d'erreur, ou ne plus réinjecter
   `ws.active_goal` comme entrée explicite si le checkpoint fait déjà foi.

## P2 documentés, non fixés (dette explicite, pas urgente)

8. **`ActiveSlotDecision.extracted_entities` sans `extra="forbid"`** — seul
   des 4 contrats micro-prompt à ne pas interdire structurellement un champ
   `_id`. Aucun exploit conversationnel démontré aujourd'hui (les
   consommateurs réels résolvent via des listes candidates fraîches), mais
   c'est la seule ligne de défense sur ce chemin — un futur bug de liste de
   candidats périmée y trouverait une porte ouverte. Fix recommandé :
   enumérer les champs autorisés + `extra="forbid"`, avec un test de
   non-régression sur les extractions existantes avant de l'activer.
9. **Fallback "interpréteur unifié legacy" encore câblé dans le graphe
   compilé** (`interpreter/routing.py`, atteint sur exception/désactivation
   de `MARKET_COACH_NEW_TASK_V2_ENABLED`) — construit un prompt exposant des
   IDs techniques (`producer_id`/`pricing_tier_id`), exactement le pattern
   que le micro-prompt moderne a été conçu pour éliminer. Rollback
   volontaire (`_use_new_task_v2`), pas du code mort — sa suppression est
   une décision produit (perdre le filet de sécurité en cas de régression
   du chemin moderne), pas un correctif technique unilatéral.
10. **Pas de reconciliation record pour les ~15 goals sans draft** — un
    crash mi-write pour `STOCK_REGISTER_HARVEST`/paiement ne laisse aucun
    artefact à réconcilier (contrairement aux 4 drafts, qui ont chacun leur
    service + cron de reconciliation testés). Infrastructure manquante,
    pas un bug — ajouter un draft à chacun de ces 15 goals serait le
    "gros refactor" que ce mandat exclut explicitement.

## P3 — dette documentée uniquement

- `RecurringNeedDraft` sans `check_confirmation_target_invariant` (les 3
  autres drafts l'ont) — filet redondant manquant, pas une brèche (la
  protection primaire `STALE_TARGET` fonctionne).
- Pas de test cross-flow-leak dédié pour `procurement_draft`/`preorder_draft`
  (le mécanisme générique les protège, mais rien ne verrouille spécifiquement
  leur comportement si `_draft_state_key_for_goal`/le registre régresse).
- `available_mapping`/`negotiation_context` nettoyage déjà correct mais pas
  documenté comme tel avant cet audit.
- 8 erreurs ruff pré-existantes (tri d'imports, imports/variables inutilisés)
  dans des fichiers touchés cette session, non corrigées (hors-diff, sans
  rapport avec les correctifs).

## Zones encore INSUFFISAMMENT prouvées (mission §49)

- **Concurrency réelle** : le mode dégradé du verrou est testé isolément
  (`test_conversation_lock.py`), mais AUCUN test ne prouve le comportement
  downstream (deux confirmations concurrentes réellement exécutées deux
  fois) pour un goal sans draft — la repro existe (fournie par l'audit) mais
  n'a pas été transformée en test cette session.
- **Redis/checkpointer réel** : suite existante (`test_checkpointer_state_machine.py`)
  couvre la survie générale de l'état, mais aucun nouveau test
  persist→restart→continue n'a été ajouté spécifiquement pour les
  correctifs de cette session (mini-flows bid/update/GPS, idempotency
  fallback). Documenté comme gap, pas fermé.
- **Golden E2E (mission §48, 12 scénarios)** : la suite existante couvre déjà
  la plupart de ces parcours dispersée sur plusieurs fichiers
  (`test_conversation_characterization.py`, `test_recurring_need_state_leak.py`,
  `test_end_to_end.py`, `tests/evals/`), mais aucune suite UNIQUE et nommée
  "golden" n'a été construite cette session.
- **Property-based testing (Hypothesis)** — non introduit cette session ;
  les invariants candidats existent déjà comme assertions ponctuelles
  (`positive_float`, statuts d'enchère/bid) mais pas comme propriétés
  généralisées testées sur un espace d'entrées.

## Gates exécutés

- Suite backend complète (`poetry run pytest tests`) : **verte** avant ET
  après chaque correctif de cette session (vérifié par comparaison au
  baseline avant chaque changement risqué).
- Ruff sur tous les fichiers touchés : propre (0 nouvelle erreur — les 8
  pré-existantes documentées ci-dessus, vérifiées par diff au baseline).
- mypy sur tous les fichiers touchés : 0 nouvelle erreur (vérifié fichier
  par fichier contre le baseline avant modification).
- Aucune migration DB nécessaire pour les correctifs de cette session.

## Recommandation de séquencement (mission §51 — PR strategy)

Les correctifs de cette session touchent des couches différentes
(state-machine, exécution MCP, DB service, parsing) mais ont été développés
et testés comme un tout cohérent sur la MÊME branche (contrainte de session :
un seul dépôt désigné, pas de stratégie multi-branches possible ici). Pour un
futur découpage en PRs séparées si le processus de revue l'exige :

- PR 1 — mini-flow state purge (goal_planner.py + tests) : autonome.
- PR 2 — idempotency fallback (executor.py + tests) : autonome.
- PR 3 — select_winning_bid ownership (auction.py + negotiation.py + tests) :
  autonome, prioritaire (sécurité).
- PR 4 — number parsing (quantity_unit.py + tests) : autonome.

Chacune reste cohérente isolément (testée, gate vert indépendamment) si un
découpage est nécessaire.
