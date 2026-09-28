"""`goal_planner` — machine à états PURE du cycle de vie des intentions.

Couverture exhaustive de CHAQUE règle (0bis à 5) et de leurs branches. C'est
le nœud qui décide, à chaque tour, si on reste dans le tunnel actuel ou si on
en change — la logique la plus centrale du graphe. 10% de couverture avant ce
fichier ; l'objectif est 100% des branches de décision.
"""
from __future__ import annotations

import pytest

# Importer `routing` déclenche `_init_intent_to_goal_map(...)` au chargement du
# module — sans ça, `INTENT_TO_GOAL_MAP` (consommé par goal_planner) est vide.
import ladini.graphs.agents.market_coach.interpreter.routing  # noqa: F401
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter.goal_planner import goal_planner
from tests.conftest import make_state, run
from tests.harness.state import clears

# La traduction `expected_input="PRODUCT"/"CONFIRMATION"/...` → écriture RÉELLE
# de `pending_interaction` vit maintenant dans `make_state` elle-même (source
# commune à toute la suite, tests/conftest.py) — plus de copie locale ici.


def gp(**overrides):
    return run(goal_planner(make_state(**overrides), None))


# =====================================================================
# RÈGLE 0bis — RÉSOLUTION DE DÉSAMBIGUÏSATION
# =====================================================================

