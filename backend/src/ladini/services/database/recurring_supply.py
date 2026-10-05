"""RecurringSupplyMixin — approvisionnement récurrent (Phase 2).

Trois opérations, mandat §12 : `create_recurring_need`, `update_recurring_need`,
`list_my_recurring_needs`. Même conventions que `AuctionMixin` (résolution acheteur/produit,
`self.session` fourni par le service composé, exceptions `BusinessRuleException`) — pas de 4ᵉ manière
de faire, pas de couche `repository.py` séparée (aucun mixin de ce paquet n'en a une).
`create_recurring_needs` (pluriel, chantier multi-produits 2026-09-23) est une variante de
`create_recurring_need` — même produit unitaire (`_insert_one_recurring_need`), appelée N fois pour N
produits partageant la même récurrence, jamais une 4ᵉ opération distincte.

## Ownership (mandat §12)

Toute mutation résout `buyer_id` depuis le TÉLÉPHONE authentifié (`self.get_buyer_profile`), jamais
depuis un identifiant fourni par l'appelant. `update_recurring_need`/`list_my_recurring_needs`
filtrent en plus explicitement `WHERE buyer_id = :buyer_id` — un acheteur ne peut ni lire ni modifier
le besoin d'un autre (`RECURRING_NEED_NOT_FOUND`, jamais une fuite d'existence).

## Transaction (mandat §14)

`create_recurring_need` insère `recurring_needs` PUIS matérialise la fenêtre J→J+7 d'occurrences dans
LA MÊME transaction (un seul `flush`, un seul commit porté par `@transactional(write=True)` — voir
`d.py`) : jamais de besoin créé sans ses occurrences.

## Idempotence (mandat §13)

Ce module ne réimplémente PAS de 4ᵉ mécanisme : la déduplication d'un rejeu de webhook/tâche pour
`create_recurring_need` est assurée EN AMONT par `mcp_idempotency_store` (dédup serveur générique de
`AgriDBMCPServer.call_tool`, voir `infrastructure/mcp/runtime.py`) — ce mixin n'a besoin de garantir
que sa propre idempotence STRUCTURELLE : générer deux fois la fenêtre d'occurrences d'un MÊME besoin
ne doit jamais produire de doublon (`UNIQUE(recurring_need_id, occurrence_date)`, `ON CONFLICT DO
NOTHING`) — c'est ce qui protège aussi la réconciliation Celery Beat (Phase 3) qui étendra la fenêtre
chaque jour.

## Sémantique des modifications (mandat §9/§10, révisée B25 — contrat normatif :
docs/RECURRING_MUTATION_CONSISTENCY_CONTRACT.md)
`update_recurring_need` verrouille le besoin (`FOR UPDATE`), contrôle `expected_version` (jeton = `updated_at` en µs, voir
`recurring_need_version_of`), puis applique UNE action ; le résultat porte `outcome` (`APPLIED`/`ALREADY_APPLIED`/
`VERSION_CONFLICT`). Une occurrence « non engagée » (`OPEN`, `MATCHED`) peut être réécrite par une mutation ; une occurrence
engagée (`ACCEPTED`, `PARTIALLY_ACCEPTED`) ou terminale ne l'est jamais. Toute réécriture passe par UNE primitive
(`_reset_uncommitted_occurrences`) qui expire les propositions `PROPOSED` et remet `quantity_matched` à 0.
- `PERMANENT_QUANTITY` : les occurrences non engagées qui suivaient l'ancien contrat le suivent ; une exception explicite
  n'est jamais écrasée.
- `PERMANENT_FREQUENCY` : les occurrences non engagées, non exceptionnelles, qui ne sont plus dues passent `CANCELLED`
  (ré-ouvertes par la matérialisation si le planning revient) ; la nouvelle fenêtre est matérialisée dans la même transaction.
- `PAUSE` : `PAUSED` ; `paused_until` = premier jour de reprise (exclu) ; les occurrences non engagées avant ce jour passent
  `SKIPPED` ; les `ACCEPTED` survivent. Reprise automatique par `replenish_occurrence_windows`.
- `RESUME` : `ACTIVE`, planning régénéré à partir d'aujourd'hui, sans rattrapage ; les dates sautées restent sautées.
- `CANCEL` : fin durable ; occurrences non engagées → `CANCELLED` ; engagements `ACCEPTED` conservés ; un besoin annulé
  n'accepte plus aucune mutation (`need_not_active`).
- `OCCURRENCE_OVERRIDE` / `OCCURRENCE_SKIP` : UNE occurrence, jamais le besoin ni son planning.
La matérialisation (cron ET self-service) reverrouille le besoin et ne crée rien s'il n'est plus `ACTIVE`.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text, update

from ladini.domain.analytics.business_events import BusinessEventName
from ladini.domain.analytics.emitter import BusinessEventEmitter
from ladini.domain.analytics.metric_dictionary import Journey
from ladini.domain.analytics.units import resolve_subcategory_canonical_unit
from ladini.domain.models import (
    BuyerProfile,
    NeedAllocation,
    NotificationOutbox,
    Order,
    OrderItem,
    OrderStatusHistory,
    Producer,
    Product,
    RecurringNeed,
    RecurringNeedOccurrence,
    SubCategory,
    User,
)
from ladini.domain.package_inventory import restore_stock_for_item, sells_by_package
from ladini.domain.recurring_supply.recurrence import (
    OCCURRENCE_WINDOW_DAYS,
    InvalidRecurrenceRule,
    RecurrenceRule,
    generate_occurrence_dates,
    next_due_date,
)

from .base import BaseMixin
from .common import normalize_phone
from .errors import BusinessRuleException
from .pricing_persistence import order_item_snapshot_columns

logger = logging.getLogger("ladini.services.database.recurring_supply")

# ── Registre durable des confirmations (`marketplace.recurring_need_drafts`) ──────────
# Une confirmation conversationnelle = `(draft_id, execution_version)`. La ligne du draft
# est verrouillée (`FOR UPDATE`) puis passée à EXECUTED DANS LA MÊME TRANSACTION que
# l'insertion des besoins : soit les deux sont commités, soit aucun. Conséquences :
#   - un retry de la même confirmation (HTTP, Celery, timeout MCP, « oui » répété,
#     réconciliation) trouve EXECUTED et REJOUE le résultat stocké — jamais un 2e jeu ;
#   - deux exécutions concurrentes sont sérialisées par le verrou de ligne ;
#   - un timeout client après COMMIT n'est plus ambigu : la ligne dit ce qui s'est passé.
# SQL brut sur le payload JSON : ce service ne dépend pas du module de draft conversationnel
# (`graphs/.../recurring_need_draft.py`, voir test_service_does_not_depend_on_graph_domain).
_CONFIRMATION_LOCK_SQL = text(
    "SELECT status, version, payload FROM marketplace.recurring_need_drafts "
    "WHERE draft_id = :draft_id FOR UPDATE"
)
_CONFIRMATION_DONE_SQL = text(
    "UPDATE marketplace.recurring_need_drafts "
    "SET status = 'EXECUTED', version = :version, payload = :payload, updated_at = now() "
    "WHERE draft_id = :draft_id"
)
_EXECUTABLE_DRAFT_STATUSES = ("EXECUTING", "EXECUTION_UNKNOWN")

# Une occurrence encore à cet état n'a reçu aucune réponse fournisseur — seule une mise à jour
# permanente ou une exception ponctuelle peut encore la faire changer sans piétiner du travail déjà
# engagé (matching, acceptation, livraison — tous hors scope Phase 2, mais le schéma les anticipe).
_MUTABLE_OCCURRENCE_STATUSES = ("OPEN",)
# B25 — occurrences NON ENGAGÉES : aucune réponse acheteur définitive (OPEN = à chercher, MATCHED = proposition pendante,
# réservation logique sans stock débité). Seules elles peuvent être réécrites par une mutation ; ACCEPTED, PARTIALLY_ACCEPTED
# et tous les statuts terminaux sont de l'historique métier, jamais réécrit (contrat B25).
_UNCOMMITTED_OCCURRENCE_STATUSES = ("OPEN", "MATCHED")

_VERSION_EPOCH = datetime(1970, 1, 1)


def recurring_need_version_of(updated_at: datetime) -> int:
    """Jeton de version d'un besoin : `updated_at` en microsecondes (entier exact, jamais de flottant). Aucune colonne
    `version` n'existe sur `recurring_needs` (schéma Drizzle) ; `updated_at` est réécrit à chaque mutation effective par
    `_bump_need_version` (horloge du serveur de base, monotone sous le verrou de ligne)."""
    if updated_at.tzinfo is not None:
        updated_at = updated_at.astimezone(timezone.utc).replace(tzinfo=None)
    return (updated_at - _VERSION_EPOCH) // timedelta(microseconds=1)


def recurring_need_version(need: Any) -> int:
    return recurring_need_version_of(need.updated_at)


#: Mutations d'UNE livraison : la version attendue est celle de l'OCCURRENCE affichée (`expected_occurrence_version`) ;
#: toutes les autres actions portent sur le besoin (`expected_version`).
RECURRING_OCCURRENCE_ACTIONS = ("OCCURRENCE_SKIP", "OCCURRENCE_OVERRIDE")

RECURRING_NEED_ACTIONS = (
    "PERMANENT_QUANTITY",
    "PERMANENT_FREQUENCY",
    "PAUSE",
    "RESUME",
    "CANCEL",
    "OCCURRENCE_OVERRIDE",
    "OCCURRENCE_SKIP",
)

# `accept_match_proposal` — distinct de `RECURRING_NEED_ACTIONS` (mandat VS4 pilote) : celles-ci
# modifient la RÈGLE (`recurring_needs`) ou une exception d'occurrence AVANT tout matching ; ACCEPT/
# REJECT répondent à une PROPOSITION déjà calculée (allocations `PROPOSED`, Phase 3) — deux moments
# métier différents, jamais mélangés dans le même enum d'action.
MATCH_RESPONSE_ACTIONS = ("ACCEPT", "REJECT")
#: Statuts d'une occurrence « en cours » pour le Buyer (B21) : proposable ou déjà acceptée (commandes en cours).
#: Contrat « prochaine livraison » (B22) — parmi les 12 statuts du CHECK `recurring_need_occurrences_status_chk` :
#:   - COURANT/PROCHAIN (retenus, dans l'ordre des dates, à partir d'AUJOURD'HUI inclus) : OPEN, MATCHED (proposables),
#:     ACCEPTED, PARTIALLY_ACCEPTED (acceptées, commandes en cours) ;
#:   - JOUÉS/SAUTÉS (jamais « la prochaine » : la date suivante prend le relais) : REJECTED, EXPIRED, SKIPPED, CANCELLED,
#:     FULFILLED, PARTIALLY_FULFILLED, UNFULFILLED ;
#:   - PROPOSED : statut hérité du schéma, jamais produit par le moteur de matching actuel (OPEN/MATCHED) — non retenu.
#: Une occurrence du jour reste « aujourd'hui » tant qu'elle est dans un statut retenu ; dès qu'elle est jouée, la
#: prochaine échéance de la règle est affichée.
ACTIONABLE_OCCURRENCE_STATUSES = ("OPEN", "MATCHED", "ACCEPTED", "PARTIALLY_ACCEPTED")

# VS5 pilote — livraison/réception. `Order.delivery_status`/`Order.status` réutilisés tels quels
# (texte libre, aucune contrainte CHECK) : seules de NOUVELLES VALEURS s'y ajoutent, aucune
# nouvelle colonne/table pour ces deux mécanismes.
_DELIVERY_TRANSITIONS = {
    "MARK_IN_TRANSIT": ("PENDING", "IN_TRANSIT"),
    "MARK_DELIVERED": ("IN_TRANSIT", "DELIVERED"),
}
_RECEPTION_OUTCOMES = ("RECEIVED", "RECEIVED_WITH_ISSUE")


def _today() -> date:
    return datetime.now().date()


def _decode_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (str, bytes)):
        try:
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


class RecurringSupplyMixin(BaseMixin):

    async def _open_confirmation(
        self, draft_id: Optional[str], draft_version: Optional[int]
    ) -> tuple:
        """Verrouille la confirmation `(draft_id, draft_version)`.

        Retourne `(replay, ledger)` : `replay` = résultat déjà enregistré (à renvoyer tel
        quel, rien à insérer) ; `ledger` = contexte à passer à `_close_confirmation` après
        l'insertion. `(None, None)` quand aucun draft n'est fourni (appel hors conversation)."""
        if not draft_id:
            return None, None
        if draft_version is None:
            raise BusinessRuleException("Version de confirmation manquante.")
        session = self.session
        if session is None:
            raise BusinessRuleException("Session indisponible.")
        row = (await session.execute(_CONFIRMATION_LOCK_SQL, {"draft_id": str(draft_id)})).mappings().first()
        if row is None:
            raise BusinessRuleException("Demande introuvable : confirmation impossible.")
        payload = _decode_payload(row["payload"])
        if payload.get("execution_version") != int(draft_version):
            raise BusinessRuleException("Cette confirmation n'est plus à jour.")
        if row["status"] == "EXECUTED":
            stored = payload.get("execution_result")
            if isinstance(stored, dict):
                logger.info("RECURRING_NEED_CREATE_REPLAYED | draft_id=%s | version=%s", draft_id, draft_version)
                return {**stored, "replayed": True}, None
            raise BusinessRuleException("Demande déjà traitée.")
        if row["status"] not in _EXECUTABLE_DRAFT_STATUSES:
            raise BusinessRuleException(f"Demande non confirmable (statut {row['status']}).")
        return None, {"draft_id": str(draft_id), "version": int(row["version"]), "payload": payload}

    async def _close_confirmation(self, ledger: Optional[Dict[str, Any]], result: Dict[str, Any]) -> None:
        """Passe la confirmation à EXECUTED dans la transaction courante (celle des besoins)."""
        if ledger is None:
            return
        new_version = ledger["version"] + 1
        payload = {**ledger["payload"], "status": "EXECUTED", "version": new_version, "execution_result": result}
        session = self.session
        if session is None:
            raise BusinessRuleException("Session indisponible.")
        await session.execute(
            _CONFIRMATION_DONE_SQL,
            {"draft_id": ledger["draft_id"], "version": new_version, "payload": json.dumps(payload, default=str)},
        )
    # ─── CREATE ───────────────────────────────────────────────────────

    async def create_recurring_need(
        self,
        phone: str,
        product_query: str,
        quantity: float,
        unit: str,
        recurrence_type: str,
        weekly_days: Optional[List[int]] = None,
        excluded_weekdays: Optional[List[int]] = None,
        starts_at: Optional[Any] = None,
        ends_at: Optional[Any] = None,
        max_price_per_unit: Optional[float] = None,
        draft_id: Optional[str] = None,
        draft_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Crée un `RecurringNeed` PUIS matérialise la fenêtre J→J+7 d'occurrences, dans une seule
        transaction (mandat §14). `starts_at` non fourni = demain (mandat §4 : "À partir de demain.").
        UN seul produit — voir `create_recurring_needs` (pluriel) pour PLUSIEURS produits partageant la
        même récurrence, créés atomiquement (chantier multi-produits, 2026-09-23)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        replay, ledger = await self._open_confirmation(draft_id, draft_version)
        if replay is not None:
            return {"status": "success", **replay}

        user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))

        start_date = _parse_date(starts_at) or (_today() + timedelta(days=1))
        end_date = _parse_date(ends_at)

        rule = RecurrenceRule(
            recurrence_type=recurrence_type,
            weekly_days=weekly_days or (),
            excluded_weekdays=excluded_weekdays or (),
            starts_at=start_date,
            ends_at=end_date,
        )

        result = await self._insert_one_recurring_need(
            buyer_id=buyer_profile.id,
            product_query=product_query,
            quantity=quantity,
            unit=unit,
            recurrence_type=recurrence_type,
            weekly_days=weekly_days,
            excluded_weekdays=excluded_weekdays,
            start_date=start_date,
            end_date=end_date,
            max_price_per_unit=max_price_per_unit,
            rule=rule,
            actor_id=user_obj.id,
            zone_id=getattr(user_obj, "zone_id", None),
        )

        await self._close_confirmation(ledger, result)
        logger.info(
            "RECURRING_NEED_CREATED | recurring_need_id=%s | buyer_id=%s | occurrences=%s",
            result["recurring_need_id"], buyer_profile.id, result["occurrences_created"],
        )
        return {"status": "success", **result}

    async def create_recurring_needs(
        self,
        phone: str,
        items: List[Dict[str, Any]],
        recurrence_type: str,
        weekly_days: Optional[List[int]] = None,
        excluded_weekdays: Optional[List[int]] = None,
        starts_at: Optional[Any] = None,
        ends_at: Optional[Any] = None,
        max_price_per_unit: Optional[float] = None,
        draft_id: Optional[str] = None,
        draft_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Variante plurielle de `create_recurring_need` (chantier multi-produits, 2026-09-23) : crée
        PLUSIEURS `RecurringNeed` (un par item de `items`, chacun `{"product_query", "quantity",
        "unit"}`) PARTAGEANT la même récurrence/dates/prix max, chacun avec ses propres occurrences,
        dans UNE SEULE transaction — même discipline que `create_recurring_need` (mandat §14), étendue
        à N produits : un item dont le produit ne résout à aucune sous-catégorie (`BusinessRuleException`
        propagée, jamais interceptée ici) fait échouer TOUS les items déjà insérés dans CET appel
        (rollback complet porté par `@transactional(write=True)`, appliqué UNE fois pour tout l'appel —
        jamais de création partielle silencieuse)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        if not items:
            raise BusinessRuleException("Aucun produit fourni.")

        replay, ledger = await self._open_confirmation(draft_id, draft_version)
        if replay is not None:
            return {"status": "success", **replay}

        user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))

        start_date = _parse_date(starts_at) or (_today() + timedelta(days=1))
        end_date = _parse_date(ends_at)

        rule = RecurrenceRule(
            recurrence_type=recurrence_type,
            weekly_days=weekly_days or (),
            excluded_weekdays=excluded_weekdays or (),
            starts_at=start_date,
            ends_at=end_date,
        )

        created: List[Dict[str, Any]] = []
        for item in items:
            result = await self._insert_one_recurring_need(
                buyer_id=buyer_profile.id,
                product_query=item["product_query"],
                quantity=item["quantity"],
                unit=item["unit"],
                recurrence_type=recurrence_type,
                weekly_days=weekly_days,
                excluded_weekdays=excluded_weekdays,
                start_date=start_date,
                end_date=end_date,
                max_price_per_unit=max_price_per_unit,
                rule=rule,
                actor_id=user_obj.id,
                zone_id=getattr(user_obj, "zone_id", None),
            )
            created.append(result)

        await self._close_confirmation(ledger, {"items": created})
        logger.info(
            "RECURRING_NEED_CREATED | batch=true | buyer_id=%s | count=%s | recurring_need_ids=%s",
            buyer_profile.id, len(created), [c["recurring_need_id"] for c in created],
        )
        return {"status": "success", "items": created}

    async def _insert_one_recurring_need(
        self,
        *,
        buyer_id: Any,
        product_query: str,
        quantity: float,
        unit: str,
        recurrence_type: str,
        weekly_days: Optional[List[int]],
        excluded_weekdays: Optional[List[int]],
        start_date: date,
        end_date: Optional[date],
        max_price_per_unit: Optional[float],
        rule: RecurrenceRule,
        actor_id: Any = None,
        zone_id: Any = None,
    ) -> Dict[str, Any]:
        """UN `RecurringNeed` + ses occurrences — partagée par `create_recurring_need` (1 produit) et
        `create_recurring_needs` (N produits, même récurrence/dates). Aucun flush/commit propre à cette
        insertion : le flush (`current_session.flush()`) rend juste l'id disponible pour matérialiser
        les occurrences tout de suite ; le commit reste entièrement porté par l'appelant racine
        (`@transactional(write=True)`), donc partagé par TOUS les items d'un même appel `create_recurring_needs`."""
        current_session = self.session
        sub_cat = await self._resolve_sub_category(product_query)

        need = RecurringNeed(
            id=uuid.uuid4(),
            buyer_id=buyer_id,
            sub_category_id=sub_cat.id,
            quantity=float(quantity),
            unit=unit.upper().strip(),
            recurrence_type=recurrence_type,
            weekly_days=list(weekly_days) if weekly_days else None,
            excluded_weekdays=list(excluded_weekdays) if excluded_weekdays else None,
            starts_at=datetime.combine(start_date, datetime.min.time()),
            ends_at=datetime.combine(end_date, datetime.min.time()) if end_date else None,
            status="ACTIVE",
            max_price_per_unit=float(max_price_per_unit) if max_price_per_unit is not None else None,
        )
        current_session.add(need)
        await current_session.flush()

        # Analytics Phase C : le VRAI RecurringNeed vient d'être flushé (jamais un draft), dans la
        # transaction racine — l'intention d'event est commitée/rollbackée avec le besoin.
        canonical_unit = resolve_subcategory_canonical_unit(sub_cat)
        await BusinessEventEmitter(current_session).emit(
            event_name=BusinessEventName.RECURRING_NEED_CREATED,
            journey=Journey.RECURRING,
            actor_type="BUYER",
            actor_id=actor_id,
            buyer_id=buyer_id,
            zone_id=zone_id,
            entity_type="RECURRING_NEED",
            entity_id=need.id,
            idempotency_key=f"RECURRING_NEED_CREATED:{need.id}",
            sub_category_id=sub_cat.id,
            quantity=float(need.quantity),
            unit=need.unit,
            canonical_unit_override=canonical_unit,
            metadata={"recurrence_type": recurrence_type},
        )

        window_end = _today() + timedelta(days=OCCURRENCE_WINDOW_DAYS)
        occurrences_created = await self._materialize_occurrences(
            need,
            rule,
            from_date=_today(),
            to_date=window_end,
            actor_type="BUYER",
            actor_id=actor_id,
            zone_id=zone_id,
            canonical_unit=canonical_unit,
        )

        return {
            "recurring_need_id": str(need.id),
            "sub_category": sub_cat.name,
            "occurrences_created": occurrences_created,
        }

    async def _materialize_occurrences(
        self,
        need: RecurringNeed,
        rule: RecurrenceRule,
        *,
        from_date: date,
        to_date: date,
        actor_type: str = "SYSTEM",
        actor_id: Any = None,
        zone_id: Any = None,
        canonical_unit: Optional[str] = None,
    ) -> int:
        """Idempotent (mandat §8) : `ON CONFLICT (recurring_need_id, occurrence_date) DO NOTHING` —
        un deuxième appel sur la même fenêtre produit 0 doublon. Chaque occurrence SNAPSHOTTE
        `requested_quantity`/`unit` au moment de la génération (mandat §16, propriété testée)."""
        dates = generate_occurrence_dates(rule, from_date=from_date, to_date=to_date)
        if not dates:
            return 0
        current_session = self.session
        rows = [
            {
                "recurring_need_id": need.id,
                "occurrence_date": datetime.combine(d, datetime.min.time()),
                "requested_quantity": need.quantity,
                "unit": need.unit,
            }
            for d in dates
        ]
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(RecurringNeedOccurrence).values(rows)
        # B25 : une date `CANCELLED` sous un besoin ACTIF a été écartée par un changement de fréquence (l'annulation du
        # besoin, seule autre source de `CANCELLED`, n'est jamais matérialisée) : si le planning la redemande, elle est
        # ré-ouverte avec le snapshot COURANT. Toute autre ligne existante est laissée telle quelle (`DO NOTHING` effectif).
        stmt = stmt.on_conflict_do_update(
            index_elements=["recurring_need_id", "occurrence_date"],
            set_={
                "status": "OPEN",
                "requested_quantity": stmt.excluded.requested_quantity,
                "unit": stmt.excluded.unit,
                "quantity_matched": 0,
                "version": RecurringNeedOccurrence.version + 1,
            },
            where=RecurringNeedOccurrence.status == "CANCELLED",
        )
        returning_stmt = stmt.returning(RecurringNeedOccurrence.id, RecurringNeedOccurrence.occurrence_date)
        inserted = (await current_session.execute(returning_stmt)).all()

        # Analytics Phase C : SEUL point de création d'occurrences (création initiale ET cron de
        # réapprovisionnement passent ici). `RETURNING` ne renvoie QUE les lignes réellement
        # insérées : un `ON CONFLICT DO NOTHING` (rejeu/chevauchement de cron) n'émet donc rien.
        emitter = BusinessEventEmitter(current_session)
        for occ_id, occ_date in inserted:
            await emitter.emit(
                event_name=BusinessEventName.RECURRING_OCCURRENCE_CREATED,
                journey=Journey.RECURRING,
                actor_type=actor_type,
                actor_id=actor_id,
                buyer_id=need.buyer_id,
                zone_id=zone_id,
                entity_type="RECURRING_OCCURRENCE",
                entity_id=occ_id,
                idempotency_key=f"RECURRING_OCCURRENCE_CREATED:{occ_id}",
                sub_category_id=need.sub_category_id,
                quantity=float(need.quantity),
                unit=need.unit,
                canonical_unit_override=canonical_unit,
                metadata={"recurring_need_id": str(need.id), "occurrence_date": occ_date.date().isoformat()},
            )
        return len(inserted)

    def _db(self) -> Any:
        session = self.session
        if session is None:
            raise BusinessRuleException("Session indisponible.")
        return session

    # ── Matérialisation d'UN besoin : MÊME primitive pour le cron et pour le self-service Buyer (B21) ──────────
    @staticmethod
    def _recurrence_rule_for(need: RecurringNeed) -> RecurrenceRule:
        return RecurrenceRule(
            recurrence_type=need.recurrence_type,
            weekly_days=need.weekly_days or (),
            excluded_weekdays=need.excluded_weekdays or (),
            starts_at=need.starts_at,
            ends_at=need.ends_at,
        )

    async def _materialization_dims(self, need_id: Any) -> Dict[Any, Any]:
        """`{need_id: (zone_id, priority_unit)}` — pour TOUS les besoins actifs (`need_id=None`, cron) ou pour UN
        besoin (self-service) : une seule requête, jamais un lookup par besoin."""
        stmt = (
            select(
                RecurringNeed.id.label("need_id"),
                User.zone_id.label("zone_id"),
                SubCategory.priority_unit.label("priority_unit"),
            )
            .join(BuyerProfile, BuyerProfile.id == RecurringNeed.buyer_id)
            .join(User, User.id == BuyerProfile.user_id)
            .join(SubCategory, SubCategory.id == RecurringNeed.sub_category_id)
        )
        stmt = stmt.where(RecurringNeed.status == "ACTIVE") if need_id is None else stmt.where(RecurringNeed.id == need_id)
        return {row.need_id: (row.zone_id, row.priority_unit) for row in (await self._db().execute(stmt)).all()}

    async def _ensure_window_for_need(
        self, need: RecurringNeed, *, dims: Optional[Dict[Any, Any]] = None, actor_type: str = "SYSTEM", actor_id: Any = None
    ) -> int:
        """Matérialise la fenêtre `[aujourd'hui, aujourd'hui + OCCURRENCE_WINDOW_DAYS]` d'UN besoin. Idempotent et
        sûr en concurrence : `ON CONFLICT (recurring_need_id, occurrence_date) DO NOTHING` (contrainte d'unicité en
        base) — cron et Buyer simultanés produisent exactement UNE occurrence par date."""
        locked = await self._lock_active_need(need)
        if locked is None:  # annulé/suspendu entre la lecture du besoin et ici : rien n'est créé (contrat B25)
            return 0
        need = locked
        if dims is None:
            dims = await self._materialization_dims(need.id)
        zone_id, priority_unit = dims.get(need.id, (None, None))
        return await self._materialize_occurrences(
            need,
            self._recurrence_rule_for(need),
            from_date=_today(),
            to_date=_today() + timedelta(days=OCCURRENCE_WINDOW_DAYS),
            actor_type=actor_type,
            actor_id=actor_id,
            zone_id=zone_id,
            canonical_unit=resolve_subcategory_canonical_unit(SimpleNamespace(priority_unit=priority_unit)),
        )

    async def _materialize_due_date(self, need: RecurringNeed, due: date, *, actor_id: Any = None) -> int:
        """Matérialise UNE échéance précise (hors fenêtre J..J+7) — mêmes dimensions et même insertion idempotente que
        `_ensure_window_for_need`."""
        locked = await self._lock_active_need(need)
        if locked is None:
            return 0
        need = locked
        dims = await self._materialization_dims(need.id)
        zone_id, priority_unit = dims.get(need.id, (None, None))
        return await self._materialize_occurrences(
            need,
            self._recurrence_rule_for(need),
            from_date=due,
            to_date=due,
            actor_type="BUYER",
            actor_id=actor_id,
            zone_id=zone_id,
            canonical_unit=resolve_subcategory_canonical_unit(SimpleNamespace(priority_unit=priority_unit)),
        )

    async def _next_actionable_occurrence(self, need_ids: List[Any]) -> Dict[Any, RecurringNeedOccurrence]:
        """Prochaine occurrence PERTINENTE de chaque besoin : la plus proche, à partir d'aujourd'hui, parmi OPEN /
        MATCHED / ACCEPTED / PARTIALLY_ACCEPTED. Jamais une occurrence REJECTED / EXPIRED / terminale (déjà jouée) tant
        qu'une occurrence future pertinente existe. Source unique de la liste ET du détail (B21)."""
        if not need_ids:
            return {}
        stmt = (
            select(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id.in_(need_ids),
                RecurringNeedOccurrence.status.in_(ACTIONABLE_OCCURRENCE_STATUSES),
                RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
            )
            .order_by(RecurringNeedOccurrence.recurring_need_id, RecurringNeedOccurrence.occurrence_date.asc())
        )
        found: Dict[Any, RecurringNeedOccurrence] = {}
        for occ in (await self._db().execute(stmt)).scalars().all():
            found.setdefault(occ.recurring_need_id, occ)
        return found

    async def _planned_schedule(self, needs: List[RecurringNeed]) -> Dict[Any, tuple]:
        """`{need_id: (schedule_state, next_due_date)}` pour des besoins SANS occurrence retenue (B22). Calculé par la fonction
        pure `next_due_date`, en sautant les dates déjà jouées/sautées (une occurrence terminale ou SKIPPED occupe sa date).
        `schedule_state` : `OK` (date calculée) / `ENDED` (règle terminée) / `INVALID` (planning incomplet ou incohérent).
        Une seule requête pour toute la liste (pas de N+1) ; lecture seule."""
        if not needs:
            return {}
        occupied: Dict[Any, set] = {}
        rows = (
            await self._db().execute(
                select(RecurringNeedOccurrence.recurring_need_id, RecurringNeedOccurrence.occurrence_date).where(
                    RecurringNeedOccurrence.recurring_need_id.in_([n.id for n in needs]),
                    RecurringNeedOccurrence.status.notin_(ACTIONABLE_OCCURRENCE_STATUSES),
                    RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
                )
            )
        ).all()
        for need_id, occurrence_date in rows:
            occupied.setdefault(need_id, set()).add(occurrence_date.date())
        planned: Dict[Any, tuple] = {}
        for need in needs:
            try:
                rule = self._recurrence_rule_for(need)
            except (InvalidRecurrenceRule, TypeError, ValueError, AttributeError):
                planned[need.id] = ("INVALID", None)
                continue
            due = next_due_date(rule, from_date=_today(), skip=occupied.get(need.id, ()))
            planned[need.id] = ("OK", due) if due is not None else ("ENDED", None)
        return planned

    async def replenish_occurrence_windows(self) -> Dict[str, Any]:
        """Réapprovisionnement générique (Phase 3, mandat MONTHLY §17/§18) : rejoue
        `_materialize_occurrences` sur `[aujourd'hui, aujourd'hui+OCCURRENCE_WINDOW_DAYS]` pour
        CHAQUE `RecurringNeed` `ACTIVE` — le mécanisme que ce module documentait déjà comme prévu
        (voir la docstring de module, §Idempotence : "la réconciliation Celery Beat (Phase 3) qui
        étendra la fenêtre chaque jour") mais qui n'avait jamais été câblé : `create_recurring_need`
        ne matérialisait la fenêtre QU'UNE FOIS, à la création — gap préexistant à TOUS les types de
        récurrence, rendu bloquant par MONTHLY (un besoin mensuel ne recevrait alors jamais plus
        d'UNE occurrence). Générique à tous les types (aucune branche MONTHLY) : `generate_occurrence_
        dates` sait déjà, pour chacun, quels jours de CETTE fenêtre sont dus. Idempotent (`ON CONFLICT
        DO NOTHING`, comme `_insert_one_recurring_need`) : un rejeu sur la même fenêtre ne crée aucun
        doublon, donc un chevauchement de deux passages du cron ne pose aucun problème. `PAUSED`/
        `CANCELLED` sont exclus : une pause ne doit pas se voir régénérer des occurrences pendant
        qu'elle dure (mandat §9, voir docstring de module)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        # B25 : reprise automatique des pauses temporelles échues — dans CE cron, avant la matérialisation.
        needs_resumed = await self._resume_elapsed_pauses()
        needs = (
            await current_session.execute(select(RecurringNeed).where(RecurringNeed.status == "ACTIVE"))
        ).scalars().all()
        dims = await self._materialization_dims(None)

        needs_examined = 0
        occurrences_created = 0
        for need in needs:
            needs_examined += 1
            occurrences_created += await self._ensure_window_for_need(need, dims=dims)

        logger.info(
            "recurring_need.occurrence_window_replenished | needs_examined=%s | occurrences_created=%s | needs_resumed=%s",
            needs_examined, occurrences_created, needs_resumed,
        )
        return {"needs_examined": needs_examined, "occurrences_created": occurrences_created, "needs_resumed": needs_resumed}

    async def _resolve_sub_category(self, product_query: str) -> SubCategory:
        """Même résolution catalogue que `AuctionMixin.create_auction` (fuzzy trigram + auto-
        provisioning) — un besoin récurrent exprime un besoin acheteur, pas un article de catalogue
        déjà stabilisé, même raisonnement que pour un appel d'offres."""
        current_session = self.session
        from .category import _is_confident_category_match
        from .search import fuzzy_match, similarity_rank

        product_query_clean = str(product_query or "").strip()
        if not product_query_clean:
            raise BusinessRuleException("Produit manquant.")

        sub_cat_stmt = (
            select(SubCategory)
            .where(fuzzy_match(SubCategory.name, product_query_clean))
            .order_by(similarity_rank(SubCategory.name, product_query_clean))
            .limit(1)
        )
        sub_cat = await current_session.scalar(sub_cat_stmt)
        if sub_cat and not _is_confident_category_match(product_query_clean, sub_cat.name):
            sub_cat = None
        if not sub_cat:
            candidate = await self._fuzzy_match_sub_category(product_query_clean)
            if candidate and _is_confident_category_match(product_query_clean, candidate.name):
                sub_cat = candidate
        if not sub_cat:
            sub_cat = await self._get_or_create_sub_category_for_rfq(product_query_clean)
        return sub_cat

    # ─── READ ─────────────────────────────────────────────────────────

    async def list_my_recurring_needs(self, phone: str) -> Dict[str, Any]:
        """Les besoins de l'acheteur + leur prochaine occurrence OPEN/MATCHED (disponibilité
        incluse — mandat Phase 4 §7 : `GET_MY_NEEDS` affiche aussi la disponibilité, pas de nouvel
        intent `GET_MATCH_PROPOSALS`) — UNE requête jointe (mandat §19, pas de N+1 : jamais 1
        requête d'occurrence par besoin listé)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        buyer_id = buyer_profile.id
        digest_snapshot = await self._latest_digest_snapshot(getattr(_user_obj, "phone", None) or phone)

        needs_stmt = (
            select(RecurringNeed, SubCategory.name)
            .join(SubCategory, RecurringNeed.sub_category_id == SubCategory.id)
            .where(RecurringNeed.buyer_id == buyer_id, RecurringNeed.status != "CANCELLED")
            # Ordre déterministe (B22) : création, puis id — deux besoins créés dans la même seconde ne s'échangent jamais.
            .order_by(RecurringNeed.created_at.asc(), RecurringNeed.id.asc())
        )
        rows = (await current_session.execute(needs_stmt)).all()
        needs = [n for n, _ in rows]
        need_ids = [n.id for n in needs]

        next_occurrence_by_need = await self._next_actionable_occurrence(need_ids)
        # B22 : un besoin ACTIF sans occurrence retenue garde une prochaine date CALCULÉE (jamais « à planifier » tant que
        # sa règle est valide) ; PAUSED/CANCELLED ne sont jamais traités comme actifs (aucune date, aucune matérialisation).
        planned = await self._planned_schedule(
            [n for n in needs if n.status == "ACTIVE" and n.id not in next_occurrence_by_need]
        )

        items = []
        for need, sub_category_name in rows:
            next_occ = next_occurrence_by_need.get(need.id)
            schedule_state, planned_date = planned.get(need.id, ("OK" if need.status == "ACTIVE" else need.status, None))
            next_date = next_occ.occurrence_date.date() if next_occ else planned_date
            items.append(
                {
                    "recurring_need_id": str(need.id),
                    "product": sub_category_name,
                    "quantity": float(need.quantity),
                    "unit": need.unit,
                    "recurrence_type": need.recurrence_type,
                    "weekly_days": need.weekly_days,
                    "status": need.status,
                    "need_version": recurring_need_version(need),
                    "paused_until": need.paused_until.date().isoformat() if need.paused_until else None,
                    "schedule_state": schedule_state,
                    # Début et création : distinguent deux besoins par ailleurs identiques (engagements distincts).
                    "starts_on": need.starts_at.date().isoformat() if need.starts_at else None,
                    "created_on": need.created_at.date().isoformat() if need.created_at else None,
                    "next_occurrence_materialized": next_occ is not None,
                    "next_occurrence_is_today": next_date == _today() if next_date else False,
                    "next_occurrence_date": next_date.isoformat() if next_date else None,
                    "next_occurrence_id": str(next_occ.id) if next_occ else None,
                    "next_occurrence_status": next_occ.status if next_occ else None,
                    "requested_quantity": float(next_occ.requested_quantity) if next_occ else None,
                    "matched_quantity": float(next_occ.quantity_matched) if next_occ else None,
                    # Identité de la proposition (réponse au digest) : version CAS + « a été notifiée ».
                    "next_occurrence_version": int(next_occ.version) if next_occ else None,
                    "next_occurrence_notified": bool(next_occ and next_occ.notified_at),
                    # Snapshot du DERNIER digest (B12) : la proposition en fait-elle partie, et à quelle
                    # version ? `None` => digest antérieur à B12 ou aucun digest.
                    "in_latest_digest": bool(
                        next_occ and digest_snapshot is not None and str(next_occ.id) in digest_snapshot
                    ),
                    "digest_occurrence_version": (
                        digest_snapshot.get(str(next_occ.id)) if next_occ and digest_snapshot else None
                    ),
                }
            )
        return {"status": "success", "items": items, "digest_snapshot_available": digest_snapshot is not None}

    async def get_recurring_need_detail(self, phone: str, recurring_need_id: str) -> Dict[str, Any]:
        """Détail d'UNE occurrence : besoin, disponibilité trouvée, fournisseurs + prix (mandat §5).
        Lecture seule — ne modifie jamais `need_allocations`/`recurring_need_occurrences` (c'est le
        rôle exclusif du moteur de matching, Phase 3). UNE requête jointe pour les allocations
        (producteur + libellé), jamais une par allocation."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        buyer_id = buyer_profile.id

        need_row = (
            await current_session.execute(
                select(RecurringNeed, SubCategory.name)
                .join(SubCategory, RecurringNeed.sub_category_id == SubCategory.id)
                .where(RecurringNeed.id == recurring_need_id, RecurringNeed.buyer_id == buyer_id)
            )
        ).first()
        if need_row is None:
            raise BusinessRuleException("Besoin introuvable.")
        need, sub_category_name = need_row

        occurrence = (await self._next_actionable_occurrence([need.id])).get(need.id)
        need_info = {
            "recurring_need_id": str(need.id),
            "recurrence_type": need.recurrence_type,
            "weekly_days": need.weekly_days,
            "need_status": need.status,
            "need_version": recurring_need_version(need),
        }
        schedule_state, planned_date = (
            (await self._planned_schedule([need])).get(need.id, ("OK", None)) if need.status == "ACTIVE" and occurrence is None
            else ("OK" if need.status == "ACTIVE" else need.status, None)
        )
        need_info["schedule_state"] = schedule_state
        need_info["starts_on"] = need.starts_at.date().isoformat() if need.starts_at else None
        if occurrence is None:
            last = await current_session.scalar(
                select(RecurringNeedOccurrence)
                .where(RecurringNeedOccurrence.recurring_need_id == need.id)
                .order_by(RecurringNeedOccurrence.occurrence_date.desc())
                .limit(1)
            )
            return {
                "status": "success",
                **need_info,
                "product": sub_category_name,
                "requested_quantity": float(need.quantity),
                "unit": need.unit,
                "occurrence_date": None,
                "planned_next_date": planned_date.isoformat() if planned_date else None,
                "allocations": [],
                "last_occurrence": (
                    {
                        "status": last.status,
                        "date": last.occurrence_date.date().isoformat(),
                        "requested_quantity": float(last.requested_quantity),
                        "quantity_delivered": float(last.quantity_delivered or 0),
                        "unit": last.unit,
                    }
                    if last is not None
                    else None
                ),
            }

        alloc_rows = (
            await current_session.execute(
                text(
                    "SELECT na.quantity, na.unit_price, na.unit, "
                    "COALESCE(pr.business_name, 'Producteur') AS producer_label "
                    "FROM marketplace.need_allocations na "
                    "JOIN marketplace.producers pr ON pr.id = na.producer_id "
                    "WHERE na.occurrence_id = :occurrence_id AND na.status = 'PROPOSED' "
                    "ORDER BY na.unit_price ASC, producer_label ASC"
                ),
                {"occurrence_id": occurrence.id},
            )
        ).mappings().all()

        orders: List[Dict[str, Any]] = []
        if occurrence.status in ("ACCEPTED", "PARTIALLY_ACCEPTED") and occurrence.order_group_id is not None:
            order_rows = (
                await current_session.execute(
                    text(
                        "SELECT o.id AS order_id, o.status AS order_status, COALESCE(pr.business_name, 'Producteur') AS producer_label, "
                        "COALESCE(SUM(oi.quantity), 0) AS quantity "
                        "FROM marketplace.orders o "
                        "JOIN marketplace.order_items oi ON oi.order_id = o.id "
                        "JOIN marketplace.products p ON p.id = oi.product_id "
                        "JOIN marketplace.producers pr ON pr.id = p.producer_id "
                        "WHERE o.checkout_group_id = :group_id AND o.order_type = 'RECURRING_SUPPLY' "
                        "GROUP BY o.id, o.status, pr.business_name ORDER BY pr.business_name, o.id"
                    ),
                    {"group_id": occurrence.order_group_id},
                )
            ).mappings().all()
            orders = [
                {"order_id": str(r["order_id"]), "order_status": r["order_status"], "producer_label": r["producer_label"],
                 "quantity": float(r["quantity"])}
                for r in order_rows
            ]

        return {
            "status": "success",
            **need_info,
            "occurrence_status": occurrence.status,
            "quantity_matched": float(occurrence.quantity_matched or 0),
            "quantity_confirmed": float(occurrence.quantity_confirmed or 0),
            "quantity_delivered": float(occurrence.quantity_delivered or 0),
            "orders": orders,
            # Identité EXACTE de la proposition affichée (B12) : la confirmation depuis cet écran
            # vise `occurrence_id` à `occurrence_version`, jamais « la prochaine ouverte ».
            "occurrence_id": str(occurrence.id),
            "occurrence_version": int(occurrence.version),
            "product": sub_category_name,
            "requested_quantity": float(occurrence.requested_quantity),
            "unit": occurrence.unit,
            "occurrence_date": occurrence.occurrence_date.date().isoformat(),
            "occurrence_is_today": occurrence.occurrence_date.date() == _today(),
            "allocations": [
                {
                    "producer_label": row["producer_label"],
                    "quantity": float(row["quantity"]),
                    "unit_price": float(row["unit_price"]),
                    "unit": row["unit"],
                }
                for row in alloc_rows
            ],
        }

    # ─── SELF-SERVICE BUYER (B21) : matérialiser / rechercher à la demande, sans cron ni digest ───────────

    async def _owned_need(self, phone: str, recurring_need_id: str):
        user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        need = await self._db().scalar(
            select(RecurringNeed).where(RecurringNeed.id == recurring_need_id, RecurringNeed.buyer_id == buyer_profile.id)
        )
        if need is None:
            raise BusinessRuleException("Besoin introuvable.")
        return user_obj, need

    async def ensure_next_recurring_occurrence(self, phone: str, recurring_need_id: str) -> Dict[str, Any]:
        """Garantit qu'une prochaine occurrence existe pour ce besoin ACTIF — MÊME primitive que le cron
        (`_ensure_window_for_need`, `ON CONFLICT DO NOTHING`) : idempotent, sûr en concurrence (cron + Buyer
        simultanés => exactement UNE occurrence par date). Ne dépend ni du digest ni de `notified_at`."""
        user_obj, need = await self._owned_need(phone, recurring_need_id)
        if need.status == "PAUSED" and await self._resume_elapsed_pauses(need_id=need.id):
            need = (await self._owned_need(phone, recurring_need_id))[1]  # état COMMITÉ après la reprise
        created = 0
        existing = await self._next_actionable_occurrence([need.id])
        source = "EXISTING" if need.id in existing else "NONE"
        if need.status == "ACTIVE" and need.id not in existing:
            actor_id = getattr(user_obj, "id", None)
            created = await self._ensure_window_for_need(need, actor_type="BUYER", actor_id=actor_id)
            existing = await self._next_actionable_occurrence([need.id])
            source = "WINDOW" if need.id in existing else "NONE"
            if need.id not in existing:
                # B22 : règle plus lente que la fenêtre J..J+7 (MENSUEL, échéance unique lointaine) — on matérialise
                # LA prochaine échéance de la règle (une seule ligne, même `ON CONFLICT DO NOTHING` que le cron).
                state, due = (await self._planned_schedule([need])).get(need.id, ("INVALID", None))
                if state == "OK" and due is not None:
                    created += await self._materialize_due_date(need, due, actor_id=actor_id)
                    existing = await self._next_actionable_occurrence([need.id])
                    source = "DUE_DATE" if need.id in existing else "NONE"
        occurrence = existing.get(need.id)
        logger.info(
            "RECURRING_OCCURRENCE_ENSURED | recurring_need_id=%s | created=%s | occurrence_status=%s",
            need.id, created, occurrence.status if occurrence else None,
        )
        logger.info("RECURRING_NEXT_OCCURRENCE_RESOLVED | recurring_need_id=%s | source=%s", need.id, source)
        return {
            "status": "success",
            "recurring_need_id": str(need.id),
            "occurrence_id": str(occurrence.id) if occurrence else None,
            "occurrence_status": occurrence.status if occurrence else None,
            "created": created,
        }

    async def refresh_recurring_need_matching(
        self,
        phone: str,
        recurring_need_id: str,
        occurrence_id: Optional[str] = None,
        expected_occurrence_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """« Rechercher maintenant » : lance le VRAI moteur de matching (`NeedMatchingService.rematch_occurrence`, celui
        du cron) sur la prochaine occurrence du besoin — mêmes filtres, classement, prix, garde conditionnement,
        allocations et versionnement (`version` n'est bumpée que si les allocations changent). N'envoie AUCUN digest.

        B28 : avec `occurrence_id` (+ `expected_occurrence_version`, obligatoire), la relance vise EXACTEMENT cette occurrence
        — jamais « la prochaine » — et passe par `recover_occurrence_sourcing` (décision de récupération, réouverture, rematch)."""
        from ladini.workers.automation.need_matching_service import NeedMatchingService

        if occurrence_id is not None:
            return await self.recover_occurrence_sourcing(
                phone, recurring_need_id, occurrence_id, expected_occurrence_version, source="USER"
            )

        ensured = await self.ensure_next_recurring_occurrence(phone, recurring_need_id)
        if ensured["occurrence_id"] is None:
            return {"status": "success", "outcome": "NO_OCCURRENCE", "recurring_need_id": ensured["recurring_need_id"]}
        if ensured["occurrence_status"] not in ("OPEN", "MATCHED"):
            return {
                "status": "success", "outcome": "NOT_MATCHABLE", "recurring_need_id": ensured["recurring_need_id"],
                "occurrence_id": ensured["occurrence_id"], "occurrence_status": ensured["occurrence_status"],
            }
        logger.info("RECURRING_MANUAL_MATCH_TRIGGERED | occurrence_id=%s", ensured["occurrence_id"])
        report = await NeedMatchingService(self._db()).rematch_occurrence(
            uuid.UUID(ensured["occurrence_id"]), trigger="self_service"
        )
        if report.error:
            return {"status": "success", "outcome": "MATCH_ERROR", "occurrence_id": ensured["occurrence_id"]}
        occurrence = await self._db().scalar(
            select(RecurringNeedOccurrence)
            .where(RecurringNeedOccurrence.id == uuid.UUID(ensured["occurrence_id"]))
            .execution_options(populate_existing=True)
        )
        return {
            "status": "success",
            "outcome": "MATCHED" if float(getattr(occurrence, "quantity_matched", 0) or 0) > 0 else "NO_AVAILABILITY",
            "changed": bool(report.changed),
            "recurring_need_id": ensured["recurring_need_id"],
            "occurrence_id": ensured["occurrence_id"],
            "occurrence_version": int(occurrence.version) if occurrence is not None else None,
            "quantity_matched": float(occurrence.quantity_matched or 0) if occurrence is not None else 0.0,
        }

    async def recover_occurrence_sourcing(
        self,
        phone: str,
        recurring_need_id: str,
        occurrence_id: str,
        expected_occurrence_version: Optional[int],
        *,
        source: str = "USER",
    ) -> Dict[str, Any]:
        """PRIMITIVE UNIQUE de récupération d'UNE occurrence (B28 ; contrat : docs/RECURRING_RECOVERY_CONTRACT.md) : décide
        (`can_recover_occurrence`), invalide la tentative échouée (elle reste historique), rouvre l'occurrence et re-matche
        EXACTEMENT cette occurrence avec le moteur existant. Le flux conversationnel ne décide rien : il résout la cible, appelle
        ceci, rend le résultat. Même primitive pour le self-service (`source="USER"`) et tout appel automatique.

        `expected_occurrence_version` est OBLIGATOIRE (B26) : jamais relue silencieusement avant la mutation. Verrous :
        besoin -> occurrence (même ordre que B25) ; deux récupérations simultanées se sérialisent, la seconde voit l'état
        déjà récupéré (`ALREADY_RECOVERED`) — jamais deux allocations, deux occurrences ni deux commandes.

        Issues (`outcome`) : RECOVERY_APPLIED (réouverte si besoin + re-matchée) / ALREADY_RECOVERED / VERSION_CONFLICT /
        RECOVERY_WINDOW_CLOSED / NOT_RECOVERABLE / NEED_NOT_ACTIVE / OCCURRENCE_COMMITTED / TARGET_NOT_FOUND / MATCH_ERROR.
        Le rematch ne crée jamais de commande et n'accepte jamais un producteur : l'acceptation reste celle de B26."""
        from ladini.workers.automation.need_matching_service import NeedMatchingService

        if expected_occurrence_version is None:
            logger.warning("RECURRING_VERSION_CHECK | action=RECOVER | scope=OCCURRENCE | expected_version_present=False | outcome=VERSION_REQUIRED")
            raise BusinessRuleException(
                "Version de l'état affiché manquante : la relance est refusée.", reason="version_required"
            )
        current_session = self._db()
        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        need = await current_session.scalar(
            select(RecurringNeed)
            .where(RecurringNeed.id == recurring_need_id, RecurringNeed.buyer_id == buyer_profile.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        try:
            occ_uuid = uuid.UUID(str(occurrence_id))
        except (TypeError, ValueError):
            occ_uuid = None
        occurrence = (
            await current_session.scalar(
                select(RecurringNeedOccurrence)
                .where(RecurringNeedOccurrence.id == occ_uuid, RecurringNeedOccurrence.recurring_need_id == need.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if need is not None and occ_uuid is not None else None
        )
        if need is None or occurrence is None:
            # Jamais de fuite d'existence : propriété inconnue et identifiant inconnu donnent la même issue.
            logger.info("RECURRING_RECOVERY_SKIPPED | occurrence_id=%s | source=%s | outcome=TARGET_NOT_FOUND", occurrence_id, source)
            return {"status": "success", "outcome": "TARGET_NOT_FOUND", "occurrence_id": str(occurrence_id)}

        need_row, occ_row = need, occurrence  # liés après la garde : le typage ne se propage pas dans la fermeture

        def _state(outcome: str, **extra: Any) -> Dict[str, Any]:
            return {
                "status": "success", "outcome": outcome, "recurring_need_id": str(need_row.id),
                "occurrence_id": str(occ_row.id), "occurrence_status": occ_row.status,
                "occurrence_version": int(occ_row.version), "occurrence_date": occ_row.occurrence_date.date().isoformat(),
                "requested_quantity": float(occ_row.requested_quantity),
                "quantity_matched": float(occ_row.quantity_matched or 0), **extra,
            }

        if int(occurrence.version) != int(expected_occurrence_version):
            self._log_version_check("RECOVER", "OCCURRENCE", match=False)
            if occurrence.status in ("OPEN", "MATCHED"):
                # L'occurrence a déjà été rouverte/re-matchée depuis l'écran affiché (autre message, cron) : un seul effet.
                _log_recovery("SKIPPED", occurrence, source=source, recoverable=True, outcome="ALREADY_RECOVERED")
                return _state("ALREADY_RECOVERED")
            _log_recovery("SKIPPED", occurrence, source=source, recoverable=False, outcome="VERSION_CONFLICT")
            return _state("VERSION_CONFLICT", expected_version=int(expected_occurrence_version))
        self._log_version_check("RECOVER", "OCCURRENCE", match=True)

        attempts = await self._attempt_summary(occurrence)
        decision = can_recover_occurrence(
            occurrence, need_status=str(need.status), today=_today(), live_orders=attempts["live"],
            sourcing_failed=attempts["failed"], user_initiated=source == "USER",
        )
        _log_recovery("REQUESTED", occurrence, source=source, recoverable=decision.recoverable, outcome=decision.outcome)
        if not decision.recoverable:
            if decision.outcome == "RECOVERY_WINDOW_CLOSED" and occurrence.status in ("OPEN", "MATCHED", "ACCEPTED", "PARTIALLY_ACCEPTED", "REJECTED"):
                # Date dépassée mais occurrence jamais clôturée (le cron d'expiration n'est pas encore passé) : clôture honnête.
                await self._close_unrecoverable(occurrence, attempts)
            _log_recovery("SKIPPED", occurrence, source=source, recoverable=False, outcome=decision.outcome)
            return _state(decision.outcome)

        if not decision.in_place:
            reason = "PROPOSAL_REJECTED" if occurrence.status == "REJECTED" else attempts["reason"]
            occurrence.status = "OPEN"
            occurrence.quantity_matched = 0
            occurrence.quantity_confirmed = 0
            occurrence.version = int(occurrence.version) + 1
            await current_session.flush()
            await current_session.execute(
                update(NeedAllocation)
                .where(NeedAllocation.occurrence_id == occurrence.id, NeedAllocation.status == "PROPOSED")
                .values(status="EXPIRED")
            )
            _log_recovery("APPLIED", occurrence, source=source, reason=reason, recoverable=True, outcome="REOPENED")

        report = await NeedMatchingService(current_session).rematch_occurrence(occurrence.id, trigger=f"recovery_{source.lower()}")
        if report.error:
            _log_recovery("FAILED", occurrence, source=source, outcome="MATCH_ERROR")
            return {"status": "success", "outcome": "MATCH_ERROR", "occurrence_id": str(occurrence.id)}
        refreshed = await current_session.scalar(
            select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.id == occurrence.id).execution_options(populate_existing=True)
        )
        occurrence = refreshed if refreshed is not None else occurrence
        _log_recovery("REMATCHED", occurrence, source=source, changed=bool(report.changed),
                      matched=float(occurrence.quantity_matched or 0), candidates=report.candidate_count)
        return _state("RECOVERY_APPLIED", changed=bool(report.changed),
                      proposal_available=float(occurrence.quantity_matched or 0) > 0)

    async def _attempt_summary(self, occurrence: Any) -> Dict[str, Any]:
        """Faits sur les tentatives d'approvisionnement d'une occurrence (commandes issues de ses allocations `CONVERTED`) :
        `live` = commandes en cours ; `failed` = la tentative COURANTE (`order_group_id`) est entièrement tombée côté
        producteur/système et rien n'a été livré ; `reason` = raison structurée du dernier échec. Lecture seule."""
        rows = (
            await self._db().execute(
                select(Order.id, Order.status, Order.delivery_status, Order.cancellation_role, Order.checkout_group_id)
                .select_from(NeedAllocation)
                .join(OrderItem, OrderItem.id == NeedAllocation.order_item_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(NeedAllocation.occurrence_id == occurrence.id, NeedAllocation.status == "CONVERTED")
            )
        ).all()
        states = {r[0]: _order_fulfillment_state(r[1], r[2]) for r in rows}
        current = [r for r in rows if occurrence.order_group_id is None or r[4] == occurrence.order_group_id]
        failed = bool(current) and all(
            states[r[0]] == "CANCELLED" and str(r[3] or "").upper() in _ATTEMPT_FAILED_BY for r in current
        ) and not any(st in ("DELIVERED", "ISSUE") for st in states.values())
        return {
            "live": sum(1 for st in states.values() if st == "ACTIVE"),
            "failed": failed,
            "reason": await self._failure_reason([r[0] for r in current]) if current else "PRODUCER_CANCELLED",
        }

    async def _close_unrecoverable(self, occurrence: Any, attempts: Dict[str, Any]) -> None:
        """Fenêtre fermée sur une occurrence encore ouverte/acceptée-sans-commande : clôture honnête (`EXPIRED` si jamais
        engagée, `UNFULFILLED` si une tentative a échoué) pour qu'elle ne masque plus la prochaine échéance."""
        if occurrence.status in ("OPEN", "MATCHED"):
            occurrence.status = "EXPIRED"
        elif occurrence.status in ("ACCEPTED", "PARTIALLY_ACCEPTED") and attempts["live"] == 0:
            occurrence.status = "UNFULFILLED"
        else:
            return
        occurrence.version = int(occurrence.version) + 1
        await self._db().execute(
            update(NeedAllocation)
            .where(NeedAllocation.occurrence_id == occurrence.id, NeedAllocation.status == "PROPOSED")
            .values(status="EXPIRED")
        )
        await self._db().flush()

    # ─── CONFIRMATION (VS4 pilote) ────────────────────────────────────

    async def accept_match_proposal(
        self,
        phone: str,
        recurring_need_id: str,
        action: str,
        occurrence_id: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Confirme ou refuse la proposition d'approvisionnement de la PROCHAINE occurrence
        matchée d'un besoin (UX pilote : "CONFIRMER TOUT" / "PAS DEMAIN").

        `ACCEPT` convertit CHAQUE allocation `PROPOSED` de cette occurrence en commande — UNE
        commande PAR PRODUCTEUR, corrélées par `order_group_id` (même convention que
        `Order.checkout_group_id`, voir `buyer.py::create_preorder`) — réutilise le moteur de
        commande existant tel quel : aucune nouvelle table, aucun nouveau statut de commande,
        aucun paiement. Statut initial `PENDING_PRODUCER_CONFIRMATION` : le producteur doit
        encore accuser réception, via les MÊMES intents `PRODUCER_CONFIRM_ORDER`/
        `PRODUCER_CANCEL_ORDER` que le reste du catalogue — aucun nouveau mécanisme producteur.

        Débite `Product.quantity_for_sale` ICI, jamais avant : Phase 3 (`domain/recurring_supply/
        matching.py`) laisse délibérément ce champ intact — une allocation `PROPOSED` n'est qu'une
        réservation logique jusqu'à cette confirmation réelle de l'acheteur.

        `REJECT` ne crée aucune commande, ne débite aucun stock — l'occurrence passe `REJECTED`.

        Verrouille l'occurrence ET ses allocations (`FOR UPDATE`) pour empêcher un double accept
        concurrent (deux tours WhatsApp presque simultanés) de convertir deux fois les mêmes
        allocations ou de créer deux jeux de commandes.

        MODE EXACT (`occurrence_id` fourni — chemin de la réponse au DIGEST, B11-recurring) : la
        réponse vise UNE occurrence précise (celle que le digest a notifiée), jamais « la plus
        proche encore ouverte ». Aucune mutation, retour structuré `status="success"` +
        `outcome` ∈ {ALREADY_PROCESSED, EXPIRED, NEED_INACTIVE, PROPOSAL_CHANGED, NO_PROPOSAL,
        STOCK_CHANGED, NOT_FOUND} quand la proposition n'est plus valide ; `expected_version` (CAS
        sur `occurrence.version`) détecte une proposition modifiée depuis l'envoi. Sans
        `occurrence_id` (écran détail d'UN besoin) : comportement historique, resserré
        uniquement sur le besoin ACTIF (la date d'expiration n'est vérifiée qu'en mode exact)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        action = str(action or "").upper().strip()
        if action not in MATCH_RESPONSE_ACTIONS:
            raise BusinessRuleException(f"Action inconnue : {action}")

        user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        buyer_id = buyer_profile.id

        need = await current_session.scalar(
            select(RecurringNeed).where(
                RecurringNeed.id == recurring_need_id, RecurringNeed.buyer_id == buyer_id
            )
        )
        if need is None:
            if occurrence_id is not None:
                return self._proposal_refused("NOT_FOUND", recurring_need_id, occurrence_id, action)
            raise BusinessRuleException("Besoin introuvable.")

        if need.status != "ACTIVE":
            # Besoin désactivé (PAUSED/CANCELLED) entre l'envoi du message et la réponse : aucune
            # commande ne doit en naître.
            if occurrence_id is not None:
                return self._proposal_refused("NEED_INACTIVE", need.id, occurrence_id, action)
            raise BusinessRuleException("Ce besoin n'est plus actif.", reason="need_inactive")

        occ_stmt = select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == need.id)
        if occurrence_id is not None:
            try:
                occ_uuid = uuid.UUID(str(occurrence_id))
            except (TypeError, ValueError):
                return self._proposal_refused("NOT_FOUND", need.id, occurrence_id, action)
            occ_stmt = occ_stmt.where(RecurringNeedOccurrence.id == occ_uuid)
        else:
            occ_stmt = occ_stmt.where(
                RecurringNeedOccurrence.status.in_(("OPEN", "MATCHED")),
                RecurringNeedOccurrence.quantity_matched > 0,
            ).order_by(RecurringNeedOccurrence.occurrence_date.asc())
        occurrence = await current_session.scalar(occ_stmt.limit(1).with_for_update())

        if occurrence_id is not None:
            # Revalidation de la proposition EXACTE (jamais « la prochaine occurrence ouverte »).
            if occurrence is None:
                return self._proposal_refused("NOT_FOUND", need.id, occurrence_id, action)
            if occurrence.status == "EXPIRED":
                return self._proposal_refused("EXPIRED", need.id, occurrence.id, action)
            if occurrence.status not in ("OPEN", "MATCHED"):
                return self._proposal_refused(
                    "ALREADY_PROCESSED", need.id, occurrence.id, action, occurrence_status=occurrence.status
                )
            if _occurrence_is_past(occurrence):
                return self._proposal_refused("EXPIRED", need.id, occurrence.id, action)
            # GARDE PRINCIPALE (B12) : la version MÉTIER vue par l'acheteur (digest ou écran détail)
            # doit être EXACTEMENT la version courante — `matching` ne la bumpe que si l'ensemble des
            # allocations change réellement. Même contrat pour ACCEPT et REJECT (un REJECT ne doit
            # jamais refuser silencieusement une NOUVELLE version que l'acheteur n'a pas vue).
            logger.info(
                "RECURRING_PROPOSAL_VERSION_CHECK | recurring_need_id=%s | occurrence_id=%s | "
                "expected_version=%s | current_version=%s | action=%s | result=%s",
                need.id, occurrence.id, expected_version, occurrence.version, action,
                "NOT_PROVIDED" if expected_version is None
                else ("MATCH" if int(occurrence.version) == int(expected_version) else "MISMATCH"),
            )
            if expected_version is not None:
                if int(occurrence.version) != int(expected_version):
                    return self._proposal_refused(
                        "PROPOSAL_CHANGED", need.id, occurrence.id, action,
                        expected_version=int(expected_version), current_version=int(occurrence.version),
                    )
            else:
                # REPLI TRANSITOIRE uniquement (digest envoyé AVANT que son payload porte les versions) :
                # sans version attendue, on retombe sur l'heuristique temporelle. Jamais la garde
                # principale ; à supprimer une fois les digests antérieurs à B12 écoulés.
                if occurrence.notified_at is None:
                    return self._proposal_refused(
                        "NO_PROPOSAL", need.id, occurrence.id, action, reason="not_notified"
                    )
                if occurrence.updated_at is not None and occurrence.updated_at > occurrence.notified_at + timedelta(
                    seconds=2
                ):
                    return self._proposal_refused("PROPOSAL_CHANGED", need.id, occurrence.id, action)
            if float(occurrence.quantity_matched or 0) <= 0:
                return self._proposal_refused("NO_PROPOSAL", need.id, occurrence.id, action)
        else:
            if occurrence is None:
                raise BusinessRuleException("Aucune proposition en attente pour ce besoin.")

        allocations = (
            (
                await current_session.execute(
                    select(NeedAllocation)
                    .where(
                        NeedAllocation.occurrence_id == occurrence.id,
                        NeedAllocation.status == "PROPOSED",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if not allocations:
            if occurrence_id is not None:
                return self._proposal_refused("NO_PROPOSAL", need.id, occurrence.id, action)
            raise BusinessRuleException("Aucune proposition en attente pour ce besoin.")

        if action == "ACCEPT" and occurrence_id is not None:
            # Pré-validation du stock AVANT toute création : un stock modifié depuis la proposition
            # ne doit jamais produire de commande partielle ni de débit négatif.
            short: List[str] = []
            unavailable: List[str] = []
            for alloc in allocations:
                product = await current_session.get(Product, alloc.product_id)
                label = str(getattr(product, "name", None) or alloc.product_id)
                # « Producteur/produit indisponible » = signaux DÉJÀ présents dans le modèle (aucun
                # nouveau système) : produit retiré de la vente (`is_available`), compte producteur
                # bloqué (`User.account_status`).
                if product is None or getattr(product, "is_available", True) is False:
                    unavailable.append(label)
                    continue
                if await self._producer_account_blocked(current_session, alloc.producer_id):
                    unavailable.append(label)
                    continue
                if float(product.quantity_for_sale or 0.0) < float(alloc.quantity):
                    short.append(label)
            if unavailable:
                return self._proposal_refused(
                    "PRODUCER_UNAVAILABLE", need.id, occurrence.id, action, products=unavailable
                )
            if short:
                return self._proposal_refused(
                    "STOCK_CHANGED", need.id, occurrence.id, action, products=short
                )

        if action == "REJECT":
            for alloc in allocations:
                alloc.status = "REJECTED"
            occurrence.status = "REJECTED"
            occurrence.version = occurrence.version + 1
            await current_session.flush()
            logger.info(
                "RECURRING_PROPOSAL_REJECTED | recurring_need_id=%s | occurrence_id=%s | exact=%s",
                need.id, occurrence.id, occurrence_id is not None,
            )
            logger.info("recurring_need.match_rejected | occurrence_id=%s", occurrence.id)
            return {"status": "success", "occurrence_id": str(occurrence.id), "action": "REJECT"}

        # ACCEPT — une commande par producteur, débit du stock produit par produit.
        order_group_id = uuid.uuid4()
        by_producer: Dict[str, List[NeedAllocation]] = {}
        for alloc in allocations:
            by_producer.setdefault(str(alloc.producer_id), []).append(alloc)

        created_order_ids: List[str] = []
        converted_quantity = 0.0
        for allocs in by_producer.values():
            order = Order(
                id=uuid.uuid4(),
                buyer_id=buyer_id,
                zone_id=getattr(user_obj, "zone_id", None),
                customer_name=getattr(buyer_profile, "establishment_name", None)
                or getattr(user_obj, "name", None),
                customer_phone=normalize_phone(str(phone), required=False),
                total_amount=0.0,
                subtotal=0.0,
                status="PENDING_PRODUCER_CONFIRMATION",
                payment_status="PENDING",
                delivery_status="PENDING",
                payment_method="CASH",
                source="WHATSAPP",
                order_type="RECURRING_SUPPLY",
                is_agent_order=True,
                expected_fulfillment_date=occurrence.occurrence_date,
                checkout_group_id=order_group_id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            current_session.add(order)
            await current_session.flush()

            order_total = 0.0
            for alloc in allocs:
                # B17 : verrou d'écriture (le débit lit-puis-écrit le stock) ET refus d'un produit à conditionnements —
                # le récurrent alloue en UNITÉ DE BASE à un prix par unité ; il ne sait ni débiter le compte d'une
                # variante ni facturer un prix par sachet/bidon (voir `need_matching_service._CANDIDATES_SQL`).
                product = await current_session.get(Product, alloc.product_id, with_for_update=True)
                if product is None:
                    raise BusinessRuleException("Produit introuvable pour une allocation.")
                if sells_by_package(getattr(product, "pricing_tiers", None)):
                    raise BusinessRuleException(
                        f"« {product.name} » se vend par conditionnement : non disponible en approvisionnement "
                        "récurrent.",
                        reason="recurring_package_product_unsupported",
                    )
                available = float(product.quantity_for_sale or 0.0)
                debit = float(alloc.quantity)
                if available < debit:
                    raise BusinessRuleException(
                        f"Stock insuffisant pour {product.name} (disponible : {available})."
                    )
                product.quantity_for_sale = available - debit
                await BusinessEventEmitter(current_session).emit_product_quantity_changed(
                    product, previous_quantity=available, source="order_debit_recurring"
                )

                item = OrderItem(
                    id=uuid.uuid4(),
                    order_id=order.id,
                    product_id=product.id,
                    quantity=debit,
                    price_at_sale=float(alloc.unit_price),
                    # Phase B2a : le prix et l'unité de l'ALLOCATION (convenus à l'appariement), pas
                    # ceux — éventuellement modifiés depuis — du produit.
                    **order_item_snapshot_columns(
                        product,
                        quantity=debit,
                        price_at_sale=float(alloc.unit_price),
                        unit=str(getattr(alloc, "unit", None) or getattr(product, "unit", None) or "KG"),
                        unit_price_override=float(alloc.unit_price),
                        price_source="RECURRING_ALLOCATION",
                    ),
                )
                current_session.add(item)
                await current_session.flush()

                alloc.status = "CONVERTED"
                alloc.order_item_id = item.id
                order_total += debit * float(alloc.unit_price)
                converted_quantity += debit

            order.subtotal = order_total
            order.total_amount = order_total
            created_order_ids.append(str(order.id))

        occurrence.quantity_confirmed = converted_quantity
        occurrence.order_group_id = order_group_id
        occurrence.accepted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        occurrence.status = (
            "ACCEPTED"
            if converted_quantity >= float(occurrence.requested_quantity)
            else "PARTIALLY_ACCEPTED"
        )
        occurrence.version = occurrence.version + 1
        await current_session.flush()

        # Analytics Phase C : émis APRÈS la persistance réelle de l'acceptation (commandes créées,
        # allocations CONVERTED, occurrence ACCEPTED), même transaction. Clé = l'occurrence : elle
        # ne peut être acceptée qu'UNE fois (le statut sort de OPEN/MATCHED), donc un message
        # WhatsApp rejoué échoue plus haut (BusinessRuleException) et, à défaut, ON CONFLICT DO NOTHING.
        await BusinessEventEmitter(current_session).emit(
            event_name=BusinessEventName.RECURRING_DIGEST_ACCEPTED,
            journey=Journey.RECURRING,
            actor_type="BUYER",
            actor_id=user_obj.id,
            buyer_id=buyer_id,
            zone_id=getattr(user_obj, "zone_id", None),
            entity_type="RECURRING_OCCURRENCE",
            entity_id=occurrence.id,
            idempotency_key=f"RECURRING_DIGEST_ACCEPTED:{occurrence.id}",
            sub_category_id=need.sub_category_id,
            quantity=converted_quantity,
            unit=occurrence.unit,
            amount=sum(float(a.quantity) * float(a.unit_price) for a in allocations),
            metadata={
                "recurring_need_id": str(need.id),
                "order_group_id": str(order_group_id),
                "occurrence_status": occurrence.status,
            },
        )

        logger.info(
            "RECURRING_PROPOSAL_ACCEPTED | recurring_need_id=%s | occurrence_id=%s | exact=%s | "
            "orders=%s | quantity=%s | result=%s",
            need.id, occurrence.id, occurrence_id is not None, len(created_order_ids),
            converted_quantity, occurrence.status,
        )
        logger.info(
            "recurring_need.match_accepted | occurrence_id=%s | orders=%s | quantity=%s",
            occurrence.id, created_order_ids, converted_quantity,
        )
        return {
            "status": "success",
            "occurrence_id": str(occurrence.id),
            "action": "ACCEPT",
            "order_ids": created_order_ids,
            "quantity_confirmed": converted_quantity,
        }

    # ─── FULFILLMENT (B14) : fermeture déterministe de l'occurrence ──────────────────────

    async def _recompute_occurrence_fulfillment_for_order(
        self, order: Any, *, reason: str = "order_changed", require_recurring: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Recalcule l'occurrence d'une commande RECURRING_SUPPLY (même transaction que le changement
        d'état de la commande). Aucun effet pour une commande d'un autre type ou sans lignée."""
        if require_recurring and str(getattr(order, "order_type", "") or "").upper() != "RECURRING_SUPPLY":
            return None
        group_id = getattr(order, "checkout_group_id", None)
        if group_id is None:
            return None
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        occurrence = await current_session.scalar(
            select(RecurringNeedOccurrence)
            .where(RecurringNeedOccurrence.order_group_id == group_id)
            .with_for_update()
        )
        if occurrence is None:
            return None
        return await self._recompute_occurrence_fulfillment(occurrence, reason=reason)

    async def _recompute_occurrence_fulfillment(self, occurrence: Any, *, reason: str = "recompute") -> Dict[str, Any]:
        """PRIMITIVE UNIQUE du fulfillment d'une occurrence (B14) — l'occurrence est DÉJÀ verrouillée.

        Source de vérité = les lignes des commandes issues de SES allocations `CONVERTED`
        (`need_allocations.order_item_id` -> `order_items` -> `orders`), jamais un incrément :
        un rejeu produit exactement le même résultat (idempotent).

        - `quantity_delivered` = somme des quantités (en UNITÉ DE BASE, `COALESCE(base_unit_quantity,
          quantity)`) des commandes `delivery_status == RECEIVED` et non annulées — contrat INCHANGÉ
          (lu par l'analytics et le contrôle de dérive). `RECEIVED_WITH_ISSUE` n'est pas compté.
          Une ligne dont l'unité diffère de celle de l'occurrence n'est JAMAIS sommée (pas de kg+L).
        - Une commande est TERMINALE si : annulée, RECEIVED, ou RECEIVED_WITH_ISSUE (résolution humaine).
          Tout le reste (attente producteur, CONFIRMED, en route, livrée sans réception) est EN COURS.
        - Statut final, UNIQUEMENT depuis ACCEPTED/PARTIALLY_ACCEPTED et seulement quand TOUTES les
          commandes sont terminales : livré >= demandé -> FULFILLED ; 0 < livré < demandé ->
          PARTIALLY_FULFILLED ; livré == 0 -> UNFULFILLED. Tant qu'une commande est en cours : aucun
          statut terminal. Le paiement n'intervient pas (fulfillment PHYSIQUE).
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        rows = (
            await current_session.execute(
                select(
                    Order.id,
                    Order.status,
                    Order.delivery_status,
                    func.coalesce(OrderItem.base_unit_quantity, OrderItem.quantity),
                    NeedAllocation.unit,
                    Order.cancellation_role,
                    Order.checkout_group_id,
                )
                .select_from(NeedAllocation)
                .join(OrderItem, OrderItem.id == NeedAllocation.order_item_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(NeedAllocation.occurrence_id == occurrence.id, NeedAllocation.status == "CONVERTED")
            )
        ).all()

        occ_unit = str(occurrence.unit or "").upper()
        order_state: Dict[Any, str] = {}
        current_attempt: Dict[Any, tuple] = {}  # B28 : les commandes de la tentative COURANTE (`order_group_id`)
        delivered = 0.0
        for oid, o_status, d_status, qty, unit, cancel_role, group_id in rows:
            state = _order_fulfillment_state(o_status, d_status)
            order_state[oid] = state
            if occurrence.order_group_id is None or group_id == occurrence.order_group_id:
                current_attempt[oid] = (state, str(cancel_role or "").upper())
            if state == "DELIVERED":
                if str(unit or "").upper() == occ_unit:
                    delivered += float(qty or 0)
                else:
                    logger.warning(
                        "RECURRING_FULFILLMENT_UNIT_MISMATCH | occurrence_id=%s | order_id=%s | line_unit=%s | occurrence_unit=%s",
                        occurrence.id, oid, unit, occ_unit,
                    )
        previous_status = str(occurrence.status)
        previous_delivered = float(occurrence.quantity_delivered or 0)
        requested = float(occurrence.requested_quantity or 0)
        all_terminal = bool(order_state) and all(s != "ACTIVE" for s in order_state.values())

        new_status = previous_status
        if previous_status in ("ACCEPTED", "PARTIALLY_ACCEPTED") and all_terminal:
            if delivered >= requested - 1e-9 and requested > 0:
                new_status = "FULFILLED"
            elif delivered > 1e-9:
                new_status = "PARTIALLY_FULFILLED"
            else:
                new_status = "UNFULFILLED"

        # B28 — ÉCHEC D'UNE TENTATIVE ≠ ÉCHEC DE L'OCCURRENCE. Quand TOUTES les commandes de la tentative courante sont
        # tombées côté producteur/système (timeout, refus, annulation) et que rien n'a été livré, la livraison est encore
        # due : si la fenêtre est ouverte et le besoin actif, l'occurrence redevient cherchable (OPEN) ; sinon elle reste
        # UNFULFILLED (clôture honnête). Une annulation par l'ACHETEUR, une livraison ou une réception litigieuse ne se
        # récupèrent jamais : c'est le choix ou le fait de l'acheteur.
        recovery: Optional[Dict[str, Any]] = None
        if new_status == "UNFULFILLED" and previous_status in ("ACCEPTED", "PARTIALLY_ACCEPTED"):
            attempt_failed = bool(current_attempt) and all(
                st == "CANCELLED" and role in _ATTEMPT_FAILED_BY for st, role in current_attempt.values()
            ) and not any(st in ("DELIVERED", "ISSUE") for st in order_state.values())
            if attempt_failed:
                need_status = await current_session.scalar(
                    select(RecurringNeed.status).where(RecurringNeed.id == occurrence.recurring_need_id)
                )
                reason = await self._failure_reason(list(current_attempt))
                decision = can_recover_occurrence(
                    occurrence, need_status=str(need_status or ""), today=_today(), sourcing_failed=True, user_initiated=False
                )
                recovery = {"outcome": decision.outcome, "reason": reason,
                            "recurring_need_id": str(occurrence.recurring_need_id), "occurrence_id": str(occurrence.id),
                            "occurrence_date": occurrence.occurrence_date.date().isoformat()}
                if decision.recoverable:
                    new_status = "OPEN"
                _log_recovery("REQUESTED", occurrence, source="FAILURE", reason=reason,
                              recoverable=decision.recoverable, outcome=decision.outcome)

        changed = abs(delivered - previous_delivered) > 1e-9 or new_status != previous_status
        if changed:
            occurrence.quantity_delivered = delivered
            occurrence.status = new_status
            if new_status == "OPEN":  # tentative échouée = historique ; l'occurrence repart de zéro (aucune réservation vivante)
                occurrence.quantity_confirmed = 0
                occurrence.quantity_matched = 0
            occurrence.version = int(occurrence.version) + 1
            await current_session.flush()
            if recovery is not None:
                recovery["occurrence_version"] = int(occurrence.version)
                _log_recovery("APPLIED" if new_status == "OPEN" else "SKIPPED", occurrence, source="FAILURE",
                              reason=recovery["reason"], recoverable=new_status == "OPEN", outcome=recovery["outcome"])
        logger.info(
            "RECURRING_FULFILLMENT_RECOMPUTED | occurrence_id=%s | recurring_need_id=%s | orders=%s | "
            "active_orders=%s | requested_quantity=%s | delivered_quantity=%s | previous_status=%s | "
            "new_status=%s | changed=%s | reason=%s",
            occurrence.id, occurrence.recurring_need_id, len(order_state),
            sum(1 for s in order_state.values() if s == "ACTIVE"), requested, delivered, previous_status,
            new_status, changed, reason,
        )
        if new_status != previous_status:
            logger.info(
                "RECURRING_OCCURRENCE_%s | occurrence_id=%s | recurring_need_id=%s | requested_quantity=%s | "
                "delivered_quantity=%s | previous_status=%s | reason=%s",
                new_status, occurrence.id, occurrence.recurring_need_id, requested, delivered, previous_status, reason,
            )
        result: Dict[str, Any] = {"occurrence_id": str(occurrence.id), "status": new_status,
                                  "quantity_delivered": delivered, "changed": changed}
        if recovery is not None:
            recovery.setdefault("occurrence_version", int(occurrence.version))
            result["recovery"] = recovery
        return result

    async def _failure_reason(self, order_ids: List[Any]) -> str:
        """Raison structurée de l'échec d'une tentative, lue dans l'historique durable des commandes (note écrite par le
        service qui a fait échouer la commande) — jamais déduite d'un texte libre."""
        notes = (
            await self._db().execute(
                select(OrderStatusHistory.note)
                .where(OrderStatusHistory.order_id.in_(order_ids), OrderStatusHistory.status_type == "ORDER",
                       OrderStatusHistory.to_status == "CANCELLED")
                .order_by(OrderStatusHistory.created_at.desc())
            )
        ).scalars().all()
        for note in notes:
            if note in _FAILURE_NOTE_TO_REASON:
                return _FAILURE_NOTE_TO_REASON[str(note)]
        return "PRODUCER_CANCELLED"

    async def reconcile_recurring_fulfillment(self, limit: int = 200) -> Dict[str, Any]:
        """Filet idempotent (cron) : recalcule les occurrences encore `ACCEPTED`/`PARTIALLY_ACCEPTED` — répare
        une commande terminée AVANT B14 (ou modifiée hors des chemins instrumentés) sans jamais rien
        ré-incrémenter. `SKIP LOCKED` : une transaction en cours (livraison/annulation) gagne."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        rows = (
            await current_session.execute(
                select(RecurringNeedOccurrence)
                .where(RecurringNeedOccurrence.status.in_(("ACCEPTED", "PARTIALLY_ACCEPTED")))
                .order_by(RecurringNeedOccurrence.accepted_at.asc())
                .limit(int(limit))
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        closed = 0
        for occ in rows:
            res = await self._recompute_occurrence_fulfillment(occ, reason="reconciliation")
            if res["status"] not in ("ACCEPTED", "PARTIALLY_ACCEPTED"):
                closed += 1
        return {"occurrences_examined": len(rows), "occurrences_closed": closed}

    async def expire_unconfirmed_recurring_orders(self) -> Dict[str, Any]:
        """TIMEOUT PRODUCTEUR (B14) : une commande RECURRING_SUPPLY encore `PENDING_PRODUCER_CONFIRMATION`
        alors que sa date de livraison (`expected_fulfillment_date`) est STRICTEMENT passée ne sera plus jamais
        honorée : elle passe `CANCELLED` (`cancellation_role="SYSTEM"`), le stock débité à l'acceptation est
        RESTITUÉ (même calcul que l'annulation), l'acheteur est notifié (Outbox) et l'occurrence est recalculée.

        Règle dérivée : par défaut le délai de confirmation est la date de livraison elle-même
        (`RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS` = 0, B28 : valeur > 0 = échéance AVANT la livraison, donc livraison encore
        récupérable). Idempotent : le verrou `FOR UPDATE SKIP LOCKED` + le garde de statut empêchent tout double recrédit.

        B28 : l'échec de CETTE tentative ne tue pas l'occurrence — `_recompute_occurrence_fulfillment_for_order` la rouvre
        (OPEN) si la fenêtre le permet ; la notification dit honnêtement laquelle des deux situations s'applique."""
        from ladini.core.settings import settings

        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        lead_days = max(0, int(settings.RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS))
        today_dt = datetime.combine(_today() + timedelta(days=lead_days), datetime.min.time())
        orders = (
            await current_session.execute(
                select(Order)
                .where(
                    Order.order_type == "RECURRING_SUPPLY",
                    Order.status == "PENDING_PRODUCER_CONFIRMATION",
                    Order.expected_fulfillment_date < today_dt,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        expired_ids: List[str] = []
        for order in orders:
            items = (
                await current_session.execute(select(OrderItem).where(OrderItem.order_id == order.id))
            ).scalars().all()
            product_ids = sorted({i.product_id for i in items if i.product_id}, key=str)
            products = {
                p.id: p
                for p in (
                    await current_session.scalars(
                        select(Product).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()
                    )
                ).all()
            } if product_ids else {}
            for item in items:
                product = products.get(item.product_id)
                if product is not None:
                    previous = float(product.quantity_for_sale or 0.0)
                    restore_stock_for_item(product, item)  # B16 : stock physique ET compte d'une variante éventuelle
                    await BusinessEventEmitter(current_session).emit_product_quantity_changed(
                        product, previous_quantity=previous, source="recurring_confirmation_expired_recredit"
                    )
            order.status = "CANCELLED"
            order.cancellation_role = "SYSTEM"
            current_session.add(
                OrderStatusHistory(
                    id=uuid.uuid4(), order_id=order.id, status_type="ORDER",
                    from_status="PENDING_PRODUCER_CONFIRMATION", to_status="CANCELLED",
                    actor_id=None, note="producer_confirmation_expired",
                )
            )
            await current_session.flush()
            logger.info(
                "RECURRING_ORDER_CONFIRMATION_EXPIRED | order_id=%s | checkout_group_id=%s | items=%s",
                order.id, order.checkout_group_id, len(items),
            )
            recomputed = await self._recompute_occurrence_fulfillment_for_order(order, reason="producer_confirmation_expired")
            await self._notify_attempt_failure(order, reason_text="Le producteur n'a pas confirmé à temps.",
                                               recovery=(recomputed or {}).get("recovery"))
            expired_ids.append(str(order.id))
        return {"recurring_orders_expired": len(expired_ids)}

    async def _attempt_failure_payload(self, recovery: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Champs de RÉCUPÉRATION ajoutés à la notification d'échec d'une commande récurrente (B27/B28) : identifiants, version
        de l'occurrence au moment de la notification (B26 : la commande de relance la portera), raison structurée et verdict du
        domaine (`RECOVERABLE` / `RECOVERY_WINDOW_CLOSED` / …). Rien d'autre : le texte est produit par le gabarit."""
        if not recovery:
            return {}
        label = await self._db().scalar(
            select(SubCategory.name).join(RecurringNeed, RecurringNeed.sub_category_id == SubCategory.id)
            .where(RecurringNeed.id == uuid.UUID(recovery["recurring_need_id"]))
        )
        return {
            "recovery_candidate": True,
            "recovery_outcome": recovery["outcome"],
            "failure_reason": recovery["reason"],
            "product": str(label or ""),
            "occurrences": [{
                "recurring_need_id": recovery["recurring_need_id"],
                "occurrence_id": recovery["occurrence_id"],
                "date": recovery["occurrence_date"],
                "occurrence_version": recovery.get("occurrence_version"),
            }],
        }

    async def _notify_attempt_failure(self, order: Any, *, reason_text: str, recovery: Optional[Dict[str, Any]]) -> None:
        """Notification Outbox de l'acheteur après l'échec système d'une commande récurrente (timeout producteur)."""
        current_session = self._db()
        buyer_phone = (
            await current_session.execute(
                select(User.phone).join(BuyerProfile, BuyerProfile.user_id == User.id)
                .where(BuyerProfile.id == order.buyer_id).limit(1)
            )
        ).scalar_one_or_none()
        if not buyer_phone:
            return
        from ladini.workers.outbox import templates as _outbox_templates
        from ladini.workers.repositories import outbox_repo as _outbox_repo

        await _outbox_repo.enqueue(
            current_session,
            [{
                "channel": "WHATSAPP", "recipient_phone": buyer_phone,
                "template_key": _outbox_templates.ORDER_CANCELLED_BY_PRODUCER_BUYER,
                "payload": {"order_number": str(order.id)[:8].upper(), "reason": reason_text,
                            **await self._attempt_failure_payload(recovery)},
                "dedupe_key": f"ORDER_CANCELLED_BUYER:{order.id}",
            }],
        )
        await current_session.flush()

    @staticmethod
    async def _producer_account_blocked(session: Any, producer_id: Any) -> bool:
        producer = await session.get(Producer, producer_id)
        user_id = getattr(producer, "user_id", None)
        if user_id is None:
            return False
        user = await session.get(User, user_id)
        return str(getattr(user, "account_status", "") or "").upper() in ("BLOCKED", "BANNED")

    async def expire_past_occurrences(self) -> Dict[str, Any]:
        """NO-RESPONSE (B12) — une occurrence `OPEN`/`MATCHED` dont la date est STRICTEMENT passée ne
        sera plus jamais livrable : elle passe `EXPIRED` (statut déjà permis par la contrainte), ses
        allocations `PROPOSED` aussi, `version` bumpée. Rien n'écrivait jamais `EXPIRED` : l'occurrence
        restait ouverte pour toujours et, étant « la plus proche », MASQUAIT la vraie prochaine.

        `FOR UPDATE SKIP LOCKED` : une réponse « oui » qui détient déjà la ligne (accept) gagne et le
        balayage la saute ; si le balayage gagne, l'accept qui suit voit `EXPIRED` et se refuse sans
        effet. Idempotent (un rejeu ne trouve plus rien)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        today_dt = datetime.combine(_today(), datetime.min.time())
        rows = (
            await current_session.execute(
                select(RecurringNeedOccurrence)
                .where(
                    RecurringNeedOccurrence.status.in_(("OPEN", "MATCHED")),
                    RecurringNeedOccurrence.occurrence_date < today_dt,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        expired_ids = []
        for occ in rows:
            occ.status = "EXPIRED"
            occ.version = int(occ.version) + 1
            expired_ids.append(occ.id)
            logger.info(
                "RECURRING_NO_RESPONSE_EXPIRED | recurring_need_id=%s | occurrence_id=%s | notified=%s | "
                "result=EXPIRED | reason=occurrence_date_passed",
                occ.recurring_need_id, occ.id, occ.notified_at is not None,
            )
        if expired_ids:
            await current_session.execute(
                update(NeedAllocation)
                .where(NeedAllocation.occurrence_id.in_(expired_ids), NeedAllocation.status == "PROPOSED")
                .values(status="EXPIRED")
            )
            await current_session.flush()
        return {"occurrences_expired": len(expired_ids)}

    async def _latest_digest_snapshot(self, user_phone: Any) -> Optional[Dict[str, int]]:
        """Snapshot du DERNIER digest envoyé à cet acheteur : `{occurrence_id: version}` tels que son
        message les décrivait (payload outbox, B12 — aucune migration). `None` si aucun digest, ou si
        le dernier digest est antérieur à B12 (payload sans `occurrences`) : l'appelant retombe alors
        sur le repli transitoire."""
        current_session = self.session
        if not current_session or not user_phone:
            return None
        payload = await current_session.scalar(
            select(NotificationOutbox.payload)
            .where(
                NotificationOutbox.recipient_phone == str(user_phone),
                NotificationOutbox.template_key == "RECURRING_SUPPLY_DIGEST_BUYER",
            )
            .order_by(NotificationOutbox.created_at.desc())
            .limit(1)
        )
        occurrences = (payload or {}).get("occurrences") if isinstance(payload, dict) else None
        if not isinstance(occurrences, list):
            return None
        return {
            str(o["occurrence_id"]): int(o["version"])
            for o in occurrences
            if isinstance(o, dict) and o.get("occurrence_id") is not None and o.get("version") is not None
        }

    @staticmethod
    def _proposal_refused(
        outcome: str, need_id: Any, occurrence_id: Any, action: str, **extra: Any
    ) -> Dict[str, Any]:
        """Refus MÉTIER d'une réponse à une proposition exacte — aucune mutation (rien à annuler).
        `status="success"` + `outcome` (même convention que `ALREADY_COMPLETED`, B11) : l'appelant
        distingue « confirmé » de « refusé » sur `outcome`, jamais sur une exception opaque."""
        logger.info(
            "RECURRING_PROPOSAL_REVALIDATED | recurring_need_id=%s | occurrence_id=%s | action=%s | "
            "result=%s | reason=%s",
            need_id, occurrence_id, action, "REFUSED", outcome,
        )
        if outcome == "EXPIRED":
            logger.info(
                "RECURRING_PROPOSAL_EXPIRED | recurring_need_id=%s | occurrence_id=%s", need_id, occurrence_id
            )
        return {
            "status": "success",
            "outcome": outcome,
            "action": action,
            "occurrence_id": str(occurrence_id),
            "recurring_need_id": str(need_id),
            **extra,
        }

    # ─── LIVRAISON / RÉCEPTION (VS5 pilote) ────────────────────────────

    async def mark_order_delivery_status(
        self, phone: str, order_id: str, action: str
    ) -> Dict[str, Any]:
        """PRODUCTEUR — fait avancer `Order.delivery_status` d'UN cran :
        `PENDING -> IN_TRANSIT` (« en route ») ou `IN_TRANSIT -> DELIVERED`
        (« livré »). Réutilise `Order.delivery_status` tel quel (aucune
        nouvelle colonne/table) — seules de NOUVELLES VALEURS de texte libre
        s'y ajoutent (aucune contrainte CHECK sur cette colonne). Historisé
        via `OrderStatusHistory` (`status_type="DELIVERY"`), déjà la
        convention de ce dépôt (voir `ProducerMgmtMixin.confirm_delivery_
        and_payment`), jamais une nouvelle table.

        Scope STRICT `order_type == "RECURRING_SUPPLY"` : les autres
        parcours de livraison (préorder/RFQ paiement-à-la-livraison, voir
        `confirm_delivery_and_payment`) ne passent JAMAIS par cette méthode
        — mandat §5/CAS 12, aucune régression sur le flux historique.

        Idempotent : rejouer la MÊME transition (double "en route"/"livré")
        renvoie un succès inchangé, jamais une erreur ni une 2ᵉ écriture
        d'historique. Une transition hors séquence (ex: PENDING -> DELIVERED
        directement, ou depuis un état déjà terminal) est refusée."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        action = str(action or "").upper().strip()
        if action not in _DELIVERY_TRANSITIONS:
            raise BusinessRuleException(f"Action inconnue : {action}")
        expected_from, to_status = _DELIVERY_TRANSITIONS[action]

        _user_obj, producer_profile = await self.get_producer_profile(phone=str(phone))
        order = await self._load_recurring_supply_order_for_update(order_id)

        owns = await current_session.scalar(
            select(OrderItem.id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(OrderItem.order_id == order.id, Product.producer_id == producer_profile.id)
            .limit(1)
        )
        if owns is None:
            raise BusinessRuleException(
                "Commande introuvable ou non autorisée.", reason="not_owner"
            )
        if str(order.status or "").upper() == "CANCELLED":
            raise BusinessRuleException("Cette commande est annulée.", reason="order_cancelled")

        current = str(order.delivery_status or "PENDING").upper()
        if current == to_status:
            return {
                "status": "success",
                "outcome": "UNCHANGED",
                "order_id": str(order.id),
                "delivery_status": current,
            }
        if current != expected_from:
            raise BusinessRuleException(
                f"Transition invalide (statut actuel : {current}).",
                reason="invalid_transition",
            )

        order.delivery_status = to_status
        current_session.add(
            OrderStatusHistory(
                id=uuid.uuid4(),
                order_id=order.id,
                status_type="DELIVERY",
                from_status=expected_from,
                to_status=to_status,
                actor_id=producer_profile.id,
            )
        )

        if to_status == "DELIVERED":
            # Demande la réception — jamais une simple information passive (mandat ÉTAPE 6).
            # Même mécanique Outbox que `confirm_order_by_producer` (MÊME transaction).
            buyer_row = (
                await current_session.execute(
                    select(User.phone)
                    .join(BuyerProfile, BuyerProfile.user_id == User.id)
                    .where(BuyerProfile.id == order.buyer_id)
                    .limit(1)
                )
            ).first()
            buyer_phone = buyer_row[0] if buyer_row else None
            if buyer_phone:
                from ladini.workers.outbox import templates as _outbox_templates
                from ladini.workers.repositories import outbox_repo as _outbox_repo

                await _outbox_repo.enqueue(
                    current_session,
                    [
                        {
                            "channel": "WHATSAPP",
                            "recipient_phone": buyer_phone,
                            "template_key": _outbox_templates.RECURRING_SUPPLY_ORDER_DELIVERED_BUYER,
                            "payload": {"order_number": str(order.id)[:8].upper()},
                            "dedupe_key": f"RECURRING_SUPPLY_DELIVERED_BUYER:{order.id}",
                        }
                    ],
                )

        await current_session.flush()
        logger.info(
            "recurring_supply.delivery_status_updated | order_id=%s | %s -> %s",
            order.id, expected_from, to_status,
        )
        return {
            "status": "success",
            "outcome": "UPDATED",
            "order_id": str(order.id),
            "delivery_status": to_status,
        }

    async def record_order_reception(
        self,
        phone: str,
        order_id: str,
        outcome: str,
        issue_type: Optional[str] = None,
        detail: Optional[str] = None,
        received_quantity: Optional[float] = None,
    ) -> Dict[str, Any]:
        """ACHETEUR — enregistre la réception d'une commande `DELIVERED` :
        `RECEIVED` (« tout est bon ») ou `RECEIVED_WITH_ISSUE` (« il y a un
        problème »). `Order.status` passe `COMPLETED` UNIQUEMENT sur
        `RECEIVED` — un `RECEIVED_WITH_ISSUE` laisse `status` inchangé
        (`CONFIRMED`) : la résolution reste HUMAINE (mandat §8, jamais de
        remboursement/avoir/pénalité automatique), le statut le signale.

        L'incident (type/détail/quantité reçue) est stocké dans
        `OrderStatusHistory.note`, en JSON — AUCUNE nouvelle table
        (`OrderDispute` existe mais est un mécanisme ESCROW, couplé à
        `escrow_wallet_id`/`disputed_amount`/`escrow_payout_status`, hors de
        propos pour un incident non-monétaire sur une commande payée hors
        plateforme — mandat §7 : réutiliser seulement ce qui convient
        RÉELLEMENT, jamais forcer un mauvais ajustement).

        Idempotent : une commande déjà `RECEIVED`/`RECEIVED_WITH_ISSUE` ne
        peut plus être re-réceptionnée — le premier résultat gagne,
        jamais un second signalement qui écraserait le premier."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        outcome = str(outcome or "").upper().strip()
        if outcome not in _RECEPTION_OUTCOMES:
            raise BusinessRuleException(f"Résultat inconnu : {outcome}")

        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        order = await self._load_recurring_supply_order_for_update(order_id)
        if str(order.buyer_id) != str(buyer_profile.id):
            raise BusinessRuleException(
                "Commande introuvable ou non autorisée.", reason="not_owner"
            )

        current = str(order.delivery_status or "PENDING").upper()
        if current in _RECEPTION_OUTCOMES:
            return {
                "status": "success",
                "outcome": "ALREADY_RECORDED",
                "order_id": str(order.id),
                "delivery_status": current,
            }
        if current != "DELIVERED":
            raise BusinessRuleException(
                f"Cette commande n'a pas encore été livrée (statut : {current}).",
                reason="not_delivered",
            )

        order.delivery_status = outcome
        note = None
        if outcome == "RECEIVED_WITH_ISSUE":
            note = json.dumps(
                {"issue_type": issue_type, "detail": detail, "received_quantity": received_quantity},
                ensure_ascii=False,
            )
        else:
            order.status = "COMPLETED"
            order.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)

        current_session.add(
            OrderStatusHistory(
                id=uuid.uuid4(),
                order_id=order.id,
                status_type="DELIVERY",
                from_status="DELIVERED",
                to_status=outcome,
                actor_id=buyer_profile.id,
                note=note,
            )
        )
        await current_session.flush()
        # B14 : les DEUX issues sont terminales pour l'occurrence ; seule RECEIVED compte dans `quantity_delivered`.
        await self._refresh_occurrence_quantity_delivered(order)
        logger.info(
            "recurring_supply.reception_recorded | order_id=%s | outcome=%s", order.id, outcome
        )
        return {
            "status": "success",
            "outcome": "RECORDED",
            "order_id": str(order.id),
            "delivery_status": outcome,
        }

    async def _refresh_occurrence_quantity_delivered(self, order: Order) -> Optional[float]:
        """Conservé pour compatibilité : délègue à la PRIMITIVE UNIQUE `_recompute_occurrence_fulfillment`
        (recalcul déterministe depuis les lignes terminales, jamais un incrément — B14)."""
        res = await self._recompute_occurrence_fulfillment_for_order(order, reason="reception", require_recurring=False)
        return None if res is None else float(res["quantity_delivered"])

    async def list_my_deliverable_orders(self, phone: str, delivery_status: str) -> Dict[str, Any]:
        """Lecture bornée, réservée au fast-path déterministe (VS5 pilote,
        `interpreter/routing.py::_bare_confirmation_for_order_reception`) :
        commandes `RECURRING_SUPPLY` de CET acheteur dans UN `delivery_
        status` précis. N'existe QUE parce qu'aucune méthode déjà en place
        (`get_buyer_orders_dashboard`, `get_active_orders_context_by_phone`)
        ne renvoie `order_type`/`delivery_status` de façon structurée —
        jamais un doublon d'une lecture déjà exploitable telle quelle."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        rows = (
            await current_session.execute(
                select(Order.id).where(
                    Order.buyer_id == buyer_profile.id,
                    Order.order_type == "RECURRING_SUPPLY",
                    Order.delivery_status == str(delivery_status or "").upper(),
                )
            )
        ).scalars().all()
        return {"status": "success", "items": [{"order_id": str(r)} for r in rows]}

    async def _load_recurring_supply_order_for_update(self, order_id: Any) -> Order:
        """Charge et verrouille (`FOR UPDATE`) une commande, en exigeant `order_type ==
        'RECURRING_SUPPLY'` — jamais un chemin de livraison partagé avec les autres types de
        commande (préorder/RFQ), qui restent gérés par leurs mécanismes existants inchangés."""
        current_session = self.session
        try:
            o_uuid = uuid.UUID(str(order_id))
        except (TypeError, ValueError):
            raise BusinessRuleException("Identifiant de commande invalide.") from None
        order = await current_session.scalar(
            select(Order).where(Order.id == o_uuid).with_for_update()
        )
        if order is None:
            raise BusinessRuleException("Commande introuvable.", reason="order_not_found")
        if str(order.order_type or "").upper() != "RECURRING_SUPPLY":
            raise BusinessRuleException(
                "Cette commande suit un autre parcours de livraison.",
                reason="wrong_order_type",
            )
        return order

    # ─── UPDATE ───────────────────────────────────────────────────────

    async def update_recurring_need(
        self,
        phone: str,
        recurring_need_id: str,
        action: str,
        *,
        quantity: Optional[float] = None,
        recurrence_type: Optional[str] = None,
        weekly_days: Optional[List[int]] = None,
        excluded_weekdays: Optional[List[int]] = None,
        paused_until: Optional[Any] = None,
        occurrence_date: Optional[Any] = None,
        expected_version: Optional[int] = None,
        expected_occurrence_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Mutation d'un besoin récurrent — SEULE autorité de cohérence (contrat : docs/RECURRING_MUTATION_CONSISTENCY_
        CONTRACT.md, « Version Contract »). Le besoin est verrouillé (`FOR UPDATE`) pour toute la transaction : deux
        mutations, ou une mutation et une matérialisation, se sérialisent ; tout se décide sur l'état COMMITÉ le plus récent.

        La version de l'état OBSERVÉ est OBLIGATOIRE (B26) — aucun « `None` = tout passe » : une mutation du besoin
        (`PERMANENT_QUANTITY|PERMANENT_FREQUENCY|PAUSE|RESUME|CANCEL`) exige `expected_version` (jeton du besoin lu via
        `list_my_recurring_needs`/`get_recurring_need_detail`) ; une mutation d'occurrence (`OCCURRENCE_SKIP|OVERRIDE`) exige
        `expected_occurrence_version` (version de l'occurrence affichée). Sans version : `BusinessRuleException(reason=
        "version_required")`, rien n'est lu ni écrit. Une version périmée : `{"status": "conflict", "outcome":
        "VERSION_CONFLICT", "current": {…}}`, aucune écriture, aucun effet de bord. Résultat d'une mutation réussie :
        `outcome` = `APPLIED` ou `ALREADY_APPLIED` (rejeu idempotent : ni écriture, ni version, ni audit)."""
        if action not in RECURRING_NEED_ACTIONS:
            raise BusinessRuleException(f"Action inconnue : {action!r}")
        occurrence_scope = action in RECURRING_OCCURRENCE_ACTIONS
        if (expected_occurrence_version if occurrence_scope else expected_version) is None:
            logger.warning(
                "RECURRING_VERSION_CHECK | action=%s | scope=%s | expected_version_present=False | outcome=VERSION_REQUIRED",
                action, "OCCURRENCE" if occurrence_scope else "NEED",
            )
            raise BusinessRuleException(
                "Version de l'état affiché manquante : la modification est refusée.", reason="version_required"
            )

        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        buyer_id = buyer_profile.id

        need = await current_session.scalar(
            select(RecurringNeed)
            .where(RecurringNeed.id == recurring_need_id, RecurringNeed.buyer_id == buyer_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if need is None:
            # Jamais de fuite d'existence (mandat §12) : même message qu'un id inexistant.
            raise BusinessRuleException("Besoin introuvable.")

        if expected_version is not None and int(expected_version) != recurring_need_version(need):
            self._log_version_check(action, "NEED", match=False)
            return {
                "status": "conflict",
                "outcome": "VERSION_CONFLICT",
                "scope": "NEED",
                "recurring_need_id": str(need.id),
                "expected_version": int(expected_version),
                "current": self._need_snapshot(need),
            }
        if not occurrence_scope:
            self._log_version_check(action, "NEED", match=True)
        if str(need.status) == "CANCELLED" and action != "CANCEL":
            raise BusinessRuleException("Ce besoin est arrêté.", reason="need_not_active")

        if action == "PERMANENT_QUANTITY":
            return await self._apply_permanent_quantity(need, quantity=quantity)
        if action == "PERMANENT_FREQUENCY":
            return await self._apply_permanent_frequency(
                need, recurrence_type=recurrence_type, weekly_days=weekly_days, excluded_weekdays=excluded_weekdays
            )
        if action == "PAUSE":
            return await self._apply_pause(need, paused_until=_parse_date(paused_until))
        if action == "RESUME":
            return await self._apply_resume(need)
        if action == "CANCEL":
            return await self._apply_cancel(need)
        if action == "OCCURRENCE_OVERRIDE":
            return await self._apply_occurrence_override(
                need, occurrence_date=_parse_date(occurrence_date), quantity=quantity,
                expected_occurrence_version=int(expected_occurrence_version),  # type: ignore[arg-type]
            )
        if action == "OCCURRENCE_SKIP":
            return await self._apply_occurrence_skip(
                need,
                occurrence_date=_parse_date(occurrence_date),
                actor_id=_user_obj.id,
                zone_id=getattr(_user_obj, "zone_id", None),
                expected_occurrence_version=int(expected_occurrence_version),  # type: ignore[arg-type]
            )
        raise AssertionError(action)  # pragma: no cover — filtré par RECURRING_NEED_ACTIONS ci-dessus

    # ── primitives de cohérence ───────────────────────────────────────

    @staticmethod
    def _need_snapshot(need: RecurringNeed) -> Dict[str, Any]:
        """État courant d'un besoin tel que montré à l'acheteur après un conflit (aucune donnée personnelle)."""
        return {
            "need_version": recurring_need_version(need),
            "status": need.status,
            "quantity": float(need.quantity),
            "unit": need.unit,
            "recurrence_type": need.recurrence_type,
            "weekly_days": list(need.weekly_days) if need.weekly_days else None,
            "paused_until": need.paused_until.date().isoformat() if need.paused_until else None,
        }

    @staticmethod
    def _log_version_check(action: str, scope: str, *, match: bool) -> None:
        """Observabilité du contrat de version (B26) : jamais le jeton brut, jamais d'identifiant d'acheteur."""
        logger.info(
            "RECURRING_VERSION_CHECK | action=%s | scope=%s | expected_version_present=True | version_match=%s | outcome=%s",
            action, scope, match, "OK" if match else "VERSION_CONFLICT",
        )

    def _occurrence_conflict(
        self, need: RecurringNeed, occ: RecurringNeedOccurrence, action: str, expected: int
    ) -> Dict[str, Any]:
        """Conflit de version d'OCCURRENCE : même forme que le conflit du besoin, plus l'état courant de la livraison."""
        self._log_version_check(action, "OCCURRENCE", match=False)
        return {
            "status": "conflict",
            "outcome": "VERSION_CONFLICT",
            "scope": "OCCURRENCE",
            "recurring_need_id": str(need.id),
            "expected_version": int(expected),
            "current": {
                **self._need_snapshot(need),
                "occurrence_id": str(occ.id),
                "occurrence_version": int(occ.version),
                "occurrence_status": occ.status,
                "occurrence_date": occ.occurrence_date.date().isoformat(),
                "requested_quantity": float(occ.requested_quantity),
            },
        }

    def _mutation_result(self, need: RecurringNeed, outcome: str, **extra: Any) -> Dict[str, Any]:
        return {
            "status": "success",
            "outcome": outcome,
            "recurring_need_id": str(need.id),
            "need_version": recurring_need_version(need),
            **extra,
        }

    async def _bump_need_version(self, need: RecurringNeed) -> None:
        """Nouvelle version = nouvel `updated_at` (`clock_timestamp()`, monotone sous le verrou de ligne). Pas de colonne
        ajoutée : `updated_at` est déjà le jeton de compare-and-swap du besoin (voir `recurring_need_version`)."""
        current_session = self._db()
        need.updated_at = func.clock_timestamp()
        await current_session.flush()
        await current_session.refresh(need, ["updated_at"])

    @staticmethod
    def _audit_mutation(need: RecurringNeed, action: str, old: Any, new: Any, *, actor: str = "BUYER") -> None:
        """Piste d'audit d'une mutation EFFECTIVE (qui/quoi/avant/après/quand) — journal structuré, sans téléphone ni nom.
        `analytics.business_events` a un CHECK fermé sur `event_name` (schéma Drizzle) : un événement dédié exige une
        migration côté frontend — voir « limites » du contrat."""
        logger.info(
            "recurring_need.mutation | recurring_need_id=%s | action=%s | old=%s | new=%s | version=%s | actor=%s | at=%s",
            need.id, action, old, new, recurring_need_version(need), actor, datetime.utcnow().isoformat(timespec="seconds"),
        )

    async def _reset_uncommitted_occurrences(
        self,
        need_id: Any,
        *where: Any,
        status: str = "OPEN",
        requested_quantity: Optional[float] = None,
        from_today: bool = True,
    ) -> List[Any]:
        """UNIQUE point qui réécrit des occurrences futures NON ENGAGÉES (`OPEN`/`MATCHED`, date ≥ aujourd'hui) : nouveau
        statut/quantité, `quantity_matched = 0`, `version + 1`, et les allocations `PROPOSED` (simples réservations
        logiques, aucun stock débité) passent `EXPIRED` — jamais d'allocation vivante au-delà de la quantité demandée, jamais
        de proposition périmée. ACCEPTED / PARTIALLY_ACCEPTED / terminales ne sont jamais touchées (le `WHERE` lit leur état
        committé, y compris après l'attente d'un verrou tenu par un matching ou une acceptation)."""
        current_session = self._db()
        values: Dict[str, Any] = {
            "status": status,
            "quantity_matched": 0,
            "version": RecurringNeedOccurrence.version + 1,
        }
        if requested_quantity is not None:
            values["requested_quantity"] = requested_quantity
        horizon = (RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),) if from_today else ()
        stmt = (
            update(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id == need_id,
                RecurringNeedOccurrence.status.in_(_UNCOMMITTED_OCCURRENCE_STATUSES),
                *horizon,
                *where,
            )
            .values(**values)
            .returning(RecurringNeedOccurrence.id)
        )
        ids = [row[0] for row in (await current_session.execute(stmt)).all()]
        if ids:
            await current_session.execute(
                update(NeedAllocation)
                .where(NeedAllocation.occurrence_id.in_(ids), NeedAllocation.status == "PROPOSED")
                .values(status="EXPIRED")
            )
        return ids

    # ── actions ───────────────────────────────────────────────────────

    async def _apply_permanent_quantity(self, need: RecurringNeed, *, quantity: Optional[float]) -> Dict[str, Any]:
        """Change le contrat. Les occurrences futures NON engagées qui suivaient l'ancien contrat (`requested == ancienne
        quantité`) le suivent ; une exception explicite (quantité différente) n'est JAMAIS écrasée ; ACCEPTED/terminales
        jamais réécrites. Une occurrence suivie perd sa proposition (à rematcher par le cron ou « rechercher »)."""
        if quantity is None or not 0 < float(quantity) < float("inf"):
            raise BusinessRuleException("Quantité invalide : un nombre supérieur à 0 est requis.", reason="invalid_quantity")
        new_quantity = float(quantity)
        old_quantity = need.quantity
        if float(old_quantity) == new_quantity:
            return self._mutation_result(need, "ALREADY_APPLIED", occurrences_updated=0)
        old_unit = need.unit
        need.quantity = new_quantity
        await self._bump_need_version(need)
        ids = await self._reset_uncommitted_occurrences(
            need.id,
            RecurringNeedOccurrence.requested_quantity == old_quantity,
            RecurringNeedOccurrence.unit == old_unit,
            requested_quantity=new_quantity,
        )
        self._audit_mutation(need, "PERMANENT_QUANTITY", f"{float(old_quantity):g}", f"{new_quantity:g}")
        logger.info("recurring_need.updated | recurring_need_id=%s | occurrences_updated=%s", need.id, len(ids))
        return self._mutation_result(need, "APPLIED", occurrences_updated=len(ids))

    async def _apply_permanent_frequency(
        self,
        need: RecurringNeed,
        *,
        recurrence_type: Optional[str],
        weekly_days: Optional[List[int]],
        excluded_weekdays: Optional[List[int]],
    ) -> Dict[str, Any]:
        """Change le planning. Les occurrences futures NON engagées, non exceptionnelles, qui ne sont plus dues sont
        `CANCELLED` (puis ré-ouvertes par la matérialisation si le planning revient) ; une exception explicite, une occurrence
        engagée ou terminale n'est jamais perdue. La nouvelle fenêtre est matérialisée dans la même transaction."""
        old_key = self._schedule_key(need)
        if recurrence_type is not None:
            need.recurrence_type = recurrence_type
        if weekly_days is not None:
            need.weekly_days = list(weekly_days)
        if excluded_weekdays is not None:
            need.excluded_weekdays = list(excluded_weekdays)
        new_key = self._schedule_key(need)
        if new_key == old_key:
            return self._mutation_result(need, "ALREADY_APPLIED", occurrences_cancelled=0)
        try:
            rule = self._recurrence_rule_for(need)
        except InvalidRecurrenceRule as exc:
            raise BusinessRuleException(f"Fréquence invalide : {exc}", reason="invalid_frequency") from exc
        await self._bump_need_version(need)

        current_session = self._db()
        candidates = (
            await current_session.execute(
                select(RecurringNeedOccurrence.id, RecurringNeedOccurrence.occurrence_date).where(
                    RecurringNeedOccurrence.recurring_need_id == need.id,
                    RecurringNeedOccurrence.status.in_(_UNCOMMITTED_OCCURRENCE_STATUSES),
                    RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
                    RecurringNeedOccurrence.requested_quantity == need.quantity,  # hors exceptions explicites
                    RecurringNeedOccurrence.unit == need.unit,
                )
            )
        ).all()
        cancelled = 0
        if candidates:
            due = set(generate_occurrence_dates(rule, from_date=_today(), to_date=max(d for _, d in candidates).date()))
            stale = [oid for oid, d in candidates if d.date() not in due]
            if stale:
                cancelled = len(
                    await self._reset_uncommitted_occurrences(
                        need.id, RecurringNeedOccurrence.id.in_(stale), status="CANCELLED"
                    )
                )
        created = 0
        if need.status == "ACTIVE":
            created = await self._ensure_window_for_need(need, actor_type="BUYER")
        self._audit_mutation(need, "PERMANENT_FREQUENCY", old_key, new_key)
        logger.info(
            "recurring_need.frequency_reconciled | recurring_need_id=%s | cancelled=%s | created=%s", need.id, cancelled, created
        )
        return self._mutation_result(need, "APPLIED", occurrences_cancelled=cancelled, occurrences_created=created)

    @staticmethod
    def _schedule_key(need: RecurringNeed) -> str:
        days = ",".join(str(d) for d in sorted(need.weekly_days or []))
        excl = ",".join(str(d) for d in sorted(need.excluded_weekdays or []))
        return f"{need.recurrence_type}[days={days};excl={excl}]"

    async def _apply_pause(self, need: RecurringNeed, *, paused_until: Optional[date]) -> Dict[str, Any]:
        """`paused_until` = premier jour de REPRISE (exclu de la pause) : les occurrences futures non engagées strictement
        avant ce jour (toutes, sans date) passent `SKIPPED`. Une occurrence ACCEPTED est un engagement : préservée. La
        génération future est arrêtée (le besoin n'est plus `ACTIVE`) ; la reprise automatique à `paused_until` est portée
        par `replenish_occurrence_windows` (même cron, aucun second planificateur)."""
        if paused_until is not None and paused_until <= _today():
            raise BusinessRuleException(
                "La date de reprise doit être postérieure à aujourd'hui.", reason="invalid_pause_date"
            )
        until_dt = datetime.combine(paused_until, datetime.min.time()) if paused_until else None
        if need.status == "PAUSED" and need.paused_until == until_dt:
            return self._mutation_result(need, "ALREADY_APPLIED", occurrences_skipped=0)
        old = f"{need.status}(until={need.paused_until.date().isoformat() if need.paused_until else None})"
        need.status = "PAUSED"
        need.paused_until = until_dt
        await self._bump_need_version(need)
        where = (RecurringNeedOccurrence.occurrence_date < until_dt,) if until_dt else ()
        ids = await self._reset_uncommitted_occurrences(need.id, *where, status="SKIPPED")
        self._audit_mutation(need, "PAUSE", old, f"PAUSED(until={paused_until.isoformat() if paused_until else None})")
        logger.info("recurring_need.paused | recurring_need_id=%s | occurrences_skipped=%s", need.id, len(ids))
        return self._mutation_result(need, "APPLIED", occurrences_skipped=len(ids))

    async def _apply_resume(self, need: RecurringNeed) -> Dict[str, Any]:
        """Reprise : `ACTIVE`, planning régénéré À PARTIR D'AUJOURD'HUI (jamais de rattrapage de la période de pause) ; les
        dates `SKIPPED` par la pause restent des faits historiques."""
        if need.status == "ACTIVE":
            return self._mutation_result(need, "ALREADY_APPLIED")
        need.status = "ACTIVE"
        need.paused_until = None
        await self._bump_need_version(need)
        created = await self._ensure_window_for_need(need, actor_type="BUYER")
        self._audit_mutation(need, "RESUME", "PAUSED", "ACTIVE")
        logger.info("recurring_need.resumed | recurring_need_id=%s | occurrences_created=%s", need.id, created)
        return self._mutation_result(need, "APPLIED", occurrences_created=created)

    async def _apply_cancel(self, need: RecurringNeed) -> Dict[str, Any]:
        """Fin durable du contrat : plus de génération, plus de matching ; les occurrences futures non engagées (OPEN et
        MATCHED) passent `CANCELLED`, leurs propositions expirent. Les engagements ACCEPTED et leurs commandes subsistent."""
        if need.status == "CANCELLED":
            return self._mutation_result(need, "ALREADY_APPLIED", occurrences_cancelled=0)
        old = need.status
        need.status = "CANCELLED"
        await self._bump_need_version(need)
        ids = await self._reset_uncommitted_occurrences(need.id, status="CANCELLED")
        self._audit_mutation(need, "CANCEL", old, "CANCELLED")
        logger.info("recurring_need.cancelled | recurring_need_id=%s | occurrences_cancelled=%s", need.id, len(ids))
        return self._mutation_result(need, "APPLIED", occurrences_cancelled=len(ids))

    async def _apply_occurrence_override(
        self, need: RecurringNeed, *, occurrence_date: Optional[date], quantity: Optional[float],
        expected_occurrence_version: int,
    ) -> Dict[str, Any]:
        if occurrence_date is None or quantity is None:
            raise BusinessRuleException("Date et quantité requises.")
        if not 0 < float(quantity) < float("inf"):
            raise BusinessRuleException("Quantité invalide : un nombre supérieur à 0 est requis.", reason="invalid_quantity")
        occ = await self._get_mutable_occurrence(need, occurrence_date)
        if int(occ.version) != expected_occurrence_version:  # occurrence verrouillée : le refus précède tout effet de bord
            return self._occurrence_conflict(need, occ, "OCCURRENCE_OVERRIDE", expected_occurrence_version)
        self._log_version_check("OCCURRENCE_OVERRIDE", "OCCURRENCE", match=True)
        if float(occ.requested_quantity) == float(quantity):
            return self._mutation_result(need, "ALREADY_APPLIED", occurrence_id=str(occ.id), requested_quantity=float(quantity))
        old = float(occ.requested_quantity)
        # La proposition construite sur l'ancienne quantité n'a plus de sens : même primitive que la mise à jour permanente.
        await self._reset_uncommitted_occurrences(need.id, RecurringNeedOccurrence.id == occ.id, requested_quantity=float(quantity), from_today=False)
        logger.info(
            "occurrence.mutation | occurrence_id=%s | recurring_need_id=%s | action=OCCURRENCE_OVERRIDE | old=%g | new=%g",
            occ.id, need.id, old, float(quantity),
        )
        return self._mutation_result(need, "APPLIED", occurrence_id=str(occ.id), requested_quantity=float(quantity))

    async def _apply_occurrence_skip(
        self,
        need: RecurringNeed,
        *,
        occurrence_date: Optional[date],
        actor_id: Any = None,
        zone_id: Any = None,
        expected_occurrence_version: int,
    ) -> Dict[str, Any]:
        if occurrence_date is None:
            raise BusinessRuleException("Date requise.")
        occ = await self._get_mutable_occurrence(need, occurrence_date, allow_skipped=True)
        if int(occ.version) != expected_occurrence_version:  # un rejeu après succès est aussi périmé (version + 1) : conflit
            return self._occurrence_conflict(need, occ, "OCCURRENCE_SKIP", expected_occurrence_version)
        self._log_version_check("OCCURRENCE_SKIP", "OCCURRENCE", match=True)
        if occ.status == "SKIPPED":
            return self._mutation_result(need, "ALREADY_APPLIED", occurrence_id=str(occ.id))
        quantity, unit = float(occ.requested_quantity), occ.unit
        await self._reset_uncommitted_occurrences(need.id, RecurringNeedOccurrence.id == occ.id, status="SKIPPED", from_today=False)
        # Analytics Phase C : le fait métier = l'occurrence réellement passée à SKIPPED ; un rejeu est `ALREADY_APPLIED`
        # (aucun 2e événement), et la clé est de toute façon unique par occurrence.
        await BusinessEventEmitter(self._db()).emit(
            event_name=BusinessEventName.RECURRING_OCCURRENCE_SKIPPED,
            journey=Journey.RECURRING,
            actor_type="BUYER",
            actor_id=actor_id,
            buyer_id=need.buyer_id,
            zone_id=zone_id,
            entity_type="RECURRING_OCCURRENCE",
            entity_id=occ.id,
            idempotency_key=f"RECURRING_OCCURRENCE_SKIPPED:{occ.id}",
            sub_category_id=need.sub_category_id,
            quantity=quantity,
            unit=unit,
            metadata={"recurring_need_id": str(need.id)},
        )
        logger.info("occurrence.skipped | occurrence_id=%s", occ.id)
        return self._mutation_result(need, "APPLIED", occurrence_id=str(occ.id))

    async def _get_mutable_occurrence(
        self, need: RecurringNeed, occurrence_date: date, *, allow_skipped: bool = False
    ) -> RecurringNeedOccurrence:
        current_session = self.session
        occ = await current_session.scalar(
            select(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.occurrence_date == datetime.combine(occurrence_date, datetime.min.time()),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if occ is None:
            raise BusinessRuleException("Aucune occurrence à cette date.")
        if allow_skipped and occ.status == "SKIPPED":
            return occ
        if occ.status not in _UNCOMMITTED_OCCURRENCE_STATUSES:
            raise BusinessRuleException(
                f"Cette date n'est plus modifiable (statut actuel : {occ.status}).", reason="occurrence_not_open"
            )
        return occ

    # ── pauses temporelles échues ─────────────────────────────────────

    async def _resume_elapsed_pauses(self, *, need_id: Any = None) -> int:
        """Reprise automatique : un besoin `PAUSED` dont `paused_until` est atteint repasse `ACTIVE` (sans rattrapage : la
        matérialisation part d'aujourd'hui). Appelée par le cron de réapprovisionnement ET par le self-service, sous le
        verrou du besoin — idempotent, jamais de second planificateur."""
        stmt = select(RecurringNeed).where(
            RecurringNeed.status == "PAUSED",
            RecurringNeed.paused_until.is_not(None),
            RecurringNeed.paused_until <= datetime.combine(_today(), datetime.min.time()),
        )
        if need_id is not None:
            stmt = stmt.where(RecurringNeed.id == need_id)
        needs = (
            await self._db().execute(stmt.with_for_update(skip_locked=True).execution_options(populate_existing=True))
        ).scalars().all()
        for need in needs:
            need.status = "ACTIVE"
            need.paused_until = None
            await self._bump_need_version(need)
            self._audit_mutation(need, "RESUME", "PAUSED(elapsed)", "ACTIVE", actor="SYSTEM")
        return len(needs)

    async def _lock_active_need(self, need: RecurringNeed) -> Optional[RecurringNeed]:
        """Reverrouille le besoin et renvoie son état COMMITÉ le plus récent s'il est encore `ACTIVE`, sinon `None` : la
        matérialisation ne décide jamais sur un objet lu avant une annulation/pause/modification concurrente."""
        fresh = await self._db().scalar(
            select(RecurringNeed).where(RecurringNeed.id == need.id).with_for_update().execution_options(populate_existing=True)
        )
        return fresh if fresh is not None and fresh.status == "ACTIVE" else None


# ── B28 — RÉCUPÉRATION D'UNE OCCURRENCE (contrat : docs/RECURRING_RECOVERY_CONTRACT.md) ─────────────────────────────
# Besoin = demande durable ; occurrence = UNE obligation de livraison ; allocation / commande = UNE tentative de la remplir.
# L'échec d'une tentative (producteur sans réponse, refus, annulation) n'est donc pas l'échec de l'occurrence : tant que la
# fenêtre le permet, l'occurrence redevient cherchable (OPEN) et la MÊME occurrence est re-matchée — jamais la suivante.
RECOVERY_REASONS = ("PRODUCER_TIMEOUT", "PRODUCER_REJECTED", "PRODUCER_CANCELLED", "PROPOSAL_REJECTED")
#: Note d'historique de commande (`OrderStatusHistory.note`, déjà écrite par les services d'échec) -> raison structurée.
_FAILURE_NOTE_TO_REASON = {
    "producer_confirmation_expired": "PRODUCER_TIMEOUT",
    "declined_by_producer": "PRODUCER_REJECTED",
    "cancelled_by_producer": "PRODUCER_CANCELLED",
}
#: Occurrence terminale : jamais récupérable (livrée, sautée, annulée) — la fenêtre n'a pas à être consultée.
_TERMINAL_NOT_RECOVERABLE = ("SKIPPED", "CANCELLED", "FULFILLED", "PARTIALLY_FULFILLED")
_ATTEMPT_FAILED_BY = ("PRODUCER", "SYSTEM")


@dataclass(frozen=True)
class RecoveryDecision:
    """Verdict de `can_recover_occurrence`. `outcome` ∈ RECOVERABLE / NEED_NOT_ACTIVE / NOT_RECOVERABLE /
    RECOVERY_WINDOW_CLOSED / OCCURRENCE_COMMITTED ; `in_place` = l'occurrence est déjà cherchable (OPEN/MATCHED)."""

    recoverable: bool
    outcome: str
    in_place: bool = False


def recovery_window_open(occurrence_date: Any, today: date) -> bool:
    """Fenêtre de récupération = jusqu'au JOUR de livraison INCLUS (même règle que `_occurrence_is_past` / l'expiration
    d'une proposition : aucune règle horaire nouvelle). Dates sans fuseau, date serveur (limite documentée)."""
    day = occurrence_date.date() if isinstance(occurrence_date, datetime) else occurrence_date
    return day >= today


def can_recover_occurrence(
    occurrence: Any,
    *,
    need_status: str,
    today: date,
    live_orders: int = 0,
    sourcing_failed: bool = False,
    user_initiated: bool = True,
) -> RecoveryDecision:
    """PRIMITIVE UNIQUE de décision (pure, sans I/O) : cette occurrence peut-elle encore être récupérée ? Appelée par
    le self-service, le cron et les services d'échec — jamais décidée par un flux conversationnel.

    - besoin non ACTIF (pause / annulation) -> `NEED_NOT_ACTIVE` ;
    - SKIPPED / CANCELLED / FULFILLED / PARTIALLY_FULFILLED -> `NOT_RECOVERABLE` (terminal) ;
    - EXPIRED (date passée) -> `RECOVERY_WINDOW_CLOSED` ;
    - ACCEPTED / PARTIALLY_ACCEPTED avec une commande vivante -> `OCCURRENCE_COMMITTED` (engagement irréversible) ;
    - ACCEPTED sans commande vivante, ou UNFULFILLED dont TOUTES les tentatives ont échoué côté producteur/système et
      rien n'a été livré -> récupérable si la fenêtre est ouverte ;
    - REJECTED (l'acheteur a refusé la proposition) -> récupérable seulement sur demande explicite de l'acheteur ;
    - OPEN / MATCHED -> déjà cherchable (`in_place`), récupérable si la fenêtre est ouverte."""
    status = str(getattr(occurrence, "status", "") or "")
    if str(need_status or "") != "ACTIVE":
        return RecoveryDecision(False, "NEED_NOT_ACTIVE")
    if status in _TERMINAL_NOT_RECOVERABLE:
        return RecoveryDecision(False, "NOT_RECOVERABLE")
    if status == "EXPIRED":
        return RecoveryDecision(False, "RECOVERY_WINDOW_CLOSED")
    if status in ("ACCEPTED", "PARTIALLY_ACCEPTED") and live_orders > 0:
        return RecoveryDecision(False, "OCCURRENCE_COMMITTED")
    if status == "UNFULFILLED" and not sourcing_failed:
        return RecoveryDecision(False, "NOT_RECOVERABLE")
    if status == "REJECTED" and not user_initiated:
        return RecoveryDecision(False, "NOT_RECOVERABLE")
    if status not in ("OPEN", "MATCHED", "PROPOSED", "ACCEPTED", "PARTIALLY_ACCEPTED", "UNFULFILLED", "REJECTED"):
        return RecoveryDecision(False, "NOT_RECOVERABLE")
    if not recovery_window_open(occurrence.occurrence_date, today):
        return RecoveryDecision(False, "RECOVERY_WINDOW_CLOSED")
    return RecoveryDecision(True, "RECOVERABLE", in_place=status in ("OPEN", "MATCHED", "PROPOSED"))


def _log_recovery(event: str, occurrence: Any, **fields: Any) -> None:
    """Observabilité B28 (sans PII : identifiants d'occurrence et statuts seulement)."""
    logger.info(
        "RECURRING_RECOVERY_%s | occurrence_id=%s | recurring_need_id=%s | occurrence_status=%s | %s",
        event, getattr(occurrence, "id", None), getattr(occurrence, "recurring_need_id", None),
        getattr(occurrence, "status", None), " | ".join(f"{k}={v}" for k, v in fields.items()),
    )


def _order_fulfillment_state(order_status: Any, delivery_status: Any) -> str:
    """Classe une commande RECURRING_SUPPLY pour le fulfillment : DELIVERED (reçue, comptée),
    ISSUE (réception avec problème, terminale, non comptée), CANCELLED, sinon ACTIVE (en cours)."""
    if str(order_status or "").upper() == "CANCELLED":
        return "CANCELLED"
    d = str(delivery_status or "").upper()
    if d == "RECEIVED":
        return "DELIVERED"
    if d == "RECEIVED_WITH_ISSUE":
        return "ISSUE"
    return "ACTIVE"


def _occurrence_is_past(occurrence: Any) -> bool:
    """Une occurrence dont la date est STRICTEMENT passée n'est plus livrable : sa proposition a
    expiré (un « oui » tardif ne doit jamais produire de commande pour hier)."""
    occ_date = getattr(occurrence, "occurrence_date", None)
    if occ_date is None:
        return False
    occ_day = occ_date.date() if isinstance(occ_date, datetime) else occ_date
    return occ_day < _today()


def _parse_date(value: Optional[Any]) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise BusinessRuleException(f"Date invalide : {value!r}")


__all__ = [
    "RecurringSupplyMixin",
    "RECURRING_NEED_ACTIONS",
    "RECURRING_OCCURRENCE_ACTIONS",
    "MATCH_RESPONSE_ACTIONS",
    "recurring_need_version",
    "recurring_need_version_of",
    "RecoveryDecision",
    "RECOVERY_REASONS",
    "can_recover_occurrence",
    "recovery_window_open",
]
