"""Analyse déterministe et adaptateur legacy -> CommercialOffer (Phase B1).

`domain/commercial_offer_flow.py` est l'UNIQUE endroit où l'état legacy (`transaction_payload`,
texte, entités dites CE tour) devient une `CommercialOffer`. Ces tests verrouillent ses décisions
sans passer par le graphe : provenance réelle, contexte de question, garde « réponse pure »."""
from __future__ import annotations

import pytest

from ladini.domain.commercial_offer import PriceBasis, Provenance
from ladini.domain.commercial_offer_flow import (
    FIELD_PACKAGE_SIZE,
    FIELD_PRICE_BASIS,
    CommercialQuestion,
    evaluate_sales_offer,
    normalize_text,
    parse_basis_reply,
    parse_package_content,
    price_expression_in_text,
    price_unit_next_to_amount,
)

PACKAGE_Q = CommercialQuestion(requested_field=FIELD_PACKAGE_SIZE, package_type="SACHET", content_unit="LITRE")
BASIS_Q = CommercialQuestion(requested_field=FIELD_PRICE_BASIS, expected_basis_unit="TONNE")


class TestPackageContentReply:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0,5 litre", (0.5, "LITRE")),
            ("0.5 l", (0.5, "LITRE")),
            ("1 litre", (1.0, "LITRE")),
            ("ça fait 1 litre", (1.0, "LITRE")),
            ("500 ml", (0.5, "LITRE")),
            ("environ 1 litre par sachet", (1.0, "LITRE")),
        ],
    )
    def test_pure_content_replies_are_read_in_the_question_context(self, text, expected):
        amount, unit, source = parse_package_content(text, question=PACKAGE_Q)
        assert (amount, unit) == expected
        assert source in (Provenance.QUESTION_CONTEXT_EXPLICIT, Provenance.USER_EXPLICIT)

    @pytest.mark.parametrize(
        "text",
        [
            "je veux vendre 30 kg de tomates",  # NOUVELLE vente, pas un contenu de sachet
            "je vends 20 boeufs",
            "400 fcfa le litre",  # un PRIX
            "oui",
            "annuler",
        ],
    )
    def test_a_new_action_or_a_price_is_never_package_content(self, text):
        assert parse_package_content(text, question=PACKAGE_Q) is None

    def test_a_named_package_sentence_is_explicit_even_without_a_question(self):
        amount, unit, source = parse_package_content("finalement 1L le sachet")
        assert (amount, unit, source) == (1.0, "LITRE", Provenance.USER_EXPLICIT)

    def test_a_bare_number_without_a_question_is_not_a_content(self):
        assert parse_package_content("0,5 litre") is None


class TestBasisReply:
    def test_per_unit(self):
        reply = parse_basis_reply("par tonne", commercial_unit="TONNE")
        assert (reply.basis, reply.basis_unit) == (PriceBasis.PER_BASE_UNIT, "TONNE")
        assert reply.source == Provenance.USER_EXPLICIT

    def test_whole_lot_even_with_an_apostrophe(self):
        assert parse_basis_reply("pour l'ensemble", commercial_unit="TONNE").basis == PriceBasis.TOTAL_LOT
        assert parse_basis_reply("pour l’ensemble", commercial_unit="TONNE").basis == PriceBasis.TOTAL_LOT

    def test_lone_article_letters_are_not_units(self):
        # « l'ensemble » ne doit jamais être lu comme « l » = LITRE
        reply = parse_basis_reply("pour l'ensemble", commercial_unit="LITRE")
        assert reply.basis == PriceBasis.TOTAL_LOT and reply.basis_unit is None

    @pytest.mark.parametrize("text", ["oui", "je sais pas", "500", "300 fcfa le kg"])
    def test_a_reply_that_does_not_decide_returns_none(self, text):
        assert parse_basis_reply(text, commercial_unit="TONNE") is None


