"""TTL de `PendingInteraction` (Phase 2 hardening, commit 7, décision produit F : 30 minutes
par défaut). Fonction pure testée sans horloge murale — `now` toujours fourni explicitement,
jamais un `datetime.now()`/`time.time()` implicite dans les assertions."""
from __future__ import annotations

from ladini.core.settings import settings
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    PendingInteraction,
    get_pending_interaction,
    is_pending_expired,
    pending_interaction_ttl_seconds,
    set_pending_interaction,
)

_THIRTY_MIN = 1800.0
#: Epoch de référence réaliste pour les tests de frontière — `created_at=0.0` collisionnerait
#: avec le garde "jamais horodaté" de `is_pending_expired` (0.0 est le défaut du dataclass pour
#: une interaction JAMAIS posée ; `set_pending_interaction` n'utilise en pratique jamais cette
#: valeur, `time.time()` n'étant jamais exactement 0.0 en production).
_EPOCH = 1_000_000.0


def _pending_at(created_at: float) -> PendingInteraction:
    return PendingInteraction(kind=InteractionKind.ENTER_FIELD, field="product", created_at=created_at)


class TestIsPendingExpiredBoundary:
    def test_default_ttl_is_thirty_minutes(self):
        assert pending_interaction_ttl_seconds() == _THIRTY_MIN
        assert settings.PENDING_INTERACTION_TTL_SECONDS == _THIRTY_MIN

    def test_just_under_the_ttl_is_still_active(self):
        pending = _pending_at(created_at=_EPOCH)
        assert is_pending_expired(pending, now=_EPOCH + _THIRTY_MIN - 1.0) is False

    def test_exactly_at_the_ttl_is_still_active(self):
        """Politique de frontière documentée dans `is_pending_expired` : strictement `>`, pas
        `>=` — exactement 30:00 est encore ACTIF."""
        pending = _pending_at(created_at=_EPOCH)
        assert is_pending_expired(pending, now=_EPOCH + _THIRTY_MIN) is False

    def test_just_over_the_ttl_is_expired(self):
        pending = _pending_at(created_at=_EPOCH)
        assert is_pending_expired(pending, now=_EPOCH + _THIRTY_MIN + 0.001) is True

    def test_well_over_the_ttl_is_expired(self):
        pending = _pending_at(created_at=_EPOCH)
        assert is_pending_expired(pending, now=_EPOCH + _THIRTY_MIN * 10) is True

    def test_a_custom_ttl_overrides_the_default(self):
        pending = _pending_at(created_at=_EPOCH)
        assert is_pending_expired(pending, now=_EPOCH + 61.0, ttl_seconds=60.0) is True
        assert is_pending_expired(pending, now=_EPOCH + 59.0, ttl_seconds=60.0) is False

    def test_none_kind_never_expires(self):
        assert is_pending_expired(PendingInteraction(), now=10**9) is False

    def test_a_never_timestamped_pending_never_expires(self):
        """`created_at=0.0` (défaut du dataclass, jamais posé par `set_pending_interaction` en
        pratique) ne doit jamais être confondu avec une expiration au tout début de l'epoch —
        traité comme "pas d'horodatage", donc jamais expiré par CE mécanisme."""
        pending = PendingInteraction(kind=InteractionKind.ENTER_FIELD, field="product", created_at=0.0)
        assert is_pending_expired(pending, now=10**9) is False


class TestGetPendingInteractionEnforcesTheTtl:
    """`get_pending_interaction` est LE point de résolution canonique unique (docstring de
    module) — l'expiration doit y être appliquée UNE fois pour que tous les appelants
    (routage, cognitive_guard, tunnel, flows) en bénéficient automatiquement, jamais une
    vérification séparée que certains appelants oublieraient."""

    def test_a_fresh_pending_is_resolved_normally(self):
        state = {
            "current_goal": "CREATE_RECURRING_NEED",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
        }
        resolved = get_pending_interaction(state, now=state["pending_interaction"]["created_at"] + 10.0)
        assert resolved.kind == InteractionKind.ENTER_FIELD
        assert resolved.field == "product"

    def test_an_expired_pending_resolves_to_none(self):
        state = {
            "current_goal": "CREATE_RECURRING_NEED",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
        }
        created_at = state["pending_interaction"]["created_at"]
        resolved = get_pending_interaction(state, now=created_at + _THIRTY_MIN + 1.0)
        assert resolved.kind == InteractionKind.NONE

    def test_an_expired_pending_never_reappears_active_on_a_later_call(self):
        """Invariant §7 : EXPIRED ne redevient jamais ACTIVE — même en rappelant la fonction
        avec un `now` ultérieur encore, la même interaction persistée reste résolue à NONE."""
        state = {
            "current_goal": "CREATE_RECURRING_NEED",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
        }
        created_at = state["pending_interaction"]["created_at"]
        first = get_pending_interaction(state, now=created_at + _THIRTY_MIN + 1.0)
        second = get_pending_interaction(state, now=created_at + _THIRTY_MIN + 999.0)
        assert first.kind == InteractionKind.NONE
        assert second.kind == InteractionKind.NONE