class TestRule0bisDisambiguation:
    """(2026-09-09, audit Bloc 2, fermeture Blocker B) : le déclencheur de
    cette règle n'est plus `current_goal == "DISAMBIGUATION_PENDING"` — c'est
    désormais EXCLUSIVEMENT `pending_interaction.kind == SELECTION_MENU and
    .context_ref == "intent_disambiguation"` (voir
    `core/pending_interaction.py::DISAMBIGUATION_MENU_GOAL_SHIM`). Ces tests
    posent donc `pending_interaction` directement au lieu du raccourci
    `expected_input="SELECTION"` (qui ne poserait pas `context_ref`).
    `current_goal`, lui, porte maintenant le VRAI business goal antérieur (ou
    `None`) — jamais plus un pseudo-goal en entrée."""

    @staticmethod
    def _disambiguation_pending_interaction(**overrides) -> dict:
        base = {
            "kind": "SELECTION_MENU",
            "goal": None,
            "field": None,
            "context_ref": "intent_disambiguation",
            "candidates": [],
            "created_at": 0.0,
            "status": "ACTIVE",
            "target": None,
        }
        base.update(overrides)
        return base

    def test_confident_new_task_overrides_the_pending_menu(self):
        """Une intention confiante en cours de désambiguïsation prend le dessus
        SANS attendre la sélection du menu."""
        r = gp(
            current_goal=None,
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "ACTIVE"
        assert r["transaction_payload"] == {"__reset__": True}, "purge transactionnelle attendue"

    def test_interruption_overriding_menu_marks_interruption_detected(self):
        r = gp(
            current_goal=None,
            interpreted_event="INTERRUPTION",
            detected_intent="BUYER_REQUEST",
            pending_interaction=self._disambiguation_pending_interaction(),
            user_role="BUYER",
        )
        assert r["current_goal"] == "BUYER_REQUEST"
        assert r["interruption_detected"] is True

    def test_previous_real_business_goal_survives_while_menu_is_pending(self):
        """(Blocker B) Si la désambiguïsation a interrompu un tunnel réel
        (`current_goal` porte encore ce goal, PAS un pseudo-goal), une
        intention confiante concurrente doit quand même pouvoir le
        remplacer — la présence d'un vrai goal antérieur ne doit jamais
        bloquer la résolution du menu."""
        r = gp(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "BUYER_REQUEST"

    def test_selection_by_index_resolves_to_mapped_goal(self):
        r = gp(
            current_goal=None,
            interpreted_event="SELECTION",
            extracted_entities={"selection_index": 1},
            available_mapping={"1": "SALES_PUBLISH_PRODUCT", "2": "PRODUCTION_DECLARE_FUTURE"},
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert get_pending_interaction(r).kind == InteractionKind.NONE

    def test_selection_by_text_value_resolves(self):
        """`selected_value` est cherché comme CLÉ de la mappe (pas comme
        valeur) — ex: le texte que l'utilisateur a tapé/cliqué."""
        r = gp(
            current_goal=None,
            interpreted_event="SELECTION",
            extracted_entities={"selected_value": "vente"},
            available_mapping={"vente": "SALES_PUBLISH_PRODUCT"},
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_invalid_selection_index_keeps_menu_active(self):
        """Un index hors de la mappe ne fait PAS s'effondrer le tunnel — le
        menu reste affiché pour une nouvelle tentative. `current_goal` en
        sortie porte le shim de compatibilité `validator` (jamais lu pour le
        contrôle de flux — voir `pending_interaction` ci-dessous, qui est le
        SEUL signal qui redéclenchera cette règle au tour suivant)."""
        r = gp(
            current_goal=None,
            interpreted_event="SELECTION",
            extracted_entities={"selection_index": 99},
            available_mapping={"1": "SALES_PUBLISH_PRODUCT"},
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "DISAMBIGUATION_PENDING"
        assert get_pending_interaction(r).kind == InteractionKind.SELECTION_MENU
        assert get_pending_interaction(r).context_ref == "intent_disambiguation"
        assert r["response_strategy"] == "SELECTION_MENU"

    def test_no_selection_yet_keeps_menu_active(self):
        r = gp(
            current_goal=None,
            interpreted_event="UNKNOWN",
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "DISAMBIGUATION_PENDING"
        assert get_pending_interaction(r).context_ref == "intent_disambiguation"

    def test_stale_mapping_is_rebuilt_from_trigger_id(self):
        """Régression : un `available_mapping` périmé (d'un AUTRE menu affiché
        entre-temps) doit être écarté au profit du catalogue reconstruit
        depuis `disambiguation_trigger_id`."""
        from ladini.graphs.agents.market_coach.interpreter.intent import (
            INTENT_DISAMBIGUATION,
        )
        trigger_id = next(iter(INTENT_DISAMBIGUATION))
        entry = INTENT_DISAMBIGUATION[trigger_id]
        first_candidate = entry["options"][0][0]

        r = gp(
            current_goal=None,
            interpreted_event="SELECTION",
            extracted_entities={"selection_index": 1},
            available_mapping={"1": "UN_GOAL_PERIME_SANS_RAPPORT"},
            working_memory={"disambiguation_trigger_id": trigger_id},
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == first_candidate

    def test_pending_interaction_is_the_sole_cross_turn_signal_no_working_memory_flag_needed(self):
        """(Blocker B) Avant ce correctif, `working_memory.disambiguation_pending`
        devait reconstituer le pseudo-goal car `current_goal` seul ne
        survivait pas de façon fiable. `pending_interaction` étant DURABLE
        nativement (voir nodes/cleanup.py::keep_selection_channel), la
        résolution doit fonctionner SANS ce flag — `current_goal=None` et
        `working_memory={}` (aucune trace du flag legacy)."""
        r = gp(
            current_goal=None,
            interpreted_event="SELECTION",
            extracted_entities={"selection_index": 1},
            available_mapping={"1": "SALES_PUBLISH_PRODUCT"},
            working_memory={},
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_no_explicit_selection_falls_back_to_a_confidently_detected_intent(self):
        """Si l'utilisateur répond au menu par une phrase que le LLM classe
        directement dans une intention connue (plutôt qu'un index/valeur de
        sélection structurée), cette intention prime sur le ré-affichage du
        menu — évite de forcer un utilisateur déjà clair à choisir un numéro."""
        r = gp(
            current_goal=None,
            interpreted_event="ANSWER",
            detected_intent="SALES_PUBLISH_PRODUCT",
            extracted_entities={},
            available_mapping={"1": "PRODUCTION_DECLARE_FUTURE"},
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_dict_form_disambiguation_options_are_parsed(self, monkeypatch):
        """`INTENT_DISAMBIGUATION` options supportent aussi la forme dict
        (`{"intent": ...}`), en plus du tuple/liste utilisé par le catalogue
        actuel — chemin défensif à couvrir explicitement."""
        import ladini.graphs.agents.market_coach.interpreter.goal_planner as gp_module

        monkeypatch.setitem(
            gp_module.INTENT_DISAMBIGUATION,
            "TEST_TRIGGER_DICT_FORM",
            {"options": [{"intent": "SALES_PUBLISH_PRODUCT"}, {"intent": "PRODUCTION_DECLARE_FUTURE"}]},
        )
        r = gp(
            current_goal=None,
            interpreted_event="SELECTION",
            extracted_entities={"selection_index": 1},
            available_mapping={},
            working_memory={"disambiguation_trigger_id": "TEST_TRIGGER_DICT_FORM"},
            pending_interaction=self._disambiguation_pending_interaction(),
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_a_cart_selection_menu_never_triggers_disambiguation_resolution(self):
        """Garde-fou de non-confusion : un AUTRE menu `SELECTION_MENU`
        (ex: sélection panier générique, `context_ref` différent) ne doit
        jamais être traité comme une désambiguïsation d'intention, même si
        `current_goal` est vide et qu'un `available_mapping` existe."""
        r = gp(
            current_goal=None,
            interpreted_event="SELECTION",
            extracted_entities={"selection_index": 1},
            available_mapping={"1": "SALES_PUBLISH_PRODUCT"},
            pending_interaction=self._disambiguation_pending_interaction(
                context_ref="some_other_menu"
            ),
        )
        assert r["current_goal"] != "SALES_PUBLISH_PRODUCT"


# =====================================================================
# RÈGLE 1 — REJECT
# =====================================================================

class TestRule1Reject:
    def test_reject_outside_confirmation_resets_everything(self):
        r = gp(
            interpreted_event="REJECT",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] is None
        assert r["goal_status"] == "IDLE"
        assert r["response_strategy"] == "CLARIFICATION"
        assert clears(r["working_memory"], "active_goal")

    def test_reject_during_confirmation_is_left_to_confirmation_gate(self):
        """`confirmation_gate` gère le VRAI refus pendant CONFIRMATION — le
        planner ne doit pas court-circuiter en annulant le goal lui-même."""
        r = gp(
            interpreted_event="REJECT",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "ACTIVE"


# =====================================================================
# RÈGLE 1bis — VERROUILLAGE (événements de slot-filling)
# =====================================================================

class TestRule1bisTunnelLocking:
    @pytest.mark.parametrize("event", ["CONFIRM", "SELECTION", "ANSWER", "UPDATE"])
    def test_slot_events_always_relock_current_goal(self, event):
        r = gp(
            interpreted_event=event,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "ACTIVE"
        assert r["working_memory"]["active_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_an_answer_inside_a_tunnel_never_purges_the_payload(self):
        """Non-régression du garde ajouté le 2026-09-08 : une réponse DANS un
        tunnel doit continuer à passer par la RÈGLE 1bis (verrouillage), et
        surtout JAMAIS retomber en RÈGLE 5, qui purge tout l'état
        transactionnel — ce serait catastrophique en plein slot-filling."""
        r = gp(
            interpreted_event="ANSWER",
            detected_intent="STOCK_REGISTER_HARVEST",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            transaction_payload={"product": "poulets", "quantity": 4000},
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert "transaction_payload" not in r, (
            "RÈGLE 1bis ne doit RIEN purger — un ANSWER en tunnel n'est pas "
            "une nouvelle tâche"
        )


class TestGoalLessSlotEventsArePromotedNotDropped:
    """Incident réel (2026-09-08, log de production) :

        User  : « j'ai 6000 poulets »
        Agent : « Je n'ai pas bien saisi. »

    Le LLM avait pourtant correctement classé
    `intent=STOCK_REGISTER_HARVEST` — mais avec `event=ANSWER` et
    `current_goal=None`. La RÈGLE 1bis (« maintien du tunnel ») s'appliquait
    au seul vu de l'événement, sans vérifier qu'un tunnel existe : elle
    réaffectait `current_goal = None` et jetait l'intention. Une minute plus
    tard, « je veux vendre mes poulets » — même situation, aucun tunnel,
    intention sûre — fonctionnait, uniquement parce que le LLM avait
    étiqueté `NEW_TASK`. Faire dépendre l'accès à toute la machine à états
    d'un label que le LLM ne peut pas trancher de façon fiable (une phrase
    qui ÉNONCE une donnée ressemble légitimement à une réponse de slot) est
    structurellement fragile."""

    @pytest.mark.parametrize("event", ["ANSWER", "UPDATE"])
    def test_a_goal_less_slot_event_with_a_real_intent_becomes_a_new_task(self, event):
        r = gp(
            interpreted_event=event,
            detected_intent="STOCK_REGISTER_HARVEST",
            current_goal=None,
            expected_input="NONE",
        )
        assert r["current_goal"] == "STOCK_REGISTER_HARVEST", (
            "hors tunnel, une intention détectée avec certitude ne doit "
            "JAMAIS être jetée — c'est le seul contenu exploitable du tour"
        )
        assert r["goal_status"] == "ACTIVE"
        assert r["working_memory"]["active_goal"] == "STOCK_REGISTER_HARVEST"

    @pytest.mark.parametrize("event", ["CONFIRM", "SELECTION"])
    def test_confirm_and_selection_without_a_tunnel_never_invent_a_goal(self, event):
        """« oui » ou « 2 » sans rien à confirmer ni menu affiché ne portent
        aucune intention métier — les promouvoir inventerait une tâche que
        l'utilisateur n'a pas demandée."""
        r = gp(
            interpreted_event=event,
            detected_intent="STOCK_REGISTER_HARVEST",
            current_goal=None,
            expected_input="NONE",
        )
        assert r["current_goal"] is None
        assert r["response_strategy"] == "CLARIFICATION"

    def test_a_goal_less_answer_with_no_mappable_intent_is_unchanged(self):
        """Repli par défaut préservé : sans intention exploitable, il n'y a
        rien à promouvoir — le tour reste une clarification, comme avant."""
        r = gp(
            interpreted_event="ANSWER",
            detected_intent="UNKNOWN",
            current_goal=None,
            expected_input="NONE",
        )
        assert r["current_goal"] is None
        assert r["response_strategy"] == "CLARIFICATION"


class TestOrphanCartConfirmPromotionIsScopedToBuyers:
    """Incident réel #7 (2026-09-14, WhatsApp production, utilisateur
    double-rôle) : cette promotion (panier ACHETEUR non vide + `CONFIRM`
    orphelin → `BUYER_PREORDER_CONFIRM`) ne regardait jamais le rôle résolu
    de CE tour, seulement la présence d'un panier — y compris un vieux
    SNAPSHOT (`working_memory.last_active_cart`) periné de plusieurs jours.
    Un producteur (aucun tunnel actif, "confirmer" classé
    `event=CONFIRM`/`intent=UNKNOWN` faute d'intention reconnue) avec un tel
    snapshot se retrouvait promu vers la confirmation de CE panier étranger
    à sa demande.

    Incident réel #11 (correction du correctif #7, même jour) : la première
    version de ce garde bloquait TOUT rôle non-BUYER sans distinguer le
    panier ACTUEL (`state["active_cart"]`, rempli CE MÊME tour) du vieux
    snapshot — un producteur en train d'ACHETER ("je veux 9" → panier →
    "okay" quelques secondes plus tard, panier fraîchement rempli) se
    retrouvait à tort bloqué en clarification. Seul le repli sur le SNAPSHOT
    reste restreint au rôle BUYER ; un panier ACTUEL non vide reste toujours
    un signal légitime, quel que soit le rôle (un dual-rôle peut acheter ET
    vendre)."""

    def test_a_producer_role_orphan_confirm_never_resurrects_a_stale_snapshot_cart(self):
        """Panier PÉRIMÉ (snapshot seulement, `active_cart` vide CE tour)."""
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="UNKNOWN",
            current_goal=None,
            expected_input="NONE",
            active_cart=[],
            working_memory={"last_active_cart": [{"product": "riz", "quantity": 50}]},
            user_role="PRODUCER",
        )
        assert r["current_goal"] is None
        assert r["response_strategy"] == "CLARIFICATION"

    def test_a_buyer_role_orphan_confirm_still_resumes_a_stale_snapshot_cart(self):
        """Non-régression : le cas d'origine (2026-09-09) reste couvert
        quand le rôle résolu de ce tour est bien BUYER."""
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="UNKNOWN",
            current_goal=None,
            expected_input="NONE",
            active_cart=[],
            working_memory={"last_active_cart": [{"product": "riz", "quantity": 50}]},
            user_role="BUYER",
        )
        assert r["current_goal"] == "BUYER_PREORDER_CONFIRM"

    def test_a_producer_role_orphan_confirm_still_resumes_a_fresh_cart(self):
        """Incident #11 : un panier ACTUEL (rempli CE MÊME tour, PAS un
        snapshot périmé) reste un signal légitime même sous rôle PRODUCER —
        un dual-rôle achetant activement ne doit jamais être bloqué."""
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="UNKNOWN",
            current_goal=None,
            expected_input="NONE",
            active_cart=[{"product": "boeufs", "quantity": 9}],
            user_role="PRODUCER",
        )
        assert r["current_goal"] == "BUYER_PREORDER_CONFIRM"


class TestConfirmDispositionCanCarryARealBreakoutIntent:
    """Incident réel (2026-09-14, WhatsApp production) : après qu'une
    interruption ait déjà été confirmée par la route SELECTION ("confirmer"
    sans rapport avec le menu affiché), le classifieur de repli (legacy,
    moins fiable que le micro-prompt NEW_TASK) retourne parfois
    `disposition=CONFIRM` au lieu de `NEW_TASK` — MAIS avec le bon intent
    identifié (`PRODUCER_CONFIRM_ORDER`), pas un intent inventé. Rejeter ce
    résultat (RÈGLE 5 excluait `CONFIRM` sans distinction) renvoyait
    l'utilisateur au tunnel périmé en boucle. Restreint aux intents déjà
    vetés pour l'interruption (`NAVIGATION_BREAKOUT_GOALS`) — jamais un
    intent arbitraire promu sur un simple "oui"."""

    def test_confirm_with_a_distinct_breakout_intent_is_promoted(self):
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="PRODUCER_CONFIRM_ORDER",
            current_goal="BUYER_LIST_ORDERS",
            expected_input="SELECTION",
            normalized_text="confirmer",
        )
        assert r["current_goal"] == "PRODUCER_CONFIRM_ORDER"
        assert r["goal_status"] == "ACTIVE"

    def test_confirm_echoing_the_current_goal_itself_is_never_promoted(self):
        """Le piège exact de l'incident précédent (contradiction
        SELECTION/NEW_TASK) : `detected_intent == current_goal` ne doit
        JAMAIS être traité comme une bascule — ce n'est pas un intent
        distinct, c'est le tunnel actuel réémis tel quel."""
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="BUYER_LIST_ORDERS",
            current_goal="BUYER_LIST_ORDERS",
            expected_input="SELECTION",
            normalized_text="confirmer",
        )
        assert r["current_goal"] == "BUYER_LIST_ORDERS"

    def test_confirm_with_a_non_breakout_intent_is_still_never_promoted(self):
        """Le cas général (RÈGLE 5 historique) reste inchangé : un intent
        qui n'est pas dans la liste vetée pour l'interruption ne doit
        toujours pas être promu sur un simple CONFIRM."""
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="STOCK_REGISTER_HARVEST",
            current_goal="BUYER_LIST_ORDERS",
            expected_input="SELECTION",
            normalized_text="confirmer",
        )
        assert r["current_goal"] == "BUYER_LIST_ORDERS"


# =====================================================================
# RÈGLE 1ter — PERSISTANCE PAR DÉFAUT (UNKNOWN)
# =====================================================================

class TestRule1terDefaultPersistence:
    @pytest.mark.parametrize("event", ["UNKNOWN", "NEW_TASK"])
    def test_unknown_intent_persists_the_active_goal(self, event):
        r = gp(
            interpreted_event=event,
            detected_intent="UNKNOWN",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="NONE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["detected_intent"] == "SALES_PUBLISH_PRODUCT"


# =====================================================================
# RÈGLE 1quater — NEW_TASK PENDANT LE SLOT-FILLING (délègue à TunnelManager)
# =====================================================================

class TestRule1quaterNewTaskDuringSlot:
    """(2026-09-09, audit Bloc 2) : `cognitive_guard` (Bloc 1, gelé) est
    l'UNIQUE autorité qui décide si une intention concurrente est assez
    confiante pour interrompre — voir nodes/cognitive.py,
    `_DISAMBIGUATION_CONFIDENCE_THRESHOLD` (0.85). Il ne promeut
    `interpreted_event` en `"INTERRUPTION"` QUE s'il approuve. Si goal_planner
    reçoit encore `"NEW_TASK"` ici, c'est que `cognitive_guard` a DÉJÀ refusé
    — aucune confiance, même 1.0, ne doit pouvoir renverser ce refus (c'était
    le bug : un second seuil, plus bas, ici même, contredisait le premier).
    Seule une intention de « breakout » critique (liste fermée, indépendante
    de la confiance — voir core/goals.py::NAVIGATION_BREAKOUT_GOALS) garde le
    droit de casser le tunnel sans repasser par `cognitive_guard`."""

    def test_high_confidence_new_task_on_soft_slot_still_stays_locked(self):
        """Anti-régression du bug trouvé à l'audit : ce scénario est
        INATTEIGNABLE en production (confiance 0.9 >= 0.85 aurait déjà fait
        promouvoir l'événement en "INTERRUPTION" par cognitive_guard) — mais
        si goal_planner le reçoit quand même tel quel (ex. appel direct,
        test, régression de la promotion), il ne doit PAS improviser sa
        propre décision d'interruption à partir de la confiance seule."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.9,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",       # slot SOFT
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "WAITING_INPUT"

    def test_low_confidence_new_task_stays_locked(self):
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.10,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "WAITING_INPUT"

    def test_new_task_on_hard_slot_never_switches_regardless_of_confidence(self):
        """CONFIRMATION est un slot dur : NEW_TASK ne le casse plus jamais
        (seule une INTERRUPTION déjà approuvée par cognitive_guard, ou une
        intention de breakout critique, le pourrait — voir les classes
        dédiées)."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.99,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_a_breakout_intent_alone_no_longer_switches_the_goal_here(self):
        """(2026-09-09, Bloc 2 passe finale — Invariant A) : contrat CHANGE.
        Le planner ne decide plus JAMAIS d'interrompre : atteindre cette
        regle avec `event == "NEW_TASK"` prouve que `cognitive_guard` a
        refuse l'interruption (sinon l'evenement serait "INTERRUPTION").
        Une intention de breakout presentee directement au planner, sans
        decision amont, ne casse donc plus rien — comportement VOULU, pas
        une perte : le chemin reel (cognitive_guard -> goal_planner) est
        prouve de bout en bout dans
        `tests/architecture/test_interruption_ownership.py`."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_VIEW_CART",
            interpreter_confidence=0.0,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert "suspended_goal" not in r


class TestRule1quaterPurgesStaleTransactionState:
    """(2026-09-28, incident réel — contamination cross-flow
    SALES_PUBLISH_PRODUCT) : "Vente de 461 000 UNITE de boeufs à 461 000
    FCFA/UNITE" — une vente antérieure abandonnée avait déjà rempli
    `quantity`/`price` (jamais donnés dans CETTE conversation), et cette
    RÈGLE relockait le tunnel SANS purger, laissant les deux nombres
    survivre jusqu'à la confirmation d'une vente totalement différente
    (« mes boeufs »). `cognitive_guard` refuse TOUJOURS l'INTERRUPTION
    quand `detected_intent == current_goal` (nodes/cognitive.py) — le nom du
    but ne suffit pas à prouver que c'est la MÊME transaction. Cette RÈGLE
    doit désormais purger exactement comme la RÈGLE 5 (NEW_TASK sans tunnel
    actif) : `memory_update` réapplique ensuite les entités RÉELLEMENT
    dites ce tour, rien de légitime n'est perdu."""

    def test_same_goal_new_task_purges_stale_quantity_and_price(self):
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            normalized_text="je veux vendre mes boeufs",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            transaction_payload={"quantity": 461000, "price": 461000, "unit": "UNITE"},
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "WAITING_INPUT"
        assert r["transaction_payload"] == {"__reset__": True}, (
            "les quantity/price d'une tentative abandonnée doivent être "
            f"purgés, pas relockés tels quels : {r.get('transaction_payload')!r}"
        )

    def test_an_already_bootstrapped_draft_is_never_purged_here_a_correction_instead(self):
        """Garde-fou symétrique, ESSENTIEL : cette RÈGLE ne doit purger QUE
        quand aucun draft versionné n'existe encore pour le but courant —
        dès qu'un draft existe (`core/draft_registry.py`), un NEW_TASK
        même-but qui ne restate qu'UNE partie des champs (ex: nouveau
        produit, ancien prix tu) doit rester une CORRECTION du même draft,
        jamais un nouveau départ. Preuve exacte : `tests/integration/
        test_conversation_characterization.py::TestG_CorrectionPolicy::
        test_a_reformulation_during_confirmation_is_a_correction_never_a_
        cancellation` ("non, plutôt 23 boeufs" pendant la confirmation d'un
        draft "14 coqs") régressait — même draft_id attendu, version +1 —
        avant l'ajout de ce garde-fou."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            sales_publish_draft={
                "draft_id": "already-open-instance",
                "product": "maïs",
                "quantity": 300,
                "price": 5000,
                "unit": "SAC",
            },
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert "sales_publish_draft" not in r, (
            "un draft déjà ouvert pour CE but est la preuve d'une instance "
            f"active — il ne doit jamais être purgé ici : {r!r}"
        )
        assert "transaction_payload" not in r or r["transaction_payload"] != {
            "__reset__": True
        }

    def test_purge_does_not_wipe_pending_interaction_of_an_unrelated_goal(self):
        """Garde-fou : ce correctif ne doit purger QUE quand RULE 1quater
        elle-même s'applique (tunnel actif pour le but COURANT) — un but
        différent, non concerné par cette règle, ne doit rien perdre."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            normalized_text="je veux acheter des oignons",
            current_goal="BUYER_REQUEST",
            expected_input="NONE",
            transaction_payload={"product": "riz", "quantity": 50},
            working_memory={"active_goal": "BUYER_REQUEST"},
        )
        # Sans tunnel actif (`expected_input=="NONE"`), RULE 1quater ne
        # s'applique pas du tout — c'est la RÈGLE 5 (déjà purgeante sans
        # condition) qui traite ce cas, comportement inchangé par ce
        # correctif.
        assert r["transaction_payload"] == {"__reset__": True}


# =====================================================================
# RÈGLE « is_short » — bruit court en plein tunnel
# =====================================================================

class TestShortNoisePersistence:
    def test_very_short_reply_persists_the_goal(self):
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="UNKNOWN",
            normalized_text="ok",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_short_reply_with_a_known_but_unrelated_intent_still_persists(self):
        """Branche dédiée `is_short` (distincte de la RÈGLE 1ter) : même si le
        LLM a mal classé un « oui » isolé comme une intention connue, un
        texte trop court pour être fiable ne doit jamais faire dérailler un
        tunnel actif — `expected_input` fixé donc pas de RÈGLE 1quater non
        plus (`expected_input=NONE` ici)."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            normalized_text="oui",
            current_goal="BUYER_REQUEST",
            expected_input="NONE",
            working_memory={"active_goal": "BUYER_REQUEST"},
        )
        assert r["current_goal"] == "BUYER_REQUEST"
        assert r["detected_intent"] == "BUYER_REQUEST"
        assert r["goal_status"] == "ACTIVE"


# =====================================================================
# RÈGLE 2 — POLLUTION PENDANT LE TUNNEL
# =====================================================================

class TestRule2PollutionDuringTunnel:
    def test_unknown_event_in_tunnel_reasks_the_same_slot(self):
        r = gp(
            interpreted_event="UNKNOWN",
            # `detected_intent` volontairement NON "UNKNOWN" : sinon la RÈGLE
            # 1ter (persistance par défaut) capte l'événement avant la RÈGLE 2.
            detected_intent="UNRELATED_GARBLED_INTENT",
            normalized_text="une phrase incomprehensible et longue",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "WAITING_INPUT"


# =====================================================================
# RÈGLE 3 — OUT_OF_SCOPE
# =====================================================================

class TestRule3OutOfScope:
    def test_out_of_scope_with_active_goal_stays_and_clarifies(self):
        r = gp(
            interpreted_event="OUT_OF_SCOPE",
            normalized_text="quel temps fait-il aujourd'hui",
            current_goal="SALES_PUBLISH_PRODUCT",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["response_strategy"] == "CLARIFICATION"

    def test_out_of_scope_without_active_goal_is_idle(self):
        r = gp(interpreted_event="OUT_OF_SCOPE", current_goal=None)
        assert r["goal_status"] == "IDLE"


# =====================================================================
# RÈGLE 4 — INTERRUPTION
# =====================================================================

class TestRule4Interruption:
    def test_confident_interruption_switches_goal_and_purges(self):
        r = gp(
            interpreted_event="INTERRUPTION",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.9,
            normalized_text="en fait je veux acheter des tomates plutot",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "BUYER_REQUEST"
        assert r["interruption_detected"] is True
        assert r["suspended_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_interruption_with_no_active_tunnel_switches_freely(self):
        """Rien à protéger : une « interruption » de rien = une nouvelle tâche."""
        r = gp(
            interpreted_event="INTERRUPTION",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.5,
            current_goal=None,
            expected_input="NONE",
        )
        assert r["current_goal"] == "BUYER_REQUEST"

    def test_interruption_to_the_same_goal_purges_stale_payload(self):
        """Régression : « je veux des poussins » pendant que « poulets » était
        déjà en confirmation — même goal, mais le payload périmé doit sauter."""
        r = gp(
            interpreted_event="INTERRUPTION",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.9,
            normalized_text="finalement je veux plutot des poussins",
            current_goal="BUYER_REQUEST",
            expected_input="CONFIRMATION",
            transaction_payload={"product": "poulets"},
            working_memory={"active_goal": "BUYER_REQUEST"},
        )
        assert r["current_goal"] == "BUYER_REQUEST"
        assert r["transaction_payload"] == {"__reset__": True}
        assert r["interruption_detected"] is True

    def test_low_confidence_interruption_is_absorbed_as_clarification(self):
        r = gp(
            interpreted_event="INTERRUPTION",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.05,
            normalized_text="hmm je sais pas trop en fait peut etre",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["response_strategy"] == "CLARIFICATION"


# =====================================================================
# RÈGLE 4bis — RESUME
# =====================================================================

class TestRule4bisResume:
    def test_resume_pops_the_goal_stack_and_restores_payload(self):
        r = gp(
            interpreted_event="RESUME",
            normalized_text="reprenons ce que je faisais avant",
            goal_stack=["SALES_PUBLISH_PRODUCT"],
            suspended_payload={"product": "maïs", "quantity": 50},
            current_goal="BUYER_REQUEST",
            working_memory={"active_goal": "BUYER_REQUEST"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_stack"] == []
        assert r["transaction_payload"]["product"] == "maïs"
        assert r["transaction_payload"]["quantity"] == 50

    def test_resume_with_empty_stack_is_a_clarification(self):
        r = gp(
            interpreted_event="RESUME",
            normalized_text="reprenons ce que je faisais avant",
            goal_stack=[],
            current_goal="BUYER_REQUEST",
        )
        assert r["response_strategy"] == "CLARIFICATION"
        assert r["current_goal"] == "BUYER_REQUEST"


# =====================================================================
# RÈGLE 5 — NEW_TASK (instanciation propre)
# =====================================================================

class TestRule5NewTask:
    def test_new_task_with_known_intent_starts_clean(self):
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            current_goal=None,
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "ACTIVE"
        assert r["transaction_payload"] == {"__reset__": True}

    def test_two_consecutive_new_tasks_of_the_same_goal_both_purge(self):
        """Régression : deux demandes d'achat à la suite (même goal) doivent
        CHACUNE purger — sinon un slot périmé (ex: quantité) fuite vers la
        nouvelle demande."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            normalized_text="je veux aussi acheter des oignons",
            current_goal="BUYER_REQUEST",
            transaction_payload={"product": "tomates", "quantity": 234},
            working_memory={"active_goal": "BUYER_REQUEST"},
        )
        assert r["transaction_payload"] == {"__reset__": True}

    def test_new_task_with_unmapped_intent_falls_back_to_clarification(self):
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="INTENT_INEXISTANT",
            current_goal=None,
        )
        assert r["response_strategy"] == "CLARIFICATION"

    def test_purge_clears_stale_finalized_transactional_drafts(self):
        """Bug réel en production (2026-09-09) : un `SALES_PUBLISH_PRODUCT`
        déjà PUBLISHED pour un produit A restait dans `sales_publish_draft`
        (jamais purgé nulle part) — une nouvelle demande de publication pour
        un produit B, sans rapport, réutilisait ce draft déjà finalisé
        (`confirmation_gate.py` recharge inconditionnellement tout draft
        présent) et se heurtait à "Cette publication est déjà publiée —
        rien à modifier ici." au lieu de démarrer une publication propre.
        Même schéma partagé par `procurement_draft`/`preorder_draft`."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            current_goal=None,
            sales_publish_draft={"draft_id": "stale-a", "status": "PUBLISHED"},
            procurement_draft={"draft_id": "stale-b", "status": "EXECUTED"},
            preorder_draft={"draft_id": "stale-c", "status": "EXECUTED"},
        )
        assert r["sales_publish_draft"] is None
        assert r["procurement_draft"] is None
        assert r["preorder_draft"] is None


# =====================================================================
# RÈGLE 4ter — CONFIRM ORPHELIN SUR UN PANIER NON VIDE
# =====================================================================

class TestRule4terOrphanCartConfirm:
    """Incident réel (2026-09-09) : après l'ajout au panier, les nœuds de
    nettoyage remettent `current_goal` à None ; « je valide » (CONFIRM /
    intent UNKNOWN) retombait sur le menu générique alors que le panier
    invitait explicitement à précommander."""

    def test_orphan_confirm_with_active_cart_promotes_to_preorder_confirm(self):
        # (2026-09-11) Promu vers BUYER_PREORDER_CONFIRM, pas ..._INIT — voir
        # interpreter/goal_planner.py RÈGLE 4ter : un CONFIRM orphelin porte
        # une vraie intention de confirmation, pas juste une ouverture de
        # tunnel. Avec ..._INIT, `create_preorder` s'arrêtait TOUJOURS sur un
        # second "Confirmez-vous ?" redondant après bootstrap du draft —
        # l'utilisateur devait confirmer deux fois la même décision (bug réel
        # signalé, corrigé en changeant le goal promu ici).
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="UNKNOWN",
            current_goal=None,
            expected_input="SELECTION",
            active_cart=[{"product_id": "P1", "name": "lait", "quantity": 3}],
            user_role="BUYER",
        )
        assert r["current_goal"] == "BUYER_PREORDER_CONFIRM"
        assert r["goal_status"] == "ACTIVE"
        assert str(r.get("status") or "").upper() != "WAITING_INPUT"

    def test_orphan_confirm_recovers_cart_from_working_memory_snapshot(self):
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="UNKNOWN",
            current_goal=None,
            expected_input="NONE",
            working_memory={"last_active_cart": [{"product_id": "P1", "name": "lait"}]},
            user_role="BUYER",
        )
        assert r["current_goal"] == "BUYER_PREORDER_CONFIRM"

    def test_orphan_confirm_with_empty_cart_does_not_promote(self):
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="UNKNOWN",
            current_goal=None,
            expected_input="SELECTION",
            active_cart=[],
        )
        assert r.get("current_goal") != "BUYER_PREORDER_INIT"

    def test_confirm_while_a_specific_field_is_pending_is_untouched(self):
        """Un CONFIRM pendant qu'un champ précis (ex: CONFIRMATION d'un
        autre tunnel) est attendu ne doit pas être détourné vers la
        précommande."""
        r = gp(
            interpreted_event="CONFIRM",
            detected_intent="UNKNOWN",
            current_goal=None,
            expected_input="CONFIRMATION",
            active_cart=[{"product_id": "P1", "name": "lait"}],
        )
        assert r.get("current_goal") != "BUYER_PREORDER_INIT"


# =====================================================================
# REPLI PAR DÉFAUT
# =====================================================================

class TestDefaultFallback:
    def test_no_matching_rule_falls_back_to_clarification(self):
        r = gp(interpreted_event="ONBOARDING_INPUT", current_goal=None)
        assert r["response_strategy"] == "CLARIFICATION"
        assert r["goal_status"] == "IDLE"

    def test_no_matching_rule_with_an_active_goal_persists_it(self):
        r = gp(
            interpreted_event="ONBOARDING_INPUT",
            detected_intent="UNRELATED_GARBLED_INTENT",
            normalized_text="une phrase suffisamment longue et non ambigue",
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="NONE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["goal_status"] == "ACTIVE"
        assert r["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert r["response_strategy"] == "CLARIFICATION"


# =====================================================================
# EXTRACTION DE TEXTE — repli sur `messages` quand normalized_text est vide
# =====================================================================

class TestTextFallbackFromMessages:
    def test_falls_back_to_last_user_message_when_normalized_text_is_empty(self):
        """Régression potentielle : si `normalized_text`/`user_query` sont
        vides mais que `messages` contient l'historique, le texte du DERNIER
        message `user` doit être utilisé pour dériver `is_short` — sinon un
        texte réellement long serait pris pour un texte court (chaîne vide)."""
        r = gp(
            interpreted_event="ONBOARDING_INPUT",
            detected_intent="UNRELATED_GARBLED_INTENT",
            normalized_text="",
            user_query="",
            messages=[
                {"role": "assistant", "content": "Bonjour, que puis-je faire pour vous ?"},
                {"role": "user", "content": "je voudrais publier un nouveau produit sur le marché"},
            ],
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="NONE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        # Le texte récupéré est long (pas is_short) : la RÈGLE 2/is_short ne
        # doit pas intervenir, on tombe bien sur le repli générique.
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["response_strategy"] == "CLARIFICATION"

    def test_skips_malformed_and_non_user_messages_when_scanning_history(self):
        """La boucle de repli doit ignorer les entrées non-dict et les
        messages non-`user` (assistant/system/tool) pour ne remonter que le
        DERNIER message réellement écrit par l'utilisateur."""
        r = gp(
            interpreted_event="ONBOARDING_INPUT",
            detected_intent="UNRELATED_GARBLED_INTENT",
            normalized_text="",
            user_query="",
            messages=[
                {"role": "user", "content": "je voudrais publier un nouveau produit sur le marché"},
                "une entree malformee qui n'est pas un dict",
                {"role": "assistant", "content": "D'accord, un instant"},
                {"role": "system", "content": "contexte interne"},
            ],
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="NONE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert r["response_strategy"] == "CLARIFICATION"

    def test_short_last_user_message_from_history_still_counts_as_short(self):
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            normalized_text="",
            user_query="",
            messages=[{"role": "user", "content": "ok"}],
            current_goal="BUYER_REQUEST",
            expected_input="NONE",
            working_memory={"active_goal": "BUYER_REQUEST"},
        )
        assert r["current_goal"] == "BUYER_REQUEST"


# =====================================================================
# `goal_metadata` — présent sur TOUTE sortie, cohérent avec INTENT_CONFIG
# =====================================================================

class TestGoalMetadata:
    def test_create_goal_has_create_lifecycle(self):
        r = gp(interpreted_event="NEW_TASK", detected_intent="SALES_PUBLISH_PRODUCT", current_goal=None)
        assert r["goal_metadata"]["lifecycle_mode"] == "CREATE"
        assert r["goal_metadata"]["update_mode"] is False

    def test_update_goal_has_update_lifecycle(self):
        r = gp(interpreted_event="NEW_TASK", detected_intent="SALES_UPDATE_PRODUCT", current_goal=None)
        assert r["goal_metadata"]["lifecycle_mode"] == "UPDATE"
        assert r["goal_metadata"]["update_mode"] is True

    def test_no_goal_defaults_to_read_lifecycle(self):
        r = gp(interpreted_event="OUT_OF_SCOPE", current_goal=None)
        assert r["goal_metadata"]["lifecycle_mode"] == "READ"


# =====================================================================
# BUYER-PRODUCT HEURISTIC (repli déterministe, LLM absent uniquement)
# =====================================================================

class TestBuyerProductFallbackHelpers:
    """`_looks_like_buyer_product_request` / `_extract_buyer_product` ne
    servent plus qu'au repli `_degraded_fallback` (LLM en panne) — pas au
    chemin nominal (voir [[llm-decides-not-frozen-french-lists]])."""

    def test_extracts_meaningful_product_tokens(self):
        from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
            _extract_buyer_product,
        )
        assert _extract_buyer_product("je veux des tomates fraiches") == "tomates fraiches"

    def test_empty_text_yields_no_product(self):
        from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
            _extract_buyer_product,
        )
        assert _extract_buyer_product("") is None

    def test_only_filler_words_yields_no_product(self):
        from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
            _extract_buyer_product,
        )
        assert _extract_buyer_product("je veux acheter") is None

    def test_looks_like_buyer_product_request_true_on_a_clear_hint(self):
        from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
            _looks_like_buyer_product_request,
        )
        assert _looks_like_buyer_product_request("je veux des tomates fraiches") is True

    def test_looks_like_buyer_product_request_false_without_any_hint(self):
        from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
            _looks_like_buyer_product_request,
        )
        assert _looks_like_buyer_product_request("bonjour comment allez vous") is False

    def test_looks_like_buyer_product_request_false_on_empty_text(self):
        from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
            _looks_like_buyer_product_request,
        )
        assert _looks_like_buyer_product_request("") is False

    def test_looks_like_buyer_product_request_false_on_order_tracking_exclude(self):
        """« Je veux suivre ma commande » a le hint « je veux » mais c'est du
        SUIVI (exclu), pas un acte d'achat — la garde `\\bcommande\\b` doit
        neutraliser le repli."""
        from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
            _looks_like_buyer_product_request,
        )
        assert _looks_like_buyer_product_request("je veux suivre ma commande") is False

    def test_looks_like_buyer_product_request_false_when_only_filler_remains(self):
        from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
            _looks_like_buyer_product_request,
        )
        assert _looks_like_buyer_product_request("je veux acheter") is False