class TestPriceExpression:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("finalement 600 le sachet", (600.0, "SAC")),
            ("500f le sachet", (500.0, "SAC")),
            ("500 fcfa/kg", (500.0, "KG")),
            ("450000 la tête", (450000.0, "TETE")),
        ],
    )
    def test_number_then_base_marker_then_unit_is_a_price(self, text, expected):
        assert price_expression_in_text(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["50 litres de lait", "200 tonnes de maïs à 500000", "je veux 600 sachets", "oui"],
    )
    def test_a_quantity_is_not_a_price_expression(self, text):
        assert price_expression_in_text(text) is None

    def test_the_quantity_unit_is_never_attached_to_the_price(self):
        # régression du scanner générique : « tonnes » collé à 500000
        assert price_unit_next_to_amount("200 tonnes de maïs à 500000", 500000.0) is None


class TestNormalizeText:
    def test_apostrophes_become_spaces(self):
        assert normalize_text("l'ensemble") == "l ensemble"
        assert normalize_text("l’ensemble") == "l ensemble"


class TestAdapterProvenance:
    def _eval(self, payload, *, said, text, question=None):
        return evaluate_sales_offer(payload, said=said, text=text, question=question)

    def test_price_with_a_package_unit_but_no_content_is_incomplete_and_asks_the_size(self):
        result = self._eval(
            {"product": "lait", "quantity": 50.0, "unit": "LITRE", "price": 500.0, "price_unit": "SAC"},
            said={"price": 500.0, "price_unit": "SAC"},
            text="500f le sachet",
        )
        assert result.validation.status == "INCOMPLETE"
        assert result.offer.pricing.basis == PriceBasis.PER_PACKAGE
        assert result.offer.pricing.basis_source == Provenance.USER_EXPLICIT
        assert result.question.requested_field == FIELD_PACKAGE_SIZE
        assert "Quelle quantité contient un *sachet*" in result.question_text

    def test_price_without_any_basis_is_unknown_never_guessed(self):
        result = self._eval(
            {"product": "maïs", "quantity": 200.0, "unit": "TONNE", "price": 500000.0},
            said={"product": "maïs", "quantity": 200.0, "unit": "TONNE", "price": 500000.0},
            text="je vends 200 tonnes de maïs à 500000",
        )
        assert result.validation.status == "INCOMPLETE"
        assert result.offer.pricing.basis is None
        assert result.offer.pricing.basis_source == Provenance.UNKNOWN
        assert result.question.requested_field == FIELD_PRICE_BASIS
        assert "par tonne" in result.question_text and "pour l'ensemble" in result.question_text

    def test_price_unit_only_in_the_llm_entities_is_inferred_not_explicit(self):
        result = self._eval(
            {"product": "maïs", "quantity": 200.0, "unit": "TONNE", "price": 500000.0, "price_unit": "TONNE"},
            said={"price": 500000.0, "price_unit": "TONNE"},
            text="je vends 200 tonnes de maïs à 500000",
        )
        assert result.offer.pricing.basis_source == Provenance.LLM_INFERRED
        assert not result.offer.pricing.basis_source.is_execution_safe
        assert result.validation.status == "INCOMPLETE"

    def test_a_bare_number_under_the_price_question_takes_its_basis_from_the_question(self):
        question = CommercialQuestion(requested_field="price", expected_basis_unit="TONNE")
        result = self._eval(
            {"product": "maïs", "quantity": 200.0, "unit": "TONNE", "price": 500000.0},
            said={"price": 500000.0},
            text="500000",
            question=question,
        )
        assert result.validation.status == "VALID"
        assert result.offer.pricing.basis == PriceBasis.PER_BASE_UNIT
        assert result.offer.pricing.basis_source == Provenance.QUESTION_CONTEXT_EXPLICIT

    def test_a_package_content_reply_never_changes_the_quantity(self):
        first = self._eval(
            {"product": "lait", "quantity": 50.0, "unit": "LITRE", "price": 500.0, "price_unit": "SAC"},
            said={"price": 500.0, "price_unit": "SAC"},
            text="500f le sachet",
        )
        second = self._eval(
            {
                "product": "lait", "quantity": 50.0, "unit": "LITRE", "price": 500.0, "price_unit": "SAC",
                "commercial_offer": first.offer.to_dict(),
            },
            said={},
            text="0,5 litre",
            question=first.question,
        )
        assert second.validation.status == "VALID"
        assert second.offer.commercial_quantity.amount == 50.0
        assert second.offer.package.content_amount == 0.5
        assert second.offer.package.source == Provenance.QUESTION_CONTEXT_EXPLICIT

    def test_a_product_change_drops_the_previous_package(self):
        first = self._eval(
            {"product": "lait", "quantity": 50.0, "unit": "LITRE", "price": 500.0, "price_unit": "SAC"},
            said={"price": 500.0, "price_unit": "SAC"},
            text="500f le sachet",
        )
        changed = self._eval(
            {
                "product": "boeufs", "quantity": 20.0, "unit": "TETE", "price": 450000.0, "price_unit": "TETE",
                "commercial_offer": first.offer.to_dict(),
            },
            said={"product": "boeufs", "quantity": 20.0, "unit": "TETE", "price": 450000.0, "price_unit": "TETE"},
            text="finalement je vends 20 boeufs à 450000 la tête",
        )
        assert changed.offer.package is None
        assert changed.validation.status == "VALID"

    def test_a_unit_change_revalidates_the_price(self):
        first = self._eval(
            {"product": "maïs", "quantity": 200.0, "unit": "TONNE", "price": 500000.0, "price_unit": "TONNE"},
            said={"price": 500000.0, "price_unit": "TONNE"},
            text="je vends 200 tonnes de maïs à 500000 la tonne",
        )
        assert first.validation.status == "VALID"
        changed = self._eval(
            {
                "product": "maïs", "quantity": 200.0, "unit": "KG", "price": 500000.0, "price_unit": "TONNE",
                "commercial_offer": first.offer.to_dict(),
            },
            said={"quantity": 200.0, "unit": "KG"},
            text="finalement 200 kg",
        )
        assert changed.validation.status == "INCOMPLETE", "500000 par TONNE ne vaut pas 500000 par KG"

    def test_simple_product_is_valid_with_no_question(self):
        result = self._eval(
            {"product": "maïs", "quantity": 100.0, "unit": "KG", "price": 300.0, "price_unit": "KG"},
            said={"product": "maïs", "quantity": 100.0, "unit": "KG", "price": 300.0, "price_unit": "KG"},
            text="je veux vendre 100 kg de maïs à 300 fcfa le kg",
        )
        assert result.validation.status == "VALID" and result.question is None


