"""Matrice de régression état-machine / propriétés (Phase 2 hardening, commit 12).

## Portée délibérément REDUITE, pas un défaut d'oubli

Le mandat demande, pour "chaque flow transactionnel majeur", de couvrir au moins :
fresh conversation, stale pending d'un AUTRE flow, draft actif du même flow, draft actif
d'un AUTRE flow, clarification, correction, annulation, interruption, confirmation,
confirmation dupliquée, retry transport, confiance faible, extraction malformée, reset de
conversation, pending expiré, draft complété, tour concurrent, WebChat, WhatsApp.

`tests/harness/conversation.py` (commit 1) ne double le domaine métier QUE pour
`recurring_need` (`tests/harness/recurring.py::RecurringSupplyServerDouble`) — aucune
doublure MCP n'existe pour procurement/preorder/sales_publish. Construire ces doublures
pour "au moins deux flows" serait une extension d'infrastructure de test substantielle,
hors du périmètre "commit par commit, pas de gros refactor" du mandat (§ garde-fous). Ce
commit garde donc `recurring_need` comme flow DE RÉFÉRENCE — c'est déjà, de très loin, le
flow le mieux couvert (voir `test_conversation_characterization.py`, ~50 tests) — et
DOCUMENTE explicitement, plutôt que de les simuler, les deux dimensions qui en dépendent :

  - "draft actif d'un AUTRE flow" : non testable sans doublure procurement/preorder/
    sales_publish. Non simulé (un faux "autre flow" ne prouverait rien sur le VRAI
    routage inter-domaines).
  - "reset de conversation" : aucune commande de reset n'existe dans le catalogue
    d'intentions du moteur (`grep -rn "reset" interpreter/intent.py` — rien). Rien à
    tester tant que cette fonctionnalité n'existe pas.

## Table de couverture (17 des 19 dimensions listées, les 2 ci-dessus étant hors périmètre)

| Dimension                      | Test couvrant                                                         |
|---------------------------------|-----------------------------------------------------------------------|
| fresh conversation               | test_conversation_characterization.py::TestA (goal_before None)      |
| stale pending, autre flow        | ...::TestI_StaleCatalogThenRecurring (tunnel catalogue périmé)        |
| draft actif, même flow           | ...::TestG_Correction / TestG_CorrectionPolicy                       |
| draft actif, autre flow          | HORS PÉRIMÈTRE (voir ci-dessus)                                       |
| clarification                    | ...::TestCDE_AmbiguousGroup                                           |
| correction                       | ...::TestG_Correction / TestG_CorrectionPolicy                       |
| annulation                       | ...::TestHM_Cancellation                                              |
| interruption                     | ...::TestL_ShortApprovedInterruption                                  |
| confirmation                     | ...::TestA (test_confirmation_creates_exactly_one_need...)            |
| confirmation dupliquée           | ...::TestJ_RepeatedConfirmation                                       |
| retry transport                  | ...::TestJ_RepeatedConfirmation::test_the_same_whatsapp_message_...   |
| confiance faible                 | CE FICHIER : TestLowConfidence (révèle H7, xfail permanent, cf. infra)|
| extraction malformée             | CE FICHIER : TestMalformedExtraction                                  |
| reset de conversation            | HORS PÉRIMÈTRE (voir ci-dessus)                                       |
| pending expiré                   | ...::TestQ_PendingInteractionTTL                                      |
| draft complété                   | ...::TestK_NewRequestAfterCompleted                                   |
| tour concurrent                  | ...::TestO_ConcurrentMessages                                         |
| WebChat                          | CE FICHIER : TestWebChatFullLifecycle (cycle complet, pas 1 seul tour)|
| WhatsApp                         | ...::TestA et la quasi-totalité du fichier (canal par défaut)         |

## Property-based tests : Hypothesis évaluée, non ajoutée

Le mandat autorise Hypothesis "seulement si elle apporte une vraie valeur" — elle
n'est PAS une dépendance existante du projet (`ModuleNotFoundError` à l'audit de ce
commit). L'ajouter engagerait pyproject/CI pour un gain marginal : les invariants ciblés
(frontière TTL, total-ité de `classify_turn`, idempotence de `canonical_unit_label`)
sont des fonctions PURES à faible arité, entièrement couvertes par un échantillon
déterministe de valeurs limites + représentatives (`pytest.mark.parametrize`
ci-dessous) — même pouvoir de preuve, aucun risque de dépendance nouvelle pendant une
phase de durcissement. Décision documentée ici plutôt que silencieuse.

## H5 (correction numérique pendant une confirmation étrangère)

Réévalué (voir le xfail lui-même, `test_conversation_characterization.py::
TestG_CorrectionPolicy::test_a_fresh_full_request_during_an_unrelated_confirmation_is_
never_swallowed_by_the_numeric_shortcut`) : C11 n'a pas changé la donne (classify_turn
reste SHADOW). xfail strict maintenu, condition de fermeture inchangée.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    PendingInteraction,
    is_pending_expired,
)
from ladini.graphs.agents.market_coach.core.turn_policy import TurnAction, classify_turn
from ladini.graphs.agents.market_coach.utils import canonical_unit_label
from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration


def _coq(**extra: Any) -> Dict[str, Any]:
    return new_task("CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="WEEKLY", **extra)


def _created(conv: ConversationHarness) -> list:
    return [(tool, kw) for tool, kw in conv.runtime.calls if tool in ("create_recurring_need", "create_recurring_needs")]


@pytest.fixture()
def conv():
    with ConversationHarness(role="BUYER", channel="whatsapp") as harness:
        yield harness


# =====================================================================
# Confiance faible pendant une CONFIRMATION en attente
# =====================================================================


class TestLowConfidence:
    def test_a_low_confidence_new_task_during_confirmation_never_interrupts(self, conv):
        """Complément direct de `TestL_ShortApprovedInterruption` (même scénario, confiance
        au-dessus du seuil) : `interpreter/routing.py` (~ligne 2755-2758) documente que
        SOUS `INTERRUPTION_CONFIDENCE_THRESHOLD` (0.60), le moteur reste "proprement dans la
        confirmation" — RÈGLE 2 du goal_planner re-verrouille le goal — au lieu d'interrompre
        à l'aveugle sur un score peu fiable. Vérifié séparément de la propriété du draft
        (voir le xfail H7 ci-dessous) : rester dans le MÊME goal est correct, ce que ce
        goal fait ENSUITE du message ne l'est pas forcément."""
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t = conv.send("maïs", llm=new_task("BUYER_ADD_TO_CART", product="maïs", confidence=0.4))
        assert t.decision.get("action") != "INTERRUPT_ACTIVE_GOAL"
        assert t.goal_after == "CREATE_RECURRING_NEED"
        assert t.pending_after.kind.value == "CONFIRM_ACTION"

    @pytest.mark.xfail(strict=True, reason=(
        "H7 (découvert commit 12, même famille que H5 — voir son xfail dans "
        "test_conversation_characterization.py) : `flows/buyer/recurring_need.py::"
        "_is_correction` (ligne ~474) traite TOUT message NEW_TASK sans `recurrence_type` "
        "explicite comme une correction du draft EN COURS ('même intention reformulée SANS "
        "sa propre fréquence'), sans jamais vérifier que le produit nommé a un rapport "
        "quelconque avec le draft actif. Un message à confiance trop faible pour interrompre "
        "(< INTERRUPTION_CONFIDENCE_THRESHOLD=0.60, donc `cognitive_guard` choisit "
        "CONTINUE_ACTIVE_GOAL par défaut, `nodes/cognitive.py` ligne 164) tombe dans cette "
        "branche et écrase silencieusement `draft.product` par un produit SANS RAPPORT "
        "('maïs' remplace 'coq') — la CONFIRMATION suivante ('oui') créerait alors le MAUVAIS "
        "produit, sans qu'aucune clarification n'ait jamais été demandée. Root cause "
        "identique à H5 dans sa nature (aucun signal générique 'ce produit est étranger au "
        "draft courant' disponible sans lexique figé) mais un chemin de déclenchement "
        "DIFFÉRENT (ici `_is_correction`, pas `_interpret_fast_path::_confirmation_ "
        "correction`) : les deux sites devront être corrigés ensemble le jour où "
        "`decide_turn`/`classify_turn` (core/turn_policy.py) devient autoritaire (post-C14, "
        "même condition de fermeture que H5), ou plus tôt si un signal produit générique "
        "apparaît."
    ))
    def test_a_low_confidence_new_task_naming_an_unrelated_product_never_corrupts_the_pending_draft(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        t = conv.send("maïs", llm=new_task("BUYER_ADD_TO_CART", product="maïs", confidence=0.4))
        draft = t.draft()
        assert draft is not None and draft["product"] == "coq", (
            f"le draft coq a été corrompu par un message à confiance trop faible pour "
            f"interrompre mais absorbé comme correction : {draft!r}"
        )


# =====================================================================
# Extraction malformée (JSON illisible renvoyé par le LLM)
# =====================================================================


class TestMalformedExtraction:
    def test_unparseable_llm_output_degrades_safely_without_a_crash(self, conv):
        """Le LLM renvoie un texte qui n'est PAS du JSON valide (panne de formatage réelle,
        distincte de `_UNKNOWN` qui simule une désambiguïsation propre côté LLM) — le moteur
        doit dégrader proprement (une réponse existe, aucun draft fantôme) plutôt que de
        laisser une exception de désérialisation remonter jusqu'à l'orchestrateur."""
        t = conv.send("je veux des trucs", llm="{ceci n'est pas du json")
        assert t.error is None, f"le tour n'aurait jamais dû lever : {t.error!r}"
        assert isinstance(t.response, str) and t.response.strip()
        assert t.draft() is None
        assert _created(conv) == []


# =====================================================================
# WebChat — cycle COMPLET (création -> confirmation -> exécution), pas 1 seul tour
# =====================================================================


class TestWebChatFullLifecycle:
    def test_create_confirm_and_execute_over_webchat_produces_the_same_side_effect_as_whatsapp(self):
        """`test_turn_trace.py::TestF_WebChatWhatsAppParity` prouve la parité sur UN tour
        (création de draft). Ce test complète le cycle jusqu'à l'exécution — le point qui
        compte réellement métier (un besoin récurrent réellement créé) — en restant sur
        WebChat du premier au dernier tour, jamais un mélange des deux canaux."""
        with ConversationHarness(role="BUYER", channel="webchat") as conv:
            conv.send("je veux 14 coqs chaque semaine", llm=_coq())
            conv.send("oui")
            created = _created(conv)
            assert [tool for tool, _ in created] == ["create_recurring_need"]
            item = created[0][1]
            assert (item.get("product_query"), item.get("quantity")) == ("coq", 14.0)


# =====================================================================
# Propriétés (fonctions pures) — échantillon déterministe, voir docstring de module
# =====================================================================


class TestProperty_PendingInteractionTTLBoundary:
    """`is_pending_expired` (core/pending_interaction.py) : frontière stricte, documentée
    dans sa propre docstring ("30:00 est encore ACTIF, 30:00.001 est EXPIRED")."""

    @pytest.mark.parametrize(
        "elapsed_seconds,expected_expired",
        [
            (0.0, False),
            (1.0, False),
            (1799.999, False),
            (1800.0, False),  # exactement au TTL : encore actif (strictement '>')
            (1800.001, True),
            (1801.0, True),
            (3600.0, True),
            (86400.0, True),  # 24h — jamais "encore actif" par accident d'arithmétique
        ],
    )
    def test_expiry_decision_matches_the_documented_strict_boundary(
        self, elapsed_seconds: float, expected_expired: bool
    ):
        pending = PendingInteraction(
            kind=InteractionKind.CONFIRM_ACTION, created_at=1_000_000.0
        )
        now = 1_000_000.0 + elapsed_seconds
        assert is_pending_expired(pending, now, ttl_seconds=1800.0) is expected_expired

    def test_a_pending_with_no_kind_never_expires_regardless_of_elapsed_time(self):
        """`kind == NONE` : rien n'est en attente, `created_at` n'a par construction
        jamais été posé — `is_pending_expired` doit rester `False` même sur un délai
        énorme, jamais un faux-positif d'expiration sur "rien"."""
        pending = PendingInteraction()
        assert is_pending_expired(pending, now=1_000_000_000.0, ttl_seconds=1.0) is False


class TestProperty_ClassifyTurnIsTotal:
    """`classify_turn` (core/turn_policy.py, SHADOW ONLY — voir C11/turn_trace.py) tourne
    maintenant sur CHAQUE tour de production (branché par C11). Une exception ici serait
    avalée par le `try/except` best-effort de `turn_trace.capture_pre_cleanup` — silencieuse,
    donc jamais un crash visible, mais un TROU de télémétrie. Ce test prouve que la fonction
    est TOTALE sur un échantillon volontairement hostile (chaînes vides, valeurs hors
    vocabulaire, `None`, unicode) plutôt que de compter sur ce filet best-effort."""

    @pytest.mark.parametrize(
        "interpreted_event,goal_before,goal_after,normalized_text",
        [
            (None, None, None, ""),
            ("", "", "", ""),
            ("UNKNOWN", None, None, ""),
            ("NEW_TASK", None, "CREATE_RECURRING_NEED", "je veux 14 coqs"),
            ("CONFIRM", "CREATE_RECURRING_NEED", None, "oui"),
            ("CE_MOT_N_EXISTE_PAS_DANS_LE_VOCABULAIRE", "GOAL_INCONNU", "AUTRE_GOAL_INCONNU", "🐔🐔🐔"),
            ("new_task", "create_recurring_need", "create_recurring_need", "  laisse tomber  "),
            (None, "CREATE_RECURRING_NEED", "CREATE_RECURRING_NEED", "non plutôt 23 boeufs"),
        ],
    )
    def test_classify_turn_never_raises_and_always_returns_a_known_action(
        self, interpreted_event, goal_before, goal_after, normalized_text
    ):
        result = classify_turn(
            interpreted_event=interpreted_event,
            cognitive_decision=None,
            goal_before=goal_before,
            goal_after=goal_after,
            normalized_text=normalized_text,
        )
        assert isinstance(result.action, TurnAction)
        assert result.action in set(TurnAction)

    def test_classify_turn_never_raises_on_a_malformed_cognitive_decision(self):
        """`cognitive_decision` est un dict quelconque produit par une couche amont — une
        forme inattendue (clé `action` absente, valeur non-string) ne doit jamais faire
        planter le classifieur SHADOW."""
        for bad_decision in ({}, {"action": None}, {"action": 12345}, {"unrelated": "x"}):
            result = classify_turn(
                interpreted_event="NEW_TASK",
                cognitive_decision=bad_decision,
                goal_before=None,
                goal_after="CREATE_RECURRING_NEED",
                normalized_text="",
            )
            assert isinstance(result.action, TurnAction)


class TestProperty_CanonicalUnitLabelIsIdempotent:
    """P1 fermé au commit 8 (voir `test_both_items_share_one_canonical_unit_even_with_
    mismatched_raw_spelling`) : une régression future de `_CANONICAL_UNIT_MAP` pourrait
    réintroduire un mapping qui ne se stabilise pas en un seul passage (`x -> y -> z`,
    `y != z`) — l'idempotence prouve qu'appliquer la fonction 2 fois ne change plus rien
    après le premier passage, sur tout l'alphabet connu ET sur du bruit."""

    @pytest.mark.parametrize(
        "raw",
        [
            "kg", "KG", "Kg", "tête", "TÊTE", "tete", "TETES", "head", "HEADS",
            "unite", "UNITE", "tonne", "TONNE", "", "   ", "xyz-inconnu", "🐔",
            None,
        ],
    )
    def test_applying_canonical_unit_label_twice_is_the_same_as_once(self, raw):
        once = canonical_unit_label(raw)
        twice = canonical_unit_label(once)
        assert once == twice
