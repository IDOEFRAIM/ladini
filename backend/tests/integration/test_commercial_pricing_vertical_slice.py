"""Phase B1 — vertical slice SALES_PUBLISH_PRODUCT branché sur le modèle commercial.

Ces tests tournent sur le VRAI graphe compilé (harnais conversationnel : orchestrateur, checkpointer,
LLM scripté, MCP en double, store de drafts Postgres simulé). Ils prouvent que l'erreur du 2026-09-28

    « 50 litres de lait » + « 500f le sachet » -> confirmé « 500 FCFA/SAC » puis exécuté à 500 FCFA/LITRE

est impossible PAR ARCHITECTURE : `validator` construit et valide une `CommercialOffer` avant toute
confirmation ; une offre INCOMPLETE ne peut pas atteindre WAITING_CONFIRMATION ; la confirmation et
l'exécution sont dérivées du draft certifié, jamais de l'état brut.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List
from unittest import mock

import pytest

from ladini.services.database import sales_publish_draft_store as store_mod
from tests.architecture.test_sales_publish_draft_persistence import (
    _FakeDraftTable,
    _FakeSession,
)
from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration

INTENT = "SALES_PUBLISH_PRODUCT"


def _nt(**entities):
    return new_task(INTENT, **entities)


def _answer(**entities):
    return {"disposition": "ANSWER", "extracted_entities": entities, "confidence": 0.9}


@pytest.fixture
def conv():
    table = _FakeDraftTable()
    patcher = mock.patch.object(store_mod, "get_sessionmaker", lambda: (lambda: _FakeSession(table)))
    with patcher, ConversationHarness(role="PRODUCER") as c:
        c.runtime.responses["get_farms"] = {
            "status": "success", "data": [{"id": "farm-1", "name": "Ferme Awa"}],
        }
        c.runtime.responses["get_or_create_farm"] = {
            "status": "success", "data": {"farm_id": "farm-1", "id": "farm-1", "name": "Ferme Awa"},
        }
        c.draft_table = table
        yield c


def _creates(conv) -> List[Dict[str, Any]]:
    return [dict(args) for name, args in conv.runtime.calls if name == "create_product"]


def _offer(conv) -> Dict[str, Any]:
    return (conv.state().get("transaction_payload") or {}).get("commercial_offer") or {}


def _draft(conv) -> Dict[str, Any]:
    return conv.state().get("sales_publish_draft") or {}


def _lait_to_confirmation(conv):
    conv.send("je veux vendre 50 litres de lait", llm=_nt(product="lait", quantity=50.0, unit="LITRE"))
    conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))
    return conv.send("0,5 litre")


# =====================================================================
# Golden A — « 500f le sachet » pour 50 litres
# =====================================================================


class TestGoldenA_PackagePriceForLitreQuantity:
    def test_the_agent_asks_the_price_then_the_content_of_the_package(self, conv):
        t1 = conv.send("je veux vendre 50 litres de lait", llm=_nt(product="lait", quantity=50.0, unit="LITRE"))
        assert "prix" in t1.response.lower() and "litre" in t1.response.lower()
        assert (t1.pending_after.kind.value, t1.pending_after.field) == ("ENTER_FIELD", "price")

        t2 = conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))
        assert "Quelle quantité contient un *sachet*" in t2.response
        assert "0,5 L" in t2.response
        assert "Confirmez" not in t2.response
        assert (t2.pending_after.kind.value, t2.pending_after.field) == ("ENTER_FIELD", "package_size")
        assert _draft(conv) == {}, "aucun draft certifiable tant que le contenu du sachet est inconnu"
        assert not _creates(conv)

    def test_the_incomplete_offer_carries_basis_package_and_unknown_size(self, conv):
        conv.send("je veux vendre 50 litres de lait", llm=_nt(product="lait", quantity=50.0, unit="LITRE"))
        conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))
        offer = _offer(conv)
        assert offer["commercial_quantity"]["amount"] == 50.0 and offer["commercial_quantity"]["unit"] == "LITRE"
        assert offer["pricing"]["amount"] == 500.0
        assert offer["pricing"]["basis"] == "PER_PACKAGE"
        assert offer["pricing"]["basis_source"] == "USER_EXPLICIT"
        assert offer["package"]["package_type"] == "SACHET"
        assert offer["package"]["content_amount"] is None
        assert offer["package"]["status"] == "UNKNOWN"

    def test_the_package_size_reply_is_package_content_not_a_new_quantity(self, conv):
        t3 = _lait_to_confirmation(conv)
        offer = _offer(conv)
        assert offer["commercial_quantity"]["amount"] == 50.0, "0,5 litre ne remplace JAMAIS la quantité"
        assert offer["commercial_quantity"]["unit"] == "LITRE"
        assert offer["package"]["content_amount"] == 0.5
        assert offer["package"]["content_unit"] == "LITRE"
        assert offer["package"]["source"] == "QUESTION_CONTEXT_EXPLICIT"
        assert "50 litres de lait à 500 FCFA par sachet de 0,5 litre" in t3.response
        assert t3.pending_after.kind.value == "CONFIRM_ACTION"

    def test_execution_uses_the_certified_offer_and_the_idempotency_key(self, conv):
        _lait_to_confirmation(conv)
        row = next(iter(conv.draft_table._rows.values()))
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["name"] == "lait"
        assert call["quantity_for_sale"] == 50.0 and call["unit"] == "LITRE"
        # prix ramené à l'unité de stock : 500 FCFA / 0,5 L = 1000 FCFA / L ; le palier garde le sachet
        assert call["price"] == 1000.0
        assert call["pricing_tiers"] == [{"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet"}]
        assert re.fullmatch(rf"sales_publish:{row['draft_id']}:\d+", call["idempotency_key"])
        # état terminal : PUBLISHED, version incrémentée (CONFIRM -> EXECUTING -> PUBLISHED)
        (final,) = conv.draft_table._rows.values()
        assert final["status"] == "PUBLISHED"

    def test_a_second_ok_never_republishes(self, conv):
        _lait_to_confirmation(conv)
        conv.send("oui")
        conv.send("oui")
        assert len(_creates(conv)) == 1


# =====================================================================
# Golden B/C — prix sans base explicite
# =====================================================================


class TestGoldenB_PriceWithoutBasisIsAskedThenBound:
    def test_the_bare_number_answer_takes_the_asked_basis_from_the_question(self, conv):
        t1 = conv.send("je vends 200 tonnes de maïs", llm=_nt(product="maïs", quantity=200.0, unit="TONNE"))
        assert "par tonne" in t1.response
        t2 = conv.send("500000", llm=_answer(price=500000.0))
        assert "500 000 FCFA par tonne" in t2.response
        offer = _offer(conv)
        assert offer["pricing"]["basis"] == "PER_BASE_UNIT" and offer["pricing"]["basis_unit"] == "TONNE"
        assert offer["pricing"]["basis_source"] == "QUESTION_CONTEXT_EXPLICIT"
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["quantity_for_sale"] * call["price"] == pytest.approx(200 * 500000)


class TestGoldenC_AmbiguousPriceIsNeverGuessed:
    def _start(self, conv):
        return conv.send(
            "je vends 200 tonnes de maïs à 500000",
            llm=_nt(product="maïs", quantity=200.0, unit="TONNE", price=500000.0),
        )

    def test_it_asks_per_unit_or_whole_lot_and_does_not_confirm(self, conv):
        t1 = self._start(conv)
        assert "par tonne" in t1.response and "pour l'ensemble" in t1.response
        assert "Confirmez" not in t1.response
        assert _draft(conv) == {}
        assert _offer(conv)["pricing"]["basis"] is None
        assert _offer(conv)["pricing"]["basis_source"] == "UNKNOWN"

    def test_per_tonne_answer(self, conv):
        self._start(conv)
        t2 = conv.send("par tonne")
        assert "500 000 FCFA par tonne" in t2.response
        assert _offer(conv)["pricing"]["basis_source"] == "USER_EXPLICIT"

    def test_whole_lot_answer_is_a_total_lot_price(self, conv):
        self._start(conv)
        t2 = conv.send("pour l'ensemble")
        assert "pour 500 000 FCFA au total" in t2.response
        assert _offer(conv)["pricing"]["basis"] == "TOTAL_LOT"
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["quantity_for_sale"] * call["price"] == pytest.approx(500000)


class TestTotalLot:
    def test_explicit_total_is_certified_directly_without_extra_question(self, conv):
        t1 = conv.send(
            "je vends les 200 tonnes de maïs pour 5000000 au total",
            llm=_nt(product="maïs", quantity=200.0, unit="TONNE", price=5000000.0),
        )
        assert "pour 5 000 000 FCFA au total" in t1.response
        assert t1.pending_after.kind.value == "CONFIRM_ACTION"
        assert _offer(conv)["pricing"]["basis"] == "TOTAL_LOT"


# =====================================================================
# Golden D/E/F — produits simples et taxonomie
# =====================================================================


class TestGoldenD_ExplicitPerLitre:
    def test_no_extra_question(self, conv):
        t1 = conv.send(
            "je vends 50 litres de lait à 500 fcfa le litre",
            llm=_nt(product="lait", quantity=50.0, unit="LITRE", price=500.0, price_unit="LITRE"),
        )
        assert "50 litres de lait à 500 FCFA par litre" in t1.response
        assert t1.pending_after.kind.value == "CONFIRM_ACTION"
        conv.send("oui")
        (call,) = _creates(conv)
        assert (call["quantity_for_sale"], call["unit"], call["price"]) == (50.0, "LITRE", 500.0)
        assert "pricing_tiers" not in call


class TestGoldenE_LivestockPerHead:
    def test_bovins_are_priced_per_head(self, conv):
        t1 = conv.send(
            "je vends 20 boeufs à 450000 la tête",
            llm=_nt(product="boeufs", quantity=20.0, unit="TETE", price=450000.0, price_unit="TETE"),
        )
        assert "20 têtes de boeufs à 450 000 FCFA par tête" in t1.response
        conv.send("oui")
        (call,) = _creates(conv)
        assert (call["quantity_for_sale"], call["unit"], call["price"]) == (20.0, "TETE", 450000.0)


class TestGoldenF_TaxonomyBeatsNameHeuristic:
    def test_lait_de_vache_is_never_counted_in_heads(self, conv):
        t1 = conv.send(
            "je vends 50 litres de lait de vache",
            llm=_nt(product="lait de vache", quantity=50.0, unit="TETE"),  # le LLM se trompe d'unité
        )
        assert "tête" not in t1.response.lower()
        assert "par litre" in t1.response
        assert _offer(conv)["commercial_quantity"]["unit"] == "LITRE"


class TestSimpleProductUnchanged:
    def test_maize_per_kg_needs_no_extra_question(self, conv):
        t1 = conv.send(
            "je veux vendre 100 kg de maïs à 300 fcfa le kg",
            llm=_nt(product="maïs", quantity=100.0, unit="KG", price=300.0, price_unit="KG"),
        )
        assert "100 kg de maïs à 300 FCFA par kg" in t1.response
        assert t1.pending_after.kind.value == "CONFIRM_ACTION"
        conv.send("oui")
        (call,) = _creates(conv)
        assert (call["quantity_for_sale"], call["unit"], call["price"]) == (100.0, "KG", 300.0)
        assert "pricing_tiers" not in call


# =====================================================================
# Certification : la confirmation et l'exécution lisent le draft, pas l'état brut
# =====================================================================


class TestCertifiedVersionIsWhatGetsExecuted:
    def test_raw_state_mutation_after_confirmation_is_ignored_by_execution(self, conv):
        _lait_to_confirmation(conv)
        conv.seed({"transaction_payload": {"quantity": 999.0, "price": 1.0, "price_unit": "KG"}})
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["quantity_for_sale"] == 50.0
        assert call["pricing_tiers"][0]["price"] == 500.0
        assert call["price"] == 1000.0

    def test_the_confirmation_text_comes_from_the_draft(self, conv):
        conv.send("je veux vendre 50 litres de lait", llm=_nt(product="lait", quantity=50.0, unit="LITRE"))
        conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))
        t3 = conv.send("0,5 litre")
        expected = _draft(conv)["commercial_offer"]
        assert expected["package"]["content_amount"] == 0.5
        assert "sachet de 0,5 litre" in t3.response


# =====================================================================
# Corrections : chaque correction certifiée ajoute une VERSION du draft
# =====================================================================


class TestCorrectionsBumpTheDraftVersion:
    def test_price_correction_keeps_the_package_content(self, conv):
        _lait_to_confirmation(conv)
        v_before = _draft(conv)["version"]
        t = conv.send("finalement 600 le sachet", llm=_nt(price=600.0, price_unit="SAC"))
        assert "600 FCFA par sachet de 0,5 litre" in t.response
        assert _draft(conv)["version"] == v_before + 1
        assert _offer(conv)["commercial_quantity"]["amount"] == 50.0, "« 600 le sachet » n'est pas 600 sachets"

    def test_content_correction_keeps_the_price(self, conv):
        _lait_to_confirmation(conv)
        v_before = _draft(conv)["version"]
        t = conv.send("finalement 1L le sachet", llm=_nt(quantity=1.0, unit="LITRE"))
        assert "500 FCFA par sachet de 1 litre" in t.response
        assert _draft(conv)["version"] == v_before + 1
        assert _offer(conv)["commercial_quantity"]["amount"] == 50.0

    def test_the_execution_uses_the_last_certified_version(self, conv):
        _lait_to_confirmation(conv)
        conv.send("finalement 600 le sachet", llm=_nt(price=600.0, price_unit="SAC"))
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["pricing_tiers"][0]["price"] == 600.0
        assert call["price"] == 1200.0


# =====================================================================
# Invalidation : produit / unité changés => la base et le conditionnement ne survivent pas
# =====================================================================


class TestInvalidation:
    def test_product_change_with_full_data_does_not_inherit_the_package(self, conv):
        _lait_to_confirmation(conv)
        t = conv.send(
            "finalement je vends 20 boeufs à 450000 la tête",
            llm=_nt(product="boeufs", quantity=20.0, unit="TETE", price=450000.0, price_unit="TETE"),
        )
        assert "sachet" not in t.response
        assert _offer(conv)["package"] is None
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["name"] == "boeufs" and "pricing_tiers" not in call

    def test_product_change_without_data_purges_quantity_and_price(self, conv):
        _lait_to_confirmation(conv)
        conv.send("finalement je vends des boeufs", llm=_nt(product="boeufs"))
        tp = conv.state().get("transaction_payload") or {}
        assert tp.get("quantity") in (None, "") and tp.get("price") in (None, "")
        # les ALIAS (quantite, prix, montant…) ne ressuscitent pas 50 / 500 dans le validateur
        for alias in ("quantite", "qty", "volume", "prix", "montant", "prix_unitaire", "offered_price"):
            assert tp.get(alias) in (None, ""), alias
        assert not _creates(conv)

    def test_unit_change_forces_price_revalidation(self, conv):
        conv.send(
            "je vends 200 tonnes de maïs à 500000 la tonne",
            llm=_nt(product="maïs", quantity=200.0, unit="TONNE", price=500000.0, price_unit="TONNE"),
        )
        t = conv.send("finalement 200 kg", llm=_nt(quantity=200.0, unit="KG"))
        assert "Confirmez" not in t.response
        assert "500 000 FCFA" in t.response and "200 kg" in t.response
        assert not _creates(conv)


# =====================================================================
# Cross-flow : rien de l'offre ne survit à la fin d'un flux ni à une nouvelle action
# =====================================================================


class TestCrossFlowCleanup:
    def _riz(self, conv):
        return conv.send(
            "je veux vendre 20 kg de riz à 400 fcfa le kg",
            llm=_nt(product="riz", quantity=20.0, unit="KG", price=400.0, price_unit="KG"),
        )

    def test_after_completion_the_next_sale_is_clean(self, conv):
        _lait_to_confirmation(conv)
        conv.send("oui")
        t = self._riz(conv)
        assert "sachet" not in t.response and "20 kg de riz à 400 FCFA par kg" in t.response
        assert _offer(conv)["package"] is None
        conv.send("oui")
        assert _creates(conv)[-1]["name"] == "riz" and "pricing_tiers" not in _creates(conv)[-1]

    def test_after_cancel_the_next_sale_is_clean(self, conv):
        _lait_to_confirmation(conv)
        t = conv.send("non")
        assert "annulée" in t.response
        t = self._riz(conv)
        assert "sachet" not in t.response
        assert _offer(conv)["package"] is None
        conv.send("oui")
        assert "pricing_tiers" not in _creates(conv)[-1]

    def test_after_timeout_the_stale_question_does_not_capture_the_next_message(self, conv):
        conv.send("je veux vendre 50 litres de lait", llm=_nt(product="lait", quantity=50.0, unit="LITRE"))
        conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))
        stale = dict(conv.state()["pending_interaction"])
        conv.seed({"pending_interaction": {**stale, "created_at": stale["created_at"] - 1900.0}})
        t = self._riz(conv)
        assert t.pending_before.kind.value == "NONE"
        assert "sachet" not in t.response and "20 kg de riz à 400 FCFA par kg" in t.response
        assert _offer(conv)["package"] is None

    def test_same_intent_new_action_during_a_package_question_is_not_package_content(self, conv):
        conv.send("je veux vendre 50 litres de lait", llm=_nt(product="lait", quantity=50.0, unit="LITRE"))
        conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))
        t = conv.send("je veux vendre 30 kg de tomates", llm=_nt(product="tomates", quantity=30.0, unit="KG"))
        assert "Quelle quantité contient" not in t.response
        assert "par kg" in t.response
        offer = _offer(conv)
        assert offer["package"] is None and offer["commercial_quantity"]["amount"] == 30.0
        conv.send("300 fcfa le kg", llm=_answer(price=300.0, price_unit="KG"))
        conv.send("oui")
        (call,) = _creates(conv)
        assert (call["name"], call["quantity_for_sale"], call["price"]) == ("tomates", 30.0, 300.0)
        assert "pricing_tiers" not in call


# =====================================================================
# Provenance : rien d'inféré n'est jamais marqué explicite
# =====================================================================


class TestProvenance:
    def test_a_price_unit_the_user_never_said_is_not_execution_safe(self, conv):
        # le LLM invente « price_unit=TONNE » : le texte ne le dit pas => INFÉRÉ, donc on redemande
        t = conv.send(
            "je vends 200 tonnes de maïs à 500000",
            llm=_nt(product="maïs", quantity=200.0, unit="TONNE", price=500000.0, price_unit="TONNE"),
        )
        assert "Confirmez" not in t.response
        assert _offer(conv)["pricing"]["basis_source"] in ("LLM_INFERRED", "UNKNOWN")
        assert _draft(conv) == {}

    def test_explicit_text_makes_the_basis_user_explicit(self, conv):
        conv.send(
            "je vends 20 boeufs à 450000 la tête",
            llm=_nt(product="boeufs", quantity=20.0, unit="TETE", price=450000.0, price_unit="TETE"),
        )
        assert _offer(conv)["pricing"]["basis_source"] == "USER_EXPLICIT"