class TestStaleAliasesAreNulledWhenTheCanonicalIsCleared:
    """`memory_update` replie les alias (`quantite`, `prix`…) sur le canonique ; sans remise à None
    explicite ils survivent dans le canal `merge_dict` et le validateur RESSUSCITE l'ancienne valeur."""

    def test_aliases_of_a_cleared_canonical_are_reset_but_live_ones_are_kept(self):
        from ladini.graphs.agents.market_coach.nodes.memory import _null_stale_aliases

        source = {"quantite": 50.0, "qty": 50.0, "prix": 500.0, "product": "lait", "produit": "lait"}
        payload = {"quantity": None, "price": None, "product": "boeufs"}
        _null_stale_aliases(payload, source)
        assert payload["quantite"] is None and payload["qty"] is None and payload["prix"] is None
        assert "produit" not in payload or payload["produit"] is not None  # canonique renseigné : intact


def test_unit_before_price_is_the_price_basis():
    from ladini.domain.commercial_offer_flow import price_unit_next_to_amount as p

    assert p("j ai 225 kg d oignon;le kg coute 175 fcfa", 175) == "KG"
    assert p("je veux vendre mes 355 kg de tomate;le kg coute 225fcfa", 225) == "KG"
    # l'unité de la QUANTITÉ n'est jamais la base du prix
    assert p("j ai 225 kg a 175", 175) is None
