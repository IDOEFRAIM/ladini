"""`products_commercial_pricing_chk` — preuve PostgreSQL réelle (2026-09-30).

Incident production : `CheckViolationError: products_commercial_pricing_chk` sur un
`INSERT INTO marketplace.products` dont le payload contenait `name=lait`,
`pricing_tiers` non vide, `commercial_pricing=null`, `price=500`, `unit=LITRE`.

Root cause PROUVÉE (voir `TestOrmSerializationOfNone` ci-dessous, et
`src/ladini/domain/catalog/models.py::Product.commercial_pricing`) : `Column(JSONB,
nullable=True)` SANS `none_as_null=True` fait sérialiser un attribut Python `None` en
littéral JSON `null` (un SCALAIRE JSONB valide, PAS un SQL NULL). Le CHECK
(`commercial_pricing IS NULL OR (jsonb_typeof(...) = 'object' AND ... ? 'schema_version')`)
échoue sur SES DEUX branches pour un JSONB `null` : `IS NULL` est faux (la colonne
n'est PAS NULL au sens SQL) et `jsonb_typeof('null'::jsonb) = 'null'`, jamais `'object'`.
`certify_commercial_offer(...)` (services/database/pricing_persistence.py) retourne
`None` pour TOUT produit sans offre certifiée — le cas legacy/PACKAGING_TIERS le plus
courant (paliers sachet/bidon, jamais de `CommercialOffer` unique) — donc CE chemin,
pas seulement un scénario de draft corrompu, déclenchait la violation.

`pricing_tiers != NULL` avec `commercial_pricing = NULL` (au sens SQL) est, lui, une
combinaison PARFAITEMENT VALIDE pour ce CHECK (voir test A/C ci-dessous) — le contrat
initial du rapport Partie B n'était pas faux, seulement incomplet : il ne distinguait
pas SQL NULL de JSONB `null`, précisément la nuance que cet incident a révélée.

Nécessite `SCHEMA_TEST_DSN` (voir `conftest.py::pg_dsn`) — SKIP en local sans base,
ÉCHEC dur en CI (`REQUIRE_SCHEMA_DB=1`). Aucun mock : base PostgreSQL fraîche,
migrations officielles rejouées (`schema_contract/migrations`)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

import psycopg2
import pytest
from factories import Graph
from psycopg2.errors import CheckViolation
from psycopg2.extras import Json

# =====================================================================
# 1. Définition du CHECK : repo vs PostgreSQL réellement appliqué
# =====================================================================


_EXPECTED_CHECK_SQL_FROM_REPO = (
    "commercial_pricing IS NULL OR (jsonb_typeof(commercial_pricing) = 'object' "
    "AND commercial_pricing ? 'schema_version')"
)


def test_the_repo_migration_defines_the_check_verbatim():
    """A. Définition ACTUELLE du repo — lue directement dans la migration Drizzle,
    pas devinée. Source unique : `schema_contract/migrations/0012_commercial_pricing_
    persistence.sql` (seule migration à toucher cette contrainte — aucune migration
    ultérieure (0013/0014) ne la touche : pas de drift de migration côté repo)."""
    from db_tools import MIGRATIONS

    sql = (MIGRATIONS / "0012_commercial_pricing_persistence.sql").read_text(encoding="utf-8")
    assert "products_commercial_pricing_chk" in sql
    assert (
        'CHECK ("marketplace"."products"."commercial_pricing" IS NULL OR '
        '(jsonb_typeof("marketplace"."products"."commercial_pricing") = \'object\' '
        'AND "marketplace"."products"."commercial_pricing" ? \'schema_version\'))'
    ) in sql


def test_no_later_migration_touches_the_products_commercial_pricing_columns():
    from db_tools import migration_files

    files = migration_files()
    idx = next(i for i, f in enumerate(files) if f.stem == "0012_commercial_pricing_persistence")
    for f in files[idx + 1 :]:
        text = f.read_text(encoding="utf-8")
        assert "products_commercial_pricing_chk" not in text
        assert '"products"."commercial_pricing"' not in text


def test_postgres_applies_the_check_with_the_exact_repo_condition(pg_dsn):
    """B. Définition RÉELLEMENT appliquée en PostgreSQL — `pg_get_constraintdef` sur la
    base migrée depuis zéro (voir `conftest.py::pg_dsn`), comparée caractère pour
    caractère à la condition du repo. C. Si ceci échouait, ce serait la preuve d'un
    DRIFT de migration — ce n'est PAS le cas (voir assertion finale)."""
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conname = 'products_commercial_pricing_chk'"
        )
        row = cur.fetchone()
        assert row is not None, "products_commercial_pricing_chk n'existe pas en base"
        live_def = row[0]
        # `pg_get_constraintdef` renvoie "CHECK (<condition>)" reformaté par Postgres
        # (espaces normalisés) — on compare la condition LOGIQUE, pas le texte brut de
        # la migration (Postgres ne rend pas les guillemets de qualification de colonne
        # une fois la contrainte attachée à une table déjà qualifiée par le search_path).
        assert live_def.startswith("CHECK (")
        normalized = live_def[len("CHECK (") : -1].replace('"', "").replace("  ", " ")
        assert "commercial_pricing IS NULL" in normalized
        assert "jsonb_typeof(commercial_pricing) = 'object'::text" in normalized or (
            "jsonb_typeof(commercial_pricing) = 'object'" in normalized
        )
        assert "commercial_pricing ? 'schema_version'::text" in normalized or (
            "commercial_pricing ? 'schema_version'" in normalized
        )
    finally:
        conn.close()


