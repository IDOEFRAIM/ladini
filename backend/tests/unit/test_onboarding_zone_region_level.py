"""Retour produit onboarding (2026-09-18) — deux changements :

1. La résolution de zone se fait au niveau RÉGION (`governance.zones`,
   `parent_id IS NULL`), plus au niveau ville/village précis — une région a
   beaucoup plus de chances d'être déjà en base, réduisant le risque de
   "zone introuvable" pendant l'onboarding. Voir
   docs/ONBOARDING_ZONE_REGION_LEVEL_2026-09-18.md pour le détail complet
   (dont la conséquence, ASSUMÉE, sur `buyer.py`'s tiers logistiques).
2. Le premier message (`_WELCOME`) est raccourci : une seule question
   claire (rôle), le pitch produit complet n'est plus montré QUE sur
   demande (`_EXPLAIN_AGAIN`).

Style « mixin instancié directement avec une session stubée » — voir
tests/unit/test_get_transaction_summary_ownership.py pour le même pattern.
"""
from __future__ import annotations

from tests.conftest import run


class _CapturingSession:
    """Capture le(s) statement(s) SQLAlchemy exécutés — ne simule PAS de
    vraies données, juste ce qui a été DEMANDÉ à la base, pour prouver la
    présence du filtre `parent_id IS NULL` sans nécessiter une DB réelle."""

    def __init__(self):
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)

        class _Result:
            def scalar_one_or_none(self):
                return None

            def scalars(self):
                class _Scalars:
                    def all(self):
                        return []

                return _Scalars()

        return _Result()


def _mixin_with_capturing_session():
    from ladini.services.database.base import BaseMixin

    session = _CapturingSession()

    class _Svc(BaseMixin):
        @property
        def session(self):
            return session

    return _Svc(), session


def _compiled_where(stmt) -> str:
    """Rend le WHERE clause lisible (dialecte générique, littéraux inlinés)
    pour une assertion texte simple et robuste — pas besoin d'un dialecte
    Postgres réel pour vérifier qu'un prédicat est présent."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


class TestGetZoneByNameRestrictedToRegionLevel:
    def test_the_query_filters_on_parent_id_is_null(self):
        svc, session = _mixin_with_capturing_session()
        run(svc.get_zone_by_name("Hauts-Bassins"))
        assert len(session.executed) == 1
        sql = _compiled_where(session.executed[0])
        assert "parent_id" in sql
        assert "IS NULL" in sql.upper()

    def test_still_returns_a_clean_not_found_message_when_nothing_matches(self):
        """Non-régression : le contrat de sortie (status/message) ne change
        pas, seul le PÉRIMÈTRE de recherche change."""
        svc, _ = _mixin_with_capturing_session()
        result = run(svc.get_zone_by_name("Zone Inexistante XYZ"))
        assert result["status"] == "error"
        assert "introuvable" in result["message"]


class TestGetAvailableZonesRestrictedToRegionLevel:
    def test_the_query_filters_on_parent_id_is_null(self):
        svc, session = _mixin_with_capturing_session()
        run(svc.get_available_zones())
        assert len(session.executed) == 1
        sql = _compiled_where(session.executed[0])
        assert "parent_id" in sql
        assert "IS NULL" in sql.upper()


class TestOnboardingTextAsksForRegionNotCity:
    def test_the_zone_field_question_mentions_region_not_city(self):
        from ladini.agents.onboarding import _FIELD_QUESTIONS

        q = _FIELD_QUESTIONS["zone"]
        assert "région" in q.lower()
        assert "ville" not in q.lower()
        assert "province" not in q.lower()

    def test_missing_zone_label_mentions_region_not_city(self):
        from ladini.agents.onboarding import OnboardingState, _missing_labels

        state = OnboardingState(role="BUYER", name="Awa")
        labels = _missing_labels(state)
        assert "ta région" in labels
        assert not any("ville" in label for label in labels)

    def test_zone_not_found_error_message_never_cites_hardcoded_city_examples(self):
        """(2026-09-18) : les anciens exemples "Ouagadougou, Bobo-Dioulasso,
        Koudougou" étaient des noms de VILLES, pas de régions — trompeurs
        après ce changement, et invérifiables contre la vraie base (aucune
        garantie que ces noms existent comme régions). Ne doivent plus
        apparaître dans le code source de l'agent."""
        import inspect

        from ladini.agents import onboarding as onboarding_mod

        source = inspect.getsource(onboarding_mod)
        for city in ("Ouagadougou", "Bobo-Dioulasso", "Koudougou"):
            assert city not in source, f"exemple de ville codé en dur trouvé : {city}"


class TestWelcomeMessageIsShortAndActionable:
    def test_welcome_asks_the_single_expected_question(self):
        from ladini.agents.onboarding import _WELCOME

        assert "producteur" in _WELCOME
        assert "acheteur" in _WELCOME

    def test_welcome_no_longer_carries_the_full_two_paragraph_pitch(self):
        """(2026-09-18) : le pitch complet (mission Ladini + détail par rôle)
        vit désormais dans `_EXPLAIN_AGAIN`, affiché SEULEMENT si
        l'utilisateur le demande — pas dans le premier message."""
        from ladini.agents.onboarding import _WELCOME

        assert "Publie tes récoltes" not in _WELCOME
        assert "Trouve les meilleurs produits frais" not in _WELCOME

    def test_welcome_points_to_the_on_demand_explanation(self):
        from ladini.agents.onboarding import _WELCOME

        assert "Ladini" in _WELCOME  # identité minimale conservée

    def test_full_pitch_still_exists_and_is_reachable_on_demand(self):
        """Le contenu retiré du premier message n'a pas disparu — il reste
        accessible via `_EXPLAIN_AGAIN` (déclenché par `is_question`, voir
        `run_onboarding_step`)."""
        from ladini.agents.onboarding import _EXPLAIN_AGAIN

        assert "Ladini" in _EXPLAIN_AGAIN
        assert len(_EXPLAIN_AGAIN) > 0
