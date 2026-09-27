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

## Sémantique des modifications (mandat §9/§10)

`update_recurring_need` reçoit une `action` structurée (voir `RECURRING_NEED_ACTIONS`) :

- `PERMANENT_QUANTITY` / `PERMANENT_FREQUENCY` : modifie `recurring_needs`, puis met à jour UNIQUEMENT
  les occurrences FUTURES encore `OPEN` (jamais `SKIPPED`/`MATCHED`/`PROPOSED`/`ACCEPTED`/
  `PARTIALLY_ACCEPTED`/`FULFILLED`/`PARTIALLY_FULFILLED`/`EXPIRED`/`CANCELLED` — une occurrence qui a
  quitté `OPEN` a déjà une vie propre, voir `_MUTABLE_OCCURRENCE_STATUSES`). Une exception ponctuelle
  déjà posée sur une occurrence encore `OPEN` (`requested_quantity` ≠ la quantité permanente d'origine)
  n'est PAS écrasée : voir `_apply_permanent_update` — c'est le point du mandat §10 vérifié par
  `test_a_permanent_update_never_silently_overwrites_an_explicit_exception`.
- `PAUSE` : `recurring_needs.status = PAUSED`, `paused_until` posé. Les occurrences déjà matérialisées
  et encore `OPEN` dans la fenêtre de pause passent à `SKIPPED` (mandat §9 : « suspend cette semaine »
  ne doit pas laisser une occurrence `OPEN` que Phase 3 matcherait quand même).
- `RESUME` : `recurring_needs.status = ACTIVE`, `paused_until = NULL`. Les occurrences qu'on venait de
  passer `SKIPPED` par CETTE pause ne repassent PAS `OPEN` automatiquement (un skip reste un fait
  historique) — seules les occurrences futures nouvellement générées seront `OPEN`.
- `CANCEL` : `recurring_needs.status = CANCELLED`. Les occurrences `OPEN` futures passent `CANCELLED`.
- `OCCURRENCE_OVERRIDE` : modifie `requested_quantity` d'UNE occurrence précise, `version += 1`. Ne
  touche jamais `recurring_needs`.
- `OCCURRENCE_SKIP` : `status = SKIPPED` sur UNE occurrence précise, `version += 1`.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text, update

from ladini.domain.analytics.business_events import BusinessEventName
from ladini.domain.analytics.emitter import BusinessEventEmitter
from ladini.domain.analytics.metric_dictionary import Journey
from ladini.domain.analytics.units import resolve_subcategory_canonical_unit
from ladini.domain.models import (
    BuyerProfile,
    NeedAllocation,
    Order,
    OrderItem,
    OrderStatusHistory,
    Product,
    RecurringNeed,
    RecurringNeedOccurrence,
    SubCategory,
    User,
)
from ladini.domain.recurring_supply.recurrence import (
    OCCURRENCE_WINDOW_DAYS,
    RecurrenceRule,
    generate_occurrence_dates,
)

from .base import BaseMixin
from .common import normalize_phone
from .errors import BusinessRuleException

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
                logger.info("recurring_need.confirmation_replayed | draft_id=%s | version=%s", draft_id, draft_version)
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
            "recurring_need.created | recurring_need_id=%s | buyer_id=%s | occurrences=%s",
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
            "recurring_need.created_batch | buyer_id=%s | count=%s | recurring_need_ids=%s",
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
        stmt = stmt.on_conflict_do_nothing(index_elements=["recurring_need_id", "occurrence_date"])
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

        window_end = _today() + timedelta(days=OCCURRENCE_WINDOW_DAYS)
        needs = (
            await current_session.execute(select(RecurringNeed).where(RecurringNeed.status == "ACTIVE"))
        ).scalars().all()
        # Une seule requête pour la zone acheteur + l'unité canonique de la sous-catégorie de TOUS
        # les besoins actifs (analytics Phase C) — jamais un lookup par besoin.
        dims = {
            row.need_id: (row.zone_id, row.priority_unit)
            for row in (
                await current_session.execute(
                    select(
                        RecurringNeed.id.label("need_id"),
                        User.zone_id.label("zone_id"),
                        SubCategory.priority_unit.label("priority_unit"),
                    )
                    .join(BuyerProfile, BuyerProfile.id == RecurringNeed.buyer_id)
                    .join(User, User.id == BuyerProfile.user_id)
                    .join(SubCategory, SubCategory.id == RecurringNeed.sub_category_id)
                    .where(RecurringNeed.status == "ACTIVE")
                )
            ).all()
        }

        needs_examined = 0
        occurrences_created = 0
        for need in needs:
            rule = RecurrenceRule(
                recurrence_type=need.recurrence_type,
                weekly_days=need.weekly_days or (),
                excluded_weekdays=need.excluded_weekdays or (),
                starts_at=need.starts_at,
                ends_at=need.ends_at,
            )
            needs_examined += 1
            zone_id, priority_unit = dims.get(need.id, (None, None))
            occurrences_created += await self._materialize_occurrences(
                need,
                rule,
                from_date=_today(),
                to_date=window_end,
                zone_id=zone_id,
                canonical_unit=resolve_subcategory_canonical_unit(SimpleNamespace(priority_unit=priority_unit)),
            )

        logger.info(
            "recurring_need.occurrence_window_replenished | needs_examined=%s | occurrences_created=%s",
            needs_examined, occurrences_created,
        )
        return {"needs_examined": needs_examined, "occurrences_created": occurrences_created}

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

        needs_stmt = (
            select(RecurringNeed, SubCategory.name)
            .join(SubCategory, RecurringNeed.sub_category_id == SubCategory.id)
            .where(RecurringNeed.buyer_id == buyer_id, RecurringNeed.status != "CANCELLED")
            .order_by(RecurringNeed.created_at.asc())
        )
        rows = (await current_session.execute(needs_stmt)).all()
        needs = [n for n, _ in rows]
        need_ids = [n.id for n in needs]

        next_occurrence_by_need: Dict[Any, RecurringNeedOccurrence] = {}
        if need_ids:
            occ_stmt = (
                select(RecurringNeedOccurrence)
                .where(
                    RecurringNeedOccurrence.recurring_need_id.in_(need_ids),
                    RecurringNeedOccurrence.status.in_(("OPEN", "MATCHED")),
                )
                .order_by(RecurringNeedOccurrence.recurring_need_id, RecurringNeedOccurrence.occurrence_date.asc())
            )
            for occ in (await current_session.execute(occ_stmt)).scalars().all():
                next_occurrence_by_need.setdefault(occ.recurring_need_id, occ)

        items = []
        for need, sub_category_name in rows:
            next_occ = next_occurrence_by_need.get(need.id)
            items.append(
                {
                    "recurring_need_id": str(need.id),
                    "product": sub_category_name,
                    "quantity": float(need.quantity),
                    "unit": need.unit,
                    "recurrence_type": need.recurrence_type,
                    "weekly_days": need.weekly_days,
                    "status": need.status,
                    "next_occurrence_date": (
                        next_occ.occurrence_date.date().isoformat() if next_occ else None
                    ),
                    "next_occurrence_id": str(next_occ.id) if next_occ else None,
                    "requested_quantity": float(next_occ.requested_quantity) if next_occ else None,
                    "matched_quantity": float(next_occ.quantity_matched) if next_occ else None,
                }
            )
        return {"status": "success", "items": items}

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

        occurrence = await current_session.scalar(
            select(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.status.in_(("OPEN", "MATCHED")),
            )
            .order_by(RecurringNeedOccurrence.occurrence_date.asc())
            .limit(1)
        )
        if occurrence is None:
            return {
                "status": "success",
                "product": sub_category_name,
                "requested_quantity": float(need.quantity),
                "unit": need.unit,
                "occurrence_date": None,
                "allocations": [],
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

        return {
            "status": "success",
            "product": sub_category_name,
            "requested_quantity": float(occurrence.requested_quantity),
            "unit": occurrence.unit,
            "occurrence_date": occurrence.occurrence_date.date().isoformat(),
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

    # ─── CONFIRMATION (VS4 pilote) ────────────────────────────────────

    async def accept_match_proposal(self, phone: str, recurring_need_id: str, action: str) -> Dict[str, Any]:
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
        allocations ou de créer deux jeux de commandes."""
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
            raise BusinessRuleException("Besoin introuvable.")

        occurrence = await current_session.scalar(
            select(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.status.in_(("OPEN", "MATCHED")),
                RecurringNeedOccurrence.quantity_matched > 0,
            )
            .order_by(RecurringNeedOccurrence.occurrence_date.asc())
            .limit(1)
            .with_for_update()
        )
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
            raise BusinessRuleException("Aucune proposition en attente pour ce besoin.")

        if action == "REJECT":
            for alloc in allocations:
                alloc.status = "REJECTED"
            occurrence.status = "REJECTED"
            occurrence.version = occurrence.version + 1
            await current_session.flush()
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
                product = await current_session.get(Product, alloc.product_id)
                if product is None:
                    raise BusinessRuleException("Produit introuvable pour une allocation.")
                available = float(product.quantity_for_sale or 0.0)
                debit = float(alloc.quantity)
                if available < debit:
                    raise BusinessRuleException(
                        f"Stock insuffisant pour {product.name} (disponible : {available})."
                    )
                product.quantity_for_sale = available - debit

                item = OrderItem(
                    id=uuid.uuid4(),
                    order_id=order.id,
                    product_id=product.id,
                    quantity=debit,
                    price_at_sale=float(alloc.unit_price),
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
        logger.info(
            "recurring_supply.reception_recorded | order_id=%s | outcome=%s", order.id, outcome
        )
        return {
            "status": "success",
            "outcome": "RECORDED",
            "order_id": str(order.id),
            "delivery_status": outcome,
        }

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
    ) -> Dict[str, Any]:
        if action not in RECURRING_NEED_ACTIONS:
            raise BusinessRuleException(f"Action inconnue : {action!r}")

        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")
        _user_obj, buyer_profile = await self.get_buyer_profile(phone=str(phone))
        buyer_id = buyer_profile.id

        need = await current_session.scalar(
            select(RecurringNeed).where(RecurringNeed.id == recurring_need_id, RecurringNeed.buyer_id == buyer_id)
        )
        if need is None:
            # Jamais de fuite d'existence (mandat §12) : même message qu'un id inexistant.
            raise BusinessRuleException("Besoin introuvable.")

        if action == "PERMANENT_QUANTITY":
            return await self._apply_permanent_update(need, quantity=quantity)
        if action == "PERMANENT_FREQUENCY":
            return await self._apply_permanent_update(
                need, recurrence_type=recurrence_type, weekly_days=weekly_days, excluded_weekdays=excluded_weekdays
            )
        if action == "PAUSE":
            return await self._apply_pause(need, paused_until=_parse_date(paused_until))
        if action == "RESUME":
            return await self._apply_resume(need)
        if action == "CANCEL":
            return await self._apply_cancel(need)
        if action == "OCCURRENCE_OVERRIDE":
            return await self._apply_occurrence_override(need, occurrence_date=_parse_date(occurrence_date), quantity=quantity)
        if action == "OCCURRENCE_SKIP":
            return await self._apply_occurrence_skip(
                need,
                occurrence_date=_parse_date(occurrence_date),
                actor_id=_user_obj.id,
                zone_id=getattr(_user_obj, "zone_id", None),
            )
        raise AssertionError(action)  # pragma: no cover — filtré par RECURRING_NEED_ACTIONS ci-dessus

    async def _apply_permanent_update(
        self,
        need: RecurringNeed,
        *,
        quantity: Optional[float] = None,
        recurrence_type: Optional[str] = None,
        weekly_days: Optional[List[int]] = None,
        excluded_weekdays: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Modifie `recurring_needs` PUIS les occurrences FUTURES encore `OPEN` — jamais une
        occurrence qui a quitté cet état (mandat §9), et jamais une exception ponctuelle déjà posée
        sur une occurrence encore `OPEN` (mandat §10) : une occurrence dont `requested_quantity` a
        déjà été explicitement modifiée (≠ l'ancienne quantité permanente) reste intacte — une mise à
        jour générale ne doit jamais écraser silencieusement une exception explicite."""
        current_session = self.session
        old_quantity = need.quantity
        old_unit = need.unit

        if quantity is not None:
            need.quantity = float(quantity)
        if recurrence_type is not None:
            need.recurrence_type = recurrence_type
        if weekly_days is not None:
            need.weekly_days = list(weekly_days)
        if excluded_weekdays is not None:
            need.excluded_weekdays = list(excluded_weekdays)
        await current_session.flush()

        updated_count = 0
        if quantity is not None:
            # Ne touche QUE les occurrences encore OPEN dont la quantité est EXACTEMENT l'ancienne
            # quantité permanente — une occurrence déjà en exception (quantité différente posée
            # explicitement par `OCCURRENCE_OVERRIDE`) n'est jamais écrasée silencieusement.
            stmt = (
                update(RecurringNeedOccurrence)
                .where(
                    RecurringNeedOccurrence.recurring_need_id == need.id,
                    RecurringNeedOccurrence.status.in_(_MUTABLE_OCCURRENCE_STATUSES),
                    RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
                    RecurringNeedOccurrence.requested_quantity == old_quantity,
                    RecurringNeedOccurrence.unit == old_unit,
                )
                .values(requested_quantity=need.quantity, unit=need.unit, version=RecurringNeedOccurrence.version + 1)
            )
            result = await current_session.execute(stmt)
            updated_count = result.rowcount or 0

        logger.info("recurring_need.updated | recurring_need_id=%s | occurrences_updated=%s", need.id, updated_count)
        return {"status": "success", "recurring_need_id": str(need.id), "occurrences_updated": updated_count}

    async def _apply_pause(self, need: RecurringNeed, *, paused_until: Optional[date]) -> Dict[str, Any]:
        current_session = self.session
        need.status = "PAUSED"
        need.paused_until = datetime.combine(paused_until, datetime.min.time()) if paused_until else None
        await current_session.flush()

        stmt = (
            update(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.status.in_(_MUTABLE_OCCURRENCE_STATUSES),
                RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
            )
        )
        if paused_until:
            stmt = stmt.where(RecurringNeedOccurrence.occurrence_date <= datetime.combine(paused_until, datetime.min.time()))
        stmt = stmt.values(status="SKIPPED", version=RecurringNeedOccurrence.version + 1)
        result = await current_session.execute(stmt)

        logger.info("recurring_need.paused | recurring_need_id=%s | occurrences_skipped=%s", need.id, result.rowcount)
        return {"status": "success", "recurring_need_id": str(need.id), "occurrences_skipped": result.rowcount or 0}

    async def _apply_resume(self, need: RecurringNeed) -> Dict[str, Any]:
        current_session = self.session
        need.status = "ACTIVE"
        need.paused_until = None
        await current_session.flush()
        logger.info("recurring_need.resumed | recurring_need_id=%s", need.id)
        return {"status": "success", "recurring_need_id": str(need.id)}

    async def _apply_cancel(self, need: RecurringNeed) -> Dict[str, Any]:
        current_session = self.session
        need.status = "CANCELLED"
        await current_session.flush()

        stmt = (
            update(RecurringNeedOccurrence)
            .where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.status.in_(_MUTABLE_OCCURRENCE_STATUSES),
                RecurringNeedOccurrence.occurrence_date >= datetime.combine(_today(), datetime.min.time()),
            )
            .values(status="CANCELLED", version=RecurringNeedOccurrence.version + 1)
        )
        result = await current_session.execute(stmt)
        logger.info("recurring_need.cancelled | recurring_need_id=%s | occurrences_cancelled=%s", need.id, result.rowcount)
        return {"status": "success", "recurring_need_id": str(need.id), "occurrences_cancelled": result.rowcount or 0}

    async def _apply_occurrence_override(
        self, need: RecurringNeed, *, occurrence_date: Optional[date], quantity: Optional[float]
    ) -> Dict[str, Any]:
        if occurrence_date is None or quantity is None:
            raise BusinessRuleException("Date et quantité requises.")
        occ = await self._get_mutable_occurrence(need, occurrence_date)
        current_session = self.session
        occ.requested_quantity = float(quantity)
        occ.version = occ.version + 1
        await current_session.flush()
        logger.info("occurrence.updated | occurrence_id=%s | requested_quantity=%s", occ.id, quantity)
        return {"status": "success", "occurrence_id": str(occ.id), "requested_quantity": float(quantity)}

    async def _apply_occurrence_skip(
        self,
        need: RecurringNeed,
        *,
        occurrence_date: Optional[date],
        actor_id: Any = None,
        zone_id: Any = None,
    ) -> Dict[str, Any]:
        if occurrence_date is None:
            raise BusinessRuleException("Date requise.")
        occ = await self._get_mutable_occurrence(need, occurrence_date)
        current_session = self.session
        occ.status = "SKIPPED"
        occ.version = occ.version + 1
        await current_session.flush()
        # Analytics Phase C : fonction de skip commune à toutes les formulations utilisateur ; le
        # fait métier = l'occurrence réellement passée à SKIPPED (une 2e tentative échoue dans
        # `_get_mutable_occurrence`, et la clé est de toute façon unique par occurrence).
        await BusinessEventEmitter(current_session).emit(
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
            quantity=float(occ.requested_quantity),
            unit=occ.unit,
            metadata={"recurring_need_id": str(need.id)},
        )
        logger.info("occurrence.skipped | occurrence_id=%s", occ.id)
        return {"status": "success", "occurrence_id": str(occ.id)}

    async def _get_mutable_occurrence(self, need: RecurringNeed, occurrence_date: date) -> RecurringNeedOccurrence:
        current_session = self.session
        occ = await current_session.scalar(
            select(RecurringNeedOccurrence).where(
                RecurringNeedOccurrence.recurring_need_id == need.id,
                RecurringNeedOccurrence.occurrence_date == datetime.combine(occurrence_date, datetime.min.time()),
            )
        )
        if occ is None:
            raise BusinessRuleException("Aucune occurrence à cette date.")
        if occ.status not in _MUTABLE_OCCURRENCE_STATUSES:
            raise BusinessRuleException(
                f"Cette date n'est plus modifiable (statut actuel : {occ.status})."
            )
        return occ


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


__all__ = ["RecurringSupplyMixin", "RECURRING_NEED_ACTIONS", "MATCH_RESPONSE_ACTIONS"]