# =====================================================================
# MINI-FLOWS PRODUCTEUR/ACHETEUR (bid_phase/update_phase/winner_gps_stage)
# — même classe de bug que le correctif SALES_PUBLISH_PRODUCT, généralisée
# =====================================================================

class TestMiniFlowKeysArePurgedOnRealGoalTransitions:
    """(2026-09-28, audit fiabilité agent) : `bid_phase`/`pending_bid_auction`/
    `pending_bid_price`/`pending_modify_bid`/`update_phase`/`winner_gps_stage`
    vivent EXCLUSIVEMENT dans `working_memory`, hors du système
    `PendingInteraction`/`draft_registry` — ils échappaient ENTIÈREMENT à
    `_purge_transaction_state()` (qui ne connaît que les canaux racine),
    contrairement au chemin RARE d'abandon max-retries
    (`core/conversation_reset.py`), qui les nettoyait déjà. Un prix de bid
    saisi pour une enchère A, puis la conversation part sur un tout autre
    but, puis l'utilisateur revient sur une enchère B totalement différente
    : sans ce correctif, `bid_phase`/`pending_bid_auction`/`pending_bid_
    price` périmés de A pouvaient encore piloter
    `producer_auction_resolver` et faire déposer une offre sur la MAUVAISE
    enchère, au MAUVAIS prix — silencieusement."""

    def test_rule5_new_task_clears_stale_bid_tunnel_state(self):
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="STOCK_REGISTER_HARVEST",
            normalized_text="j'ai récolté 200 kg de maïs",
            current_goal=None,
            working_memory={
                "bid_phase": "CONFIRM",
                "pending_bid_auction": "AUCTION_A_STALE",
                "pending_bid_price": 461000.0,
            },
        )
        wm = r["working_memory"]
        assert wm["bid_phase"] is None
        assert wm["pending_bid_auction"] is None
        assert wm["pending_bid_price"] is None

    def test_rule4_interruption_clears_stale_bid_tunnel_state(self):
        r = gp(
            interpreted_event="INTERRUPTION",
            detected_intent="STOCK_REGISTER_HARVEST",
            interpreter_confidence=0.95,
            cognitive_decision={"action": "INTERRUPT_ACTIVE_GOAL"},
            current_goal="SALES_PLACE_BID",
            expected_input="PRICE",
            working_memory={
                "active_goal": "SALES_PLACE_BID",
                "bid_phase": "ASK_PRICE_MODIFY",
                "pending_modify_bid": "bid-stale",
                "pending_bid_price": 461000.0,
            },
        )
        wm = r["working_memory"]
        assert wm["bid_phase"] is None
        assert wm["pending_modify_bid"] is None
        assert wm["pending_bid_price"] is None

    def test_a_producer_update_or_gps_key_left_over_is_also_cleared(self):
        """Même correctif, les 2 autres mini machines à états (`update_phase`
        côté producteur, `winner_gps_stage` côté acheteur) — même primitive
        `_mini_flow_reset`, mêmes contrats typés `flows/producer/contexts.py`/
        `flows/buyer/contexts.py`."""
        r = gp(
            interpreted_event="NEW_TASK",
            detected_intent="FARM_CREATE",
            normalized_text="je veux créer une nouvelle ferme",
            current_goal=None,
            working_memory={
                "update_phase": "CONFIRM_UPDATE",
                "update_pending": {"price": 999999.0},
                "winner_gps_stage": "AWAIT_GPS",
            },
        )
        wm = r["working_memory"]
        assert wm.get("update_phase") is None
        assert wm.get("update_pending") is None
        assert wm.get("winner_gps_stage") is None

    def test_a_live_active_slot_tunnel_lock_still_preserves_the_mini_flow_state(self):
        """Garde-fou : ce correctif ne doit purger QUE sur une VRAIE
        transition de but (RULE 4/5/1/0bis/4bis/1quater-sans-draft) — jamais
        pendant le verrouillage normal d'un tunnel déjà actif (RULE 1bis),
        qui doit continuer à préserver `bid_phase` intact pendant que
        l'utilisateur répond au prix demandé."""
        r = gp(
            interpreted_event="ANSWER",
            detected_intent="SALES_PLACE_BID",
            current_goal="SALES_PLACE_BID",
            expected_input="PRICE",
            working_memory={
                "active_goal": "SALES_PLACE_BID",
                "bid_phase": "ASK_PRICE",
                "pending_bid_auction": "AUCTION_LIVE",
            },
        )
        wm = r["working_memory"]
        assert wm.get("bid_phase") == "ASK_PRICE"
        assert wm.get("pending_bid_auction") == "AUCTION_LIVE"
