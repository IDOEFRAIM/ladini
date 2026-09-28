"""Verrous d'architecture — Bid -> Award (Phase B2b).

1. le prix d'un bid n'est plus jamais « le premier nombre du texte » ;
2. tout appel d'écriture d'un prix de bid depuis la conversation transporte sa BASE ;
3. l'attribution n'a qu'UN point d'exécution, qui envoie les termes CONFIRMÉS ;
4. la décision d'attribution n'est construite que côté serveur (sous verrou), jamais recomposée ailleurs ;
5. aucune liste de bids n'affiche `offered_price` avec l'unité de l'enchère ;
6. la migration 0013 gèle TOUS les champs économiques d'une ligne portant un snapshot (option A)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "ladini"
MC = SRC / "graphs" / "agents" / "market_coach"
AUCTIONS_FLOW = MC / "flows" / "producer" / "auctions.py"
DB_AUCTION = SRC / "services" / "database" / "auction.py"
MIGRATION_0013 = BACKEND / "schema_contract" / "migrations" / "0013_order_item_economic_freeze.sql"


def _calls(path: Path, attr: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr:
            yield node


class TestPriceIsNeverTheFirstNumberOfTheText:
    def test_the_lexical_helpers_that_defined_offered_price_are_gone(self):
        src = AUCTIONS_FLOW.read_text(encoding="utf-8")
        assert "def _first_number" not in src and "def _price_from_answer" not in src
        assert "_NUM_RE" not in src

    def test_the_producer_flow_certifies_prices_with_the_domain_module(self):
        src = AUCTIONS_FLOW.read_text(encoding="utf-8")
        assert "parse_bid_price(" in src and "resolve_basis_reply(" in src


class TestEveryConversationalBidWriteCarriesItsBasis:
    def test_place_bid_and_update_bid_price_calls_pass_price_basis(self):
        for attr in ("place_bid", "update_bid_price"):
            calls = list(_calls(AUCTIONS_FLOW, attr))
            assert calls, f"{attr} n'est plus appelé depuis le flux producteur ?"
            for call in calls:
                assert any(kw.arg == "price_basis" for kw in call.keywords), f"{attr} sans price_basis"

    def test_the_server_refuses_a_new_bid_without_a_basis(self):
        src = DB_AUCTION.read_text(encoding="utf-8")
        assert "price_basis_required" in src

    def test_the_generic_sales_service_place_bid_cannot_bypass_the_certification(self):
        """`SalesService.place_bid` (chemin d'exécuteur générique) ne transporte AUCUN contrat de prix : il ne doit
        pas pouvoir écrire un bid — le tool serveur le refuse sans `price_basis`."""
        src = (MC / "domain" / "sales.py").read_text(encoding="utf-8")
        block = src[src.index("    def place_bid("):src.index("    def update_product(")]
        assert "price_basis" not in block  # aucun chemin ne fabrique une base ici…
        assert "offered_price" in block  # …donc le tool serveur (qui exige `price_basis`) le rejettera


class TestSingleAwardExecutionPoint:
    def test_execute_award_is_the_only_caller_and_sends_the_confirmed_terms(self):
        callers = sorted(
            p.relative_to(MC).as_posix()
            for p in MC.rglob("*.py")
            if re.search(r"\.select_winning_bid\(", p.read_text(encoding="utf-8"))
        )
        assert callers == ["flows/buyer/award_decision.py"]
        src = (MC / "flows" / "buyer" / "award_decision.py").read_text(encoding="utf-8")
        assert "expected_award=" in src and "decision.idempotency_key" in src

    def test_neither_tunnel_selects_a_winner_on_a_bare_line_number(self):
        for rel in ("flows/buyer/negotiation.py", "flows/buyer/order_tracking.py"):
            src = (MC / rel).read_text(encoding="utf-8")
            assert "execute_award(" in src and "lookup_award(" in src, rel


class TestAwardDecisionIsBuiltServerSide:
    def test_build_award_decision_is_only_called_from_the_persistence_bridge(self):
        callers = []
        for p in SRC.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            if "build_award_decision(" in text and p.name not in {"bid_award.py"}:
                callers.append(p.relative_to(SRC).as_posix())
        assert callers == ["services/database/pricing_persistence.py"]

    def test_select_winning_bid_uses_the_decision_for_the_total_and_the_frozen_snapshot(self):
        src = DB_AUCTION.read_text(encoding="utf-8")
        assert "award_decision_for(bid, auction" in src
        assert "decision.award_total" in src and "decision.frozen_snapshot()" in src
        assert not re.search(r"offered_price\s*\*\s*auction\.quantity", src), "total « prix × quantité » supposé"


class TestNoBidListShowsOfferedPriceWithTheAuctionUnit:
    def test_no_f_string_pairs_offered_price_with_a_unit(self):
        src = DB_AUCTION.read_text(encoding="utf-8")
        assert not re.search(r"\{bid\.offered_price\}\s*(?:CFA|FCFA)", src)
        assert "pricing_label" in src

    def test_buyer_menus_use_the_bids_own_label(self):
        for rel in ("flows/buyer/negotiation.py", "flows/buyer/order_tracking.py"):
            assert "pricing_label" in (MC / rel).read_text(encoding="utf-8"), rel


class TestOrderItemEconomicFieldsAreFrozenWithTheSnapshot:
    def test_migration_0013_guards_every_economic_and_snapshot_column(self):
        sql = MIGRATION_0013.read_text(encoding="utf-8")
        for column in (
            "quantity", "base_unit_quantity", "price_at_sale", "tier_id", "product_id", "order_id",
            "pricing_snapshot_version", "quantity_unit", "commercial_price_amount", "price_basis", "price_unit",
            "package_type", "package_content_amount", "package_content_unit", "normalized_unit_price",
            "normalized_unit", "currency",
        ):
            assert re.search(rf"NEW\.{column}\s+IS DISTINCT FROM OLD\.{column}", sql), column

    def test_migration_0013_only_replaces_the_trigger_function(self):
        sql = re.sub(r"--[^\n]*", "", MIGRATION_0013.read_text(encoding="utf-8"))
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        assert len(statements) == 1 or sql.count("CREATE OR REPLACE FUNCTION") == 1
        assert not re.search(r"\bDROP\b|\bALTER\b|\bDELETE\b|\bUPDATE\b\s+\w", sql.replace("BEFORE UPDATE", ""))