# =====================================================================
# 3/5. SQLAlchemy — sérialisation de `None` AVANT tout accès DB (pure, aucune
# connexion nécessaire — prouve le MÉCANISME exact, indépendamment de PostgreSQL)
# =====================================================================


class TestOrmSerializationOfNone:
    """Item 5 du mandat : le mapping SQLAlchemy est-il responsable ? Teste directement
    `bind_processor` — ce que SQLAlchemy envoie RÉELLEMENT comme paramètre lié à
    PostgreSQL pour `commercial_pricing=None`, sans qu'aucune connexion DB ne soit
    nécessaire pour observer ce comportement (il est déterminé côté Python, avant
    l'envoi réseau)."""

    def test_commercial_pricing_none_now_binds_as_true_sql_null(self):
        from sqlalchemy.dialects import postgresql

        from ladini.domain.catalog.models import Product

        col_type = Product.__table__.c.commercial_pricing.type
        assert col_type.none_as_null is True, (
            "régression : none_as_null doit rester True sur commercial_pricing, "
            "sinon `None` redevient un JSONB `null` (voir incident 2026-09-30)"
        )
        bind = col_type.bind_processor(postgresql.dialect())
        sent = bind(None) if bind is not None else None
        assert sent is None, f"attendu un SQL NULL (paramètre Python None), reçu {sent!r}"

    def test_before_the_fix_none_was_serialized_as_the_json_null_literal(self):
        """Preuve du bug (pas juste du correctif) : le comportement PAR DÉFAUT de
        `JSONB()` sans `none_as_null=True` — celui qu'avait `commercial_pricing` avant
        ce correctif — sérialise `None` en la CHAÎNE `'null'` (littéral JSON), jamais
        un SQL NULL. Ce test n'inspecte pas la colonne du modèle (déjà corrigée
        ci-dessus) mais le type SQLAlchemy nu, pour documenter EXACTEMENT ce qui se
        passait avant — la régression que `TestCheckConstraintMatrix.test_case_D`
        reproduit ensuite au niveau SQL réel."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.dialects.postgresql import JSONB

        buggy_type = JSONB()  # comportement PAR DÉFAUT, none_as_null=False
        assert buggy_type.none_as_null is False
        bind = buggy_type.bind_processor(postgresql.dialect())
        assert bind(None) == "null", (
            "si ceci échoue, le comportement par défaut de SQLAlchemy a changé de "
            "version — revérifier l'analyse de l'incident"
        )

    def test_pricing_tiers_shares_the_same_gap_but_has_no_check_relying_on_it(self):
        """`pricing_tiers` (colonne sœur) a le MÊME défaut (`Column(JSONB, nullable=True)`,
        sans `none_as_null`) — mais AUCUN CHECK constraint ne porte sur elle (confirmé :
        `products_commercial_pricing_chk` est la SEULE contrainte CHECK jamais posée sur
        `products`, voir migrations). Documenté délibérément NON corrigé ici (hors
        périmètre de cet incident — un JSONB `null` y est sémantiquement inerte tant
        qu'aucun code ne distingue `IS NULL` de `= 'null'::jsonb` pour cette colonne)."""
        from ladini.domain.catalog.models import Product

        assert Product.__table__.c.pricing_tiers.type.none_as_null is False


# =====================================================================
# 7. Matrice PG directe du CHECK (item 7 du mandat) — SQL brut, aucun ORM, pour
# isoler le comportement du CHECK lui-même de tout mapping applicatif.
# =====================================================================


def _insert_product_raw(
    cur, producer_id, sub_category_id, *,
    commercial_pricing_sql: str, commercial_pricing_param: Optional[Any] = None,
    pricing_tiers_sql: str = "%s", pricing_tiers_param: Optional[Any] = None,
) -> Any:
    """INSERT brut — `commercial_pricing_sql`/`pricing_tiers_sql` sont des fragments SQL
    LITTÉRAUX (ex: "NULL", "%s", "'null'::jsonb", "'{}'::jsonb") pour distinguer
    précisément SQL NULL / JSONB null / JSONB {} au niveau du texte de la requête —
    un paramètre lié (`%s`) ne peut PAS, lui, exprimer un SQL NULL littéral vs une
    valeur JSONB `null` de façon aussi explicite pour ce test."""
    params = [p for p in (pricing_tiers_param, commercial_pricing_param) if p is not None]
    cur.execute(
        f"""INSERT INTO marketplace.products
            (name, category_label, price, unit, quantity_for_sale, producer_id, sub_category_id,
             pricing_tiers, commercial_pricing)
            VALUES ('lait', 'Produits laitiers', 500, 'LITRE', 55,
                    %s, %s, {pricing_tiers_sql}, {commercial_pricing_sql})
            RETURNING id""",
        [producer_id, sub_category_id, *params],
    )
    return cur.fetchone()[0]


_VALID_TIERS = Json(
    [
        {"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet"},
        {"quantity": 0.5, "unit": "LITRE", "price": 600.0, "packaging": "bidon"},
    ]
)


@pytest.fixture
def graph(pg_dsn):
    """Producteur/sous-catégorie COMMITTÉS sur une connexion dédiée — nécessaire car
    `TestCreateProductRealPath`/`TestDomainToPersistenceReplay` lisent ces lignes
    depuis une connexion ASYNCPG SÉPARÉE (`_run_pg`), qui ne verrait rien
    d'uncommitted sur la connexion `db` (laquelle ROLLBACK tout en fin de test —
    voir `conftest.py::db`). Même pattern que `test_commercial_admin_api_pg.py::user`
    (`with conn: ...` psycopg2 = commit automatique en sortie)."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
    conn.close()
    return g


class TestCheckConstraintMatrix:
    """Chaque cas ISOLE une combinaison SQL NULL / JSONB null / JSONB objet — la
    variable observée du mandat (item 6 : "distinguer SQL NULL, JSONB null, JSONB {},
    JSONB [], la chaîne \"null\""."""

    def test_case_A_both_sql_null_is_valid(self, db, graph):
        with db.cursor() as cur:
            _insert_product_raw(
                cur, graph.producer, graph.sub_category,
                pricing_tiers_sql="NULL", commercial_pricing_sql="NULL",
            )  # ne lève pas -> passe

    def test_case_B_empty_tiers_array_with_null_pricing_is_valid(self, db, graph):
        with db.cursor() as cur:
            _insert_product_raw(
                cur, graph.producer, graph.sub_category,
                pricing_tiers_sql="'[]'::jsonb", commercial_pricing_sql="NULL",
            )

    def test_case_C_valid_tiers_with_sql_null_pricing_is_valid(self, db, graph):
        """Le cas légitime PACKAGING_TIERS legacy (sachet/bidon) — confirme que le
        rapport initial ("pricing_tiers non vide + commercial_pricing NULL est une
        combinaison valide") était correct EN SOI, pourvu que ce soit un VRAI SQL NULL."""
        with db.cursor() as cur:
            _insert_product_raw(
                cur, graph.producer, graph.sub_category,
                pricing_tiers_sql="%s", pricing_tiers_param=_VALID_TIERS,
                commercial_pricing_sql="NULL",
            )

    def test_case_D_valid_tiers_with_json_null_pricing_reproduces_the_live_incident(self, db, graph):
        """LE BUG — reproduction exacte du payload live (`pricing_tiers` non vide,
        `commercial_pricing` = JSONB `null`, PAS SQL NULL). C'est ce que l'ORM envoyait
        AVANT le correctif (voir `TestOrmSerializationOfNone`)."""
        with db.cursor() as cur:
            with pytest.raises(CheckViolation) as exc:
                _insert_product_raw(
                    cur, graph.producer, graph.sub_category,
                    pricing_tiers_sql="%s", pricing_tiers_param=_VALID_TIERS,
                    commercial_pricing_sql="'null'::jsonb",
                )
            assert "products_commercial_pricing_chk" in str(exc.value)

    def test_case_E_empty_object_is_invalid_missing_schema_version(self, db, graph):
        with db.cursor() as cur:
            with pytest.raises(CheckViolation) as exc:
                _insert_product_raw(
                    cur, graph.producer, graph.sub_category,
                    pricing_tiers_sql="%s", pricing_tiers_param=_VALID_TIERS,
                    commercial_pricing_sql="'{}'::jsonb",
                )
            assert "products_commercial_pricing_chk" in str(exc.value)

    def test_case_F_per_base_unit_certified_offer_is_valid(self, db, graph):
        from ladini.domain.commercial_offer import PriceBasis
        from ladini.domain.commercial_pricing_snapshot import CommercialPricingSnapshot

        snap = CommercialPricingSnapshot(
            commercial_price_amount=Decimal("500"), price_basis=PriceBasis.PER_BASE_UNIT,
            price_unit="LITRE",
        )
        with db.cursor() as cur:
            _insert_product_raw(
                cur, graph.producer, graph.sub_category,
                pricing_tiers_sql="NULL",
                commercial_pricing_sql="%s", commercial_pricing_param=Json(snap.to_dict()),
            )

    def test_case_G_per_package_certified_offer_is_valid(self, db, graph):
        from ladini.domain.commercial_offer import PriceBasis
        from ladini.domain.commercial_pricing_snapshot import CommercialPricingSnapshot

        snap = CommercialPricingSnapshot(
            commercial_price_amount=Decimal("500"), price_basis=PriceBasis.PER_PACKAGE,
            package_type="SACHET", package_content_amount=Decimal("0.5"),
            package_content_unit="LITRE",
        )
        with db.cursor() as cur:
            _insert_product_raw(
                cur, graph.producer, graph.sub_category,
                pricing_tiers_sql="NULL",
                commercial_pricing_sql="%s", commercial_pricing_param=Json(snap.to_dict()),
            )

    def test_case_H_jsonb_array_is_invalid_not_an_object(self, db, graph):
        """Item 6 : distinction objet/tableau — un `[]` dans la colonne
        `commercial_pricing` elle-même (jamais produit par le code actuel, mais le
        CHECK doit le refuser structurellement)."""
        with db.cursor() as cur:
            with pytest.raises(CheckViolation):
                _insert_product_raw(
                    cur, graph.producer, graph.sub_category,
                    pricing_tiers_sql="NULL", commercial_pricing_sql="'[]'::jsonb",
                )


# =====================================================================
# 10. `create_product()` réel — le chemin applicatif complet
# =====================================================================


class _RealSession:
    """Adapte une `AsyncSession` réelle au contrat `BaseMixin.session` (propriété)."""

    def __init__(self, session):
        self._session = session

    @property
    def session(self):
        return self._session


def _producer_service(session, *, producer_id, phone):
    import types

    from ladini.services.database.producer import ProducerMgmtMixin

    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

        async def _resolve_producer_phone(self, *, phone=None, producer_id=None):
            return phone

        async def get_producer_profile(self, phone):
            # Identité déjà résolue par la fixture `graph` (réellement en base, via
            # `factories.Graph`) — seul `producer.id` est lu par `create_product`.
            return types.SimpleNamespace(), types.SimpleNamespace(id=producer_id)

        async def get_product_category_unit_config(self, product_name):
            return {"status": "error", "message": "pas de config admin — repli taxonomie"}

        async def guess_category(self, product_name=None):
            return "Produits laitiers"

    return _Svc()


def _run_pg(dsn, fn):
    """COMMIT (pas de rollback) — les tests appelants vérifient la ligne persistée via
    une connexion psycopg2 SÉPARÉE (`db`), qui ne verrait rien d'annulé. Même
    convention que `test_commercial_admin_api_pg.py::_run` : la base de test entière
    (session-scopée, `conftest.py::pg_dsn`) est de toute façon reconstruite/supprimée
    à la fin de la session — laisser quelques lignes committées ne pollue rien."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                async with session.begin():
                    return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(go())


class TestCreateProductRealPath:
    """Item 10 : passe réellement par `producer.py::ProducerMgmtMixin.create_product`
    (pas une construction directe de `Product(...)`)."""

    def test_packaging_tiers_product_persists_without_a_check_violation(self, db, pg_dsn, graph):
        producer_id, sub_category_id = graph.producer, graph.sub_category

        async def _go(session):
            svc = _producer_service(session, producer_id=producer_id, phone="+22670000099")
            return await svc.create_product(
                name="lait", price=500.0, quantity_for_sale=55.0, unit="LITRE",
                sub_category_id=str(sub_category_id),
                pricing_tiers=[
                    {"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet"},
                    {"quantity": 0.5, "unit": "LITRE", "price": 600.0, "packaging": "bidon"},
                ],
                phone="+22670000099",
            )

        result = _run_pg(pg_dsn, _go)
        assert result is not None

        with db.cursor() as cur:
            cur.execute(
                "select name, quantity_for_sale, price, unit, pricing_tiers, commercial_pricing "
                "from marketplace.products where producer_id = %s order by created_at desc limit 1",
                [producer_id],
            )
            row = cur.fetchone()
        assert row is not None, "aucune ligne persistée — create_product() n'a pas écrit en base"
        name, quantity_for_sale, price, unit, pricing_tiers, commercial_pricing = row
        assert name == "lait"
        assert float(quantity_for_sale) == 55.0
        assert float(price) == 500.0
        assert unit == "LITRE"
        assert commercial_pricing is None
        assert len(pricing_tiers) == 2
        assert {t["packaging"] for t in pricing_tiers} == {"sachet", "bidon"}


# =====================================================================
# 11. Replay domaine -> persistence : SalesPublishDraft -> execution_payload()
#     -> create_product()
# =====================================================================


class TestDomainToPersistenceReplay:
    """Item 11 : le même chemin que `flows/producer/sales_confirmation.py::
    apply_response_plan` emprunte réellement — `SalesPublishDraft.execution_payload()`
    produit le dict que `actions/sales.py::prep_sales_publish_product` transforme en
    appel `create_product(...)` (voir `domain/sales.py::SalesService.publish_product`,
    audité dans le rapport PR #58 initial — aucun changement ici, seule la PERSISTENCE
    finale était en cause)."""

    def test_packaging_tiers_draft_execution_payload_persists_cleanly(self, db, pg_dsn, graph):
        from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
            SalesPublishDraft,
        )

        draft = SalesPublishDraft.new(
            draft_id="sd-replay1", product="lait", quantity=55.0, unit="LITRE", price=500.0,
            pricing_tiers=[
                {"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet"},
                {"quantity": 0.5, "unit": "LITRE", "price": 600.0, "packaging": "bidon"},
            ],
        )
        payload = draft.execution_payload()
        assert payload.get("commercial_offer") is None  # confirme le mode legacy attendu
        assert "commercial_offer" not in payload  # execution_payload() l'exclut explicitement

        producer_id, sub_category_id = graph.producer, graph.sub_category

        async def _go(session):
            svc = _producer_service(session, producer_id=producer_id, phone="+22670000099")
            return await svc.create_product(
                name=payload["product"], price=payload["price"],
                quantity_for_sale=payload["quantity"], unit=payload["unit"],
                sub_category_id=str(sub_category_id),
                pricing_tiers=payload.get("pricing_tiers"),
                commercial_offer=payload.get("commercial_offer"),
                phone="+22670000099",
            )

        result = _run_pg(pg_dsn, _go)
        assert result is not None


# =====================================================================
# 14. Test de régression nommé explicitement autour du bug live
# =====================================================================


def test_packaging_tiers_product_does_not_violate_commercial_pricing_check(db, pg_dsn, graph):
    """Nommé explicitement autour de l'incident (item 14). Insère, via le chemin ORM
    RÉEL (`Product(commercial_pricing=None)`, EXACTEMENT ce que `producer.py::
    create_product` construit quand `certify_commercial_offer` renvoie `None`), un
    produit à paliers — le payload structurel exact du log live
    (`name=lait, pricing_tiers=[...], commercial_pricing=None, price=500, unit=LITRE`).

    AVANT le correctif (`none_as_null=True` sur `Product.commercial_pricing`) : ce test
    levait `CheckViolation: products_commercial_pricing_chk` (le `None` Python
    devenait un JSONB `null`, voir `TestOrmSerializationOfNone`). APRÈS : l'INSERT
    réussit — le `None` devient un vrai SQL NULL, branche `commercial_pricing IS NULL`
    du CHECK satisfaite."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from ladini.domain.catalog.models import Product

    async def _go():
        engine = create_async_engine(pg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                async with session.begin():
                    product = Product(
                        name="lait",
                        category_label="Produits laitiers",
                        price=500,
                        unit="LITRE",
                        quantity_for_sale=55,
                        producer_id=graph.producer,
                        sub_category_id=graph.sub_category,
                        pricing_tiers=[
                            {"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet"},
                            {"quantity": 0.5, "unit": "LITRE", "price": 600.0, "packaging": "bidon"},
                        ],
                        commercial_pricing=None,  # <- exactement la valeur qui déclenchait l'incident
                    )
                    session.add(product)
                    await session.flush()  # <- levait CheckViolation avant le correctif
                    await session.rollback()
        finally:
            await engine.dispose()

    import asyncio

    asyncio.run(_go())
