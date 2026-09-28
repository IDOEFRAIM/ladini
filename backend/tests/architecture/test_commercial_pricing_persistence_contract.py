"""Verrous d'architecture — contrat de persistance de la sémantique commerciale (Phase B2a).

A. toute ligne de commande NEUVE porte un snapshot (un seul constructeur) ;
B. tout bid NEUF porte sa base (un seul point d'écriture) ;
C. TOTAL_LOT n'est pas perdu (enum, contraintes DB, domaine) ;
D. une ligne ancienne n'invente jamais de base ;
E. modifier le produit ne peut pas altérer le sens d'une ligne de commande ;
F. PER_PACKAGE exige les métadonnées du conditionnement (domaine ET contraintes DB) ;
G. la migration est ADDITIVE (rien d'existant n'est supprimé, modifié ou rendu obligatoire).
"""
from __future__ import annotations

import ast
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ladini.domain import commercial_pricing_snapshot as cps
from ladini.domain.commercial_offer import PriceBasis

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "ladini"
DB_SERVICES = SRC / "services" / "database"
MIGRATION = BACKEND / "schema_contract" / "migrations" / "0012_commercial_pricing_persistence.sql"


def _calls(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if called == name:
                cur = node
                while cur in parents and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cur = parents[cur]
                yield node, cur if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _source_of(path: Path, fn_node) -> str:
    return ast.get_source_segment(path.read_text(encoding="utf-8"), fn_node) or ""


# ------------------------------------------------------------------ A
class TestA_EveryNewOrderItemHasASnapshot:
    def test_every_order_item_constructor_call_goes_through_a_snapshot_builder(self):
        offenders, seen = [], 0
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "models.py" or "tests" in path.parts:
                continue
            for call, fn in _calls(path, "OrderItem"):
                seen += 1
                src = _source_of(path, fn) if fn is not None else ""
                if "order_item_snapshot_columns(" not in src and "declared_sale_snapshot_columns(" not in src:
                    offenders.append(f"{path.relative_to(SRC)}:{call.lineno}")
        assert seen >= 4, "les 4 sites de création connus (achat direct, précommande, allocation, vente déclarée)"
        assert not offenders, f"OrderItem créé sans snapshot immuable : {offenders}"

    def test_a_single_builder_composes_the_snapshot(self):
        defs = [
            p.relative_to(SRC).as_posix()
            for p in SRC.rglob("*.py")
            if "def build_order_item_pricing_snapshot" in p.read_text(encoding="utf-8")
        ]
        assert defs == ["domain/commercial_pricing_snapshot.py"]


# ------------------------------------------------------------------ B
class TestB_EveryNewBidHasABasisOrIsExplicitlyLegacy:
    def test_bid_rows_are_only_created_in_place_bid_with_pricing_columns(self):
        sites = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "models.py":
                continue
            for call, fn in _calls(path, "Bid"):
                sites.append((path.name, fn.name if fn else None, call))
        assert [(f, n) for f, n, _ in sites] == [("auction.py", "place_bid")]
        _, _, call = sites[0]
        assert any(kw.arg is None and getattr(kw.value, "id", None) == "pricing_columns" for kw in call.keywords)

    def test_place_bid_certifies_the_basis_when_given_and_logs_when_absent(self):
        src = (DB_SERVICES / "auction.py").read_text(encoding="utf-8")
        assert "bid_snapshot_columns(" in src and "BID_PRICE_BASIS_UNSPECIFIED" in src

    def test_bid_price_updates_never_write_offered_price_alone(self):
        src = (DB_SERVICES / "auction.py").read_text(encoding="utf-8")
        assert not re.search(r"\.offered_price\s*=\s*float\(", src), "mise à jour du montant sans recalcul du snapshot"


# ------------------------------------------------------------------ C
class TestC_TotalLotIsNotLost:
    def test_total_lot_is_a_first_class_basis_in_domain_and_database(self):
        assert PriceBasis.TOTAL_LOT.value in cps.CERTIFIED_BASES
        sql = MIGRATION.read_text(encoding="utf-8")
        assert sql.count("'TOTAL_LOT'") >= 3  # order_items, bids (x2)

    def test_the_order_award_snapshot_and_product_field_exist(self):
        from ladini.domain.models import Order, Product

        assert hasattr(Order, "award_pricing_snapshot") and hasattr(Product, "commercial_pricing")

    def test_award_total_does_not_depend_on_the_rounded_normalization(self):
        from decimal import Decimal

        s = cps.CommercialPricingSnapshot(
            Decimal("1000000"), PriceBasis.TOTAL_LOT,
            inventory_quantity_amount=Decimal("3"), inventory_quantity_unit="KG",
        ).with_normalized()
        assert s.normalized_unit_price == Decimal("333333.3333")
        assert s.total_for(3, "KG") == Decimal("1000000.00")


# ------------------------------------------------------------------ D
class TestD_LegacyRowsNeverInventABasis:
    def test_views_of_rows_without_a_snapshot_have_no_basis(self):
        from types import SimpleNamespace

        assert cps.bid_pricing_view(SimpleNamespace(offered_price=1, offered_price_basis=None, pricing_snapshot_version=None)).basis is None
        assert cps.order_item_pricing_view(SimpleNamespace(price_at_sale=1, pricing_snapshot_version=None)).basis is None
        assert cps.product_pricing_view(SimpleNamespace(price=1, commercial_pricing=None)).basis is None

    def test_the_migration_does_not_backfill_or_default_any_basis(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        assert not re.search(r"^\s*UPDATE\s+\S+\s+SET\b", sql, re.IGNORECASE | re.MULTILINE)
        assert not re.search(r"ADD COLUMN[^;]*\bDEFAULT\b", sql, re.IGNORECASE)


# ------------------------------------------------------------------ E
class TestE_ProductMutationCannotAlterOrderMeaning:
    def test_the_snapshot_module_is_pure_and_frozen(self):
        src = (SRC / "domain" / "commercial_pricing_snapshot.py").read_text(encoding="utf-8")
        imports = re.findall(r"^(?:from|import)\s+([\w.]+)", src, re.MULTILINE)
        assert not any(i.startswith(("sqlalchemy", "ladini.services", "ladini.graphs")) for i in imports)
        with pytest.raises(FrozenInstanceError):
            cps.CommercialPricingSnapshot(cps.to_decimal(1), PriceBasis.TOTAL_LOT).currency = "EUR"  # type: ignore[misc]

    def test_the_database_freezes_order_item_and_award_snapshots(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        assert "order_items_snapshot_immutable_trg" in sql and "orders_award_snapshot_immutable_trg" in sql
        assert "BEFORE UPDATE ON marketplace.order_items" in sql


# ------------------------------------------------------------------ F
class TestF_PerPackageRequiresPackageMetadata:
    @pytest.mark.parametrize("table_check", ["order_items_snapshot_chk", "bids_snapshot_chk"])
    def test_database_constraint_mentions_the_package_requirements(self, table_check):
        sql = MIGRATION.read_text(encoding="utf-8")
        block = next(line for line in sql.splitlines() if f'CONSTRAINT "{table_check}"' in line)
        for needle in ("PER_PACKAGE", "package_type", "package_content_amount", "package_content_unit"):
            assert needle in block, (table_check, needle)

    def test_domain_rejects_the_same_combinations(self):
        from decimal import Decimal

        assert cps.CommercialPricingSnapshot(Decimal("500"), PriceBasis.PER_PACKAGE).issues()


# ------------------------------------------------------------------ G
class TestG_MigrationIsAdditiveOnly:
    def test_only_additive_statements(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        statements = [s.strip() for s in sql.split("--> statement-breakpoint") if s.strip()]
        allowed = re.compile(
            r"^(?:--[^\n]*\n)*\s*(ALTER TABLE \"[\w]+\"\.\"[\w]+\" ADD (?:COLUMN|CONSTRAINT)|CREATE OR REPLACE FUNCTION|CREATE TRIGGER)",
            re.MULTILINE,
        )
        offenders = [s[:90] for s in statements if not allowed.match(s)]
        assert not offenders, offenders

    def test_no_destructive_or_tightening_statement(self):
        sql = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))
        pattern = re.compile(r"\bDROP\b|\bRENAME\b|SET NOT NULL|ALTER COLUMN|\bTRUNCATE\b|DELETE FROM", re.IGNORECASE)
        assert not pattern.search(sql), pattern.search(sql)

    def test_every_added_column_is_nullable_without_default(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        adds = re.findall(r'ADD COLUMN "[\w]+" [^;]*;', sql)
        assert len(adds) == 23
        for stmt in adds:
            assert "NOT NULL" not in stmt and "DEFAULT" not in stmt, stmt
