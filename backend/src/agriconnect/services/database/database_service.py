"""AgriDatabaseService facade with optional session injection.

The facade delegates every operation to specialised mixins.  Each public
method accepts an **optional** ``session: AsyncSession`` parameter:

* When *omitted* (or ``None``), the service creates a fresh session via
  ``get_sessionmaker()`` and manages its lifecycle automatically.
* When *provided* (e.g. by a FastAPI dependency or an MCP tool that
  already owns a transaction), the caller retains control over commit /
  rollback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Dict
import json
import uuid

from sqlalchemy import select

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.core.database import get_sessionmaker
from agriconnect.services.database.common import PERFORMANCE_INDEX_DDL, clamp_limit
from agriconnect.services.database.auth import AuthMixin
from agriconnect.services.database.utils import UtilsMixin
from agriconnect.services.database.marketplace import MarketplaceMixin
from agriconnect.services.database.auction import AuctionMixin
from agriconnect.services.database.transactions import TransactionsMixin
from agriconnect.services.database.intelligence import IntelligenceMixin
from agriconnect.services.database.dashboards import DashboardsMixin
from agriconnect.services.database.crop import CropMixin

from agriconnect.domain.models import Farm, Stock, StockMovement


class AgriDatabaseService(
	AuthMixin,
	UtilsMixin,
	MarketplaceMixin,
	TransactionsMixin,
	IntelligenceMixin,
	DashboardsMixin,
	CropMixin,
	AuctionMixin,
):
	"""Facade centralisant le cycle de vie des sessions et la delegation aux mixins.

	Supporte l'injection de session pour permettre aux agents MCP et a
	FastAPI de partager des transactions.
	"""

	# ------------------------------------------------------------------
	# Exceptions
	# ------------------------------------------------------------------
	class DatabaseServiceError(Exception):
		"""Wrapper to avoid leaking SQLAlchemy internals."""

	class IntegrityError(DatabaseServiceError):
		pass

	# ------------------------------------------------------------------
	# Logger
	# ------------------------------------------------------------------
	_logger = logging.getLogger(__name__)

	# ------------------------------------------------------------------
	# Session lifecycle helpers
	# ------------------------------------------------------------------

	async def _run_in_session(
		self,
		session: AsyncSession,
		func: Callable,
		*args: Any,
		commit: bool = True,
		**kwargs: Any,
	) -> Any:
		r"""Execute *func(session, \*args, \*\*kwargs)* with error handling.

		This method does **not** manage the session lifecycle — it assumes
		the caller has already opened one.  It commits or rolls back
		depending on *commit* and translates exceptions.
		"""
		func_name = getattr(func, "__name__", str(func))
		try:
			self._logger.debug("Executing %s  commit=%s", func_name, commit)
			result = await func(session, *args, **kwargs)
			if commit:
				await session.commit()
			return result
		except asyncio.CancelledError:
			try:
				await session.rollback()
			except Exception:
				self._logger.exception("Rollback failed after cancellation in %s", func_name)
			raise
		except SAIntegrityError as exc:
			try:
				await session.rollback()
			except Exception:
				self._logger.exception("Rollback failed after integrity error in %s", func_name)
			self._logger.exception("Integrity error in %s: %s", func_name, exc)
			raise self.IntegrityError(f"Integrity constraint failed: {func_name}") from exc
		except SQLAlchemyError as exc:
			try:
				await session.rollback()
			except Exception:
				self._logger.exception("Rollback failed after SQLAlchemy error in %s", func_name)
			self._logger.exception("Database error in %s: %s", func_name, exc)
			raise self.DatabaseServiceError(f"Database operation failed: {func_name}") from exc
		except Exception as exc:
			try:
				await session.rollback()
			except Exception:
				self._logger.exception("Rollback failed after unexpected error in %s", func_name)
			self._logger.exception("Unexpected error in %s: %s", func_name, exc)
			raise

	async def _execute_transaction(
		self,
		func: Callable,
		*args: Any,
		session: Optional[AsyncSession] = None,
		commit: bool = True,
		**kwargs: Any,
	) -> Any:
		"""Run *func* in a session — injected or auto-created.

		When *session* is ``None``, a new session is opened via
		``get_sessionmaker()`` and its lifecycle is fully managed here.
		When *session* is provided the caller keeps ownership; we only
		call *func* and let the caller decide when to commit.
		"""
		if session is not None:
			# Caller owns the session — skip commit/rollback here.
			return await self._run_in_session(session, func, *args, commit=False, **kwargs)

		sm = get_sessionmaker()
		if sm is None:
			self._logger.error("Database sessionmaker unavailable")
			raise self.DatabaseServiceError("Database sessionmaker unavailable")

		async with sm() as new_session:
			return await self._run_in_session(new_session, func, *args, commit=commit, **kwargs)

	# ------------------------------------------------------------------
	# Performance indexes
	# ------------------------------------------------------------------

	async def ensure_performance_indexes(self, session: AsyncSession = None) -> dict[str, Any]:
		async def _apply(sess: AsyncSession) -> dict[str, Any]:
			applied: list[str] = []
			failed: list[dict[str, str]] = []
			for ddl in PERFORMANCE_INDEX_DDL:
				try:
					async with sess.begin_nested():
						await sess.execute(text(ddl))
					applied.append(ddl)
				except Exception as exc:
					try:
						await sess.rollback()
					except Exception:
						self._logger.exception("Rollback after DDL failure: %s", ddl)
					self._logger.warning("Index DDL failed: %s (%s)", ddl, exc)
					failed.append({"ddl": ddl, "error": str(exc)})
			return {"status": "ok" if not failed else "partial", "applied_count": len(applied), "failed": failed}

		return await self._execute_transaction(_apply, session=session)

	# ==================================================================
	# Auth
	# ==================================================================
	async def get_user_by_phone(self, phone: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_user_by_phone, phone, session=session, commit=False)

	async def get_user_by_id(self, user_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_user_by_id, user_id, session=session, commit=False)

	async def identify_or_create_user(self, phone: str, name: str = None, zone_id: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().identify_or_create_user, phone, name, zone_id, session=session)

	# ==================================================================
	# Utils
	# ==================================================================
	async def normalize_unit(self, quantity: float, unit: str, session: AsyncSession = None):
		return await self._execute_transaction(super().normalize_unit, quantity, unit, session=session, commit=False)

	async def guess_category(self, product_name: str, session: AsyncSession = None):
		return await self._execute_transaction(super().guess_category, product_name, session=session, commit=False)

	async def check_price_anomaly(self, product_name: str, proposed_price: float, zone_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().check_price_anomaly, product_name, proposed_price, zone_id, session=session, commit=False)

	# ==================================================================
	# Marketplace — Farms / Stocks / Products / Orders / Clients / Expenses / Crops
	# ==================================================================

	async def get_producer_stocks(self, producer_id: str, session: AsyncSession = None) -> Dict[str, Any]:
		"""Retourne les stocks d'un producteur (UUID interne) pour permettre la résolution "par nom" côté WhatsApp.

		Note: Cette méthode est conçue pour les agents conversationnels. Elle n'expose aucun identifiant côté utilisateur,
		mais retourne des objets complets (incluant `id`) pour usage interne du graphe.
		"""
		async def _op(sess: AsyncSession) -> Dict[str, Any]:
			pid = uuid.UUID(str(producer_id))
			stmt = (
				select(Stock)
				.join(Farm, Stock.farm_id == Farm.id)
				.where(Farm.producer_id == pid)
				.order_by(Stock.item_name)
			)
			rows = (await sess.execute(stmt)).scalars().all()
			return {
				"status": "success",
				"count": len(rows),
				"data": [
					{
						"stock_id": str(s.id),
						"item_name": s.item_name,
						"quantity": s.quantity,
						"unit": s.unit,
						"type": s.type,
						"farm_id": str(s.farm_id) if s.farm_id else None,
					}
					for s in rows
				],
			}

		return await self._execute_transaction(_op, session=session, commit=False)

	async def adjust_stock_by_id(
		self,
		producer_id: str,
		stock_id: str,
		new_quantity: float,
		reason: str = "Ajustement via agent",
		session: AsyncSession = None,
	) -> Dict[str, Any]:
		"""Ajuste un stock par identifiant (interne) après contrôle de propriété."""
		async def _op(sess: AsyncSession) -> Dict[str, Any]:
			pid = uuid.UUID(str(producer_id))
			sid = uuid.UUID(str(stock_id))
			stmt = (
				select(Stock)
				.join(Farm, Stock.farm_id == Farm.id)
				.where(Stock.id == sid, Farm.producer_id == pid)
				.with_for_update()
			)
			stock = (await sess.execute(stmt)).scalars().first()
			if not stock:
				return {"status": "error", "message": "Stock introuvable ou accès refusé."}

			try:
				new_q = float(new_quantity)
			except Exception:
				return {"status": "error", "message": "Quantité invalide."}
			if new_q < 0:
				return {"status": "error", "message": "La quantité ne peut pas être négative."}

			old_q = float(stock.quantity or 0.0)
			delta = new_q - old_q
			stock.quantity = new_q

			if delta != 0:
				mvt_type = "IN" if delta > 0 else "OUT"
				sess.add(StockMovement(stock_id=stock.id, type=mvt_type, quantity=abs(delta), reason=reason))

			await sess.flush()
			return {
				"status": "success",
				"stock_id": str(stock.id),
				"item_name": stock.item_name,
				"old_quantity": old_q,
				"new_quantity": new_q,
				"unit": stock.unit,
			}

		return await self._execute_transaction(_op, session=session)

	async def remove_stock_by_id(
		self,
		producer_id: str,
		stock_id: str,
		quantity: float,
		reason: str = "Retrait via agent",
		session: AsyncSession = None,
	) -> Dict[str, Any]:
		"""Retire une quantité d'un stock par identifiant après contrôle de propriété."""
		async def _op(sess: AsyncSession) -> Dict[str, Any]:
			pid = uuid.UUID(str(producer_id))
			sid = uuid.UUID(str(stock_id))
			stmt = (
				select(Stock)
				.join(Farm, Stock.farm_id == Farm.id)
				.where(Stock.id == sid, Farm.producer_id == pid)
				.with_for_update()
			)
			stock = (await sess.execute(stmt)).scalars().first()
			if not stock:
				return {"status": "error", "message": "Stock introuvable ou accès refusé."}

			try:
				q = float(quantity)
			except Exception:
				return {"status": "error", "message": "Quantité invalide."}
			if q <= 0:
				return {"status": "error", "message": "La quantité doit être > 0."}

			available = float(stock.quantity or 0.0)
			if available < q:
				return {"status": "error", "message": f"Stock insuffisant : {available} {stock.unit} disponibles."}

			stock.quantity = available - q
			sess.add(StockMovement(stock_id=stock.id, type="OUT", quantity=q, reason=reason))
			await sess.flush()
			return {
				"status": "success",
				"stock_id": str(stock.id),
				"item_name": stock.item_name,
				"removed": q,
				"remaining": stock.quantity,
				"unit": stock.unit,
			}

		return await self._execute_transaction(_op, session=session)

	async def add_stock_movement_by_id(
		self,
		producer_id: str,
		stock_id: str,
		movement_type: str,
		quantity: float,
		reason: str | None = None,
		session: AsyncSession = None,
	) -> Dict[str, Any]:
		"""Mouvement IN/OUT sur un stock, par identifiant, avec contrôle de propriété."""
		async def _op(sess: AsyncSession) -> Dict[str, Any]:
			pid = uuid.UUID(str(producer_id))
			sid = uuid.UUID(str(stock_id))
			mtype = str(movement_type or "").upper().strip()
			if mtype not in {"IN", "OUT"}:
				return {"status": "error", "message": "movement_type invalide (IN/OUT)."}

			stmt = (
				select(Stock)
				.join(Farm, Stock.farm_id == Farm.id)
				.where(Stock.id == sid, Farm.producer_id == pid)
				.with_for_update()
			)
			stock = (await sess.execute(stmt)).scalars().first()
			if not stock:
				return {"status": "error", "message": "Stock introuvable ou accès refusé."}

			try:
				q = float(quantity)
			except Exception:
				return {"status": "error", "message": "Quantité invalide."}
			if q <= 0:
				return {"status": "error", "message": "La quantité doit être > 0."}

			old_q = float(stock.quantity or 0.0)
			new_q = old_q + q if mtype == "IN" else old_q - q
			if new_q < 0:
				return {"status": "error", "message": f"Stock insuffisant : {old_q} {stock.unit} disponibles."}
			stock.quantity = new_q
			sess.add(StockMovement(stock_id=stock.id, type=mtype, quantity=q, reason=reason))
			await sess.flush()
			return {
				"status": "success",
				"movement": {"type": mtype, "quantity": q, "reason": reason},
				"old_quantity": old_q,
				"new_quantity": new_q,
				"unit": stock.unit,
			}

		return await self._execute_transaction(_op, session=session)

	async def delete_stock_by_id(self, producer_id: str, stock_id: str, session: AsyncSession = None) -> Dict[str, Any]:
		"""Supprime une ligne de stock par identifiant après contrôle de propriété."""
		async def _op(sess: AsyncSession) -> Dict[str, Any]:
			pid = uuid.UUID(str(producer_id))
			sid = uuid.UUID(str(stock_id))
			stmt = (
				select(Stock)
				.join(Farm, Stock.farm_id == Farm.id)
				.where(Stock.id == sid, Farm.producer_id == pid)
				.with_for_update()
			)
			stock = (await sess.execute(stmt)).scalars().first()
			if not stock:
				return {"status": "error", "message": "Stock introuvable ou accès refusé."}
			await sess.delete(stock)
			await sess.flush()
			return {"status": "success", "deleted": True}

		return await self._execute_transaction(_op, session=session)
	async def get_or_create_farm(self, producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().get_or_create_farm, producer_id, farm_name, zone_id, session=session)

	async def get_farms(self, producer_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_farms, producer_id, session=session, commit=False)

	async def update_farm(self, farm_id: str, session: AsyncSession = None, **kwargs):
		return await self._execute_transaction(super().update_farm, farm_id, session=session, **kwargs)

	async def add_stock(self, farm_id: str, item_name: str, quantity: float, unit: str = "KG", stock_type: str = "HARVEST", reason: str = "Ajout via agent", warehouse_id: str = None, organization_id: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().add_stock, farm_id, item_name, quantity, unit, stock_type, reason, warehouse_id, organization_id, session=session)

	async def remove_stock(self, farm_id: str, item_name: str, quantity: float, reason: str = "Retrait", movement_type: str = "OUT", session: AsyncSession = None):
		return await self._execute_transaction(super().remove_stock, farm_id, item_name, quantity, reason, movement_type, session=session)

	async def adjust_stock(self, farm_id: str, item_name: str, quantity_change: float, reason: str = "Adjustment via MCP", unit: str = "KG", stock_type: str = "HARVEST", warehouse_id: str = None, organization_id: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().adjust_stock, farm_id, item_name, quantity_change, reason, unit, stock_type, warehouse_id, organization_id, session=session)

	async def get_stocks(self, farm_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_stocks, farm_id, session=session, commit=False)



	async def get_stock_movements(self, stock_id: str, limit: int = 20, session: AsyncSession = None):
		return await self._execute_transaction(super().get_stock_movements, stock_id, clamp_limit(limit), session=session, commit=False)

	async def create_product(self, producer_id: str, name: str, price , quantity_for_sale,  category_label: str,unit: str = "KG", sub_category_id: str = None, description: str = None, local_names: dict = None, session: AsyncSession = None):
		return await self._execute_transaction(super().create_product, producer_id, name, price, quantity_for_sale, unit, category_label, sub_category_id, description, local_names, session=session)

	async def list_products(self, producer_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().list_products, producer_id, session=session, commit=False)

	async def search_products(self, product_name: str, zone_id: str = None, limit: int = 10, session: AsyncSession = None):
		return await self._execute_transaction(super().search_products, product_name, zone_id, clamp_limit(limit, default=10), session=session, commit=False)

	async def create_order(self, product_id: str, quantity: float, buyer_phone: str, buyer_name: str = None, zone_id: str = None, source: str = "WHATSAPP", buyer_id: str = None, organization_id: str = None, payment_method: str = "CASH", session: AsyncSession = None):
		return await self._execute_transaction(super().create_order, product_id, quantity, buyer_phone, buyer_name, zone_id, source, buyer_id, organization_id, payment_method, session=session)



	async def get_orders(self, buyer_id: str = None, buyer_phone: str = None, status: str = None, limit: int = 20, session: AsyncSession = None):
		return await self._execute_transaction(super().get_orders, buyer_id, buyer_phone, status, clamp_limit(limit), session=session, commit=False)

	async def update_order_status(self, order_id: str, new_status: str, payment_status: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().update_order_status, order_id, new_status, payment_status, session=session)

	async def get_or_create_client(self, producer_id: str, name: str, phone: str, email: str = None, location: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().get_or_create_client, producer_id, name, phone, email, location, session=session)

	async def get_clients(self, producer_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_clients, producer_id, session=session, commit=False)

	async def add_expense(self, farm_id: str, label: str, amount: float, category: str = "OTHER", date=None, session: AsyncSession = None):
		return await self._execute_transaction(super().add_expense, farm_id, label, amount, category, date, session=session)

	async def get_expenses(self, farm_id: str, category: str = None, limit: int = 50, session: AsyncSession = None):
		return await self._execute_transaction(super().get_expenses, farm_id, category, clamp_limit(limit, default=50), session=session, commit=False)

	async def get_expense_summary(self, farm_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_expense_summary, farm_id, session=session, commit=False)

	
	# ==================================================================
	# Surplus offers
	# ==================================================================
	async def create_surplus_offer(self, user_id: str | None, product_name: str, quantity_kg: float, price_kg: float | None = None, zone_id: str | None = None, location: str | None = None, channel: str = "api", session: AsyncSession = None):
		return await self._execute_transaction(super().create_surplus_offer, user_id, product_name, quantity_kg, price_kg, zone_id, location, channel, session=session)

	# ==================================================================
	# Transactions / staging
	# ==================================================================
	async def prepare_transaction_staging(self, payload: dict, expires_in_seconds: int = 3600, session: AsyncSession = None):
		return await self._execute_transaction(super().prepare_transaction_staging, payload, expires_in_seconds, session=session)

	async def get_staged_transaction(self, transaction_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_staged_transaction, transaction_id, session=session, commit=False)

	async def commit_staged_transaction(self, transaction_id: str, approved: bool = True, session: AsyncSession = None):
		return await self._execute_transaction(super().commit_staged_transaction, transaction_id, approved, session=session)

	async def delete_expired_stagings(self, older_than_seconds: int = 0, session: AsyncSession = None):
		return await self._execute_transaction(super().delete_expired_stagings, older_than_seconds, session=session)

	# ==================================================================
	# Intelligence
	# ==================================================================
	async def record_zone_metric(self, zone_id: str, metric_name: str, value: float, session: AsyncSession = None):
		return await self._execute_transaction(super().record_zone_metric, zone_id, metric_name, value, session=session)

	async def get_user_context(self, user_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_user_context, user_id, session=session, commit=False)

	async def upsert_user_context(self, user_id: str, last_intent: str = None, pending_intent: str = None, draft_data: dict = None, session: AsyncSession = None):
		payload = {"user_id": user_id, "last_intent": last_intent, "pending_intent": pending_intent, "draft_data": draft_data}
		return await self._execute_transaction(super().upsert_user_context, payload, session=session)

	async def create_market_match(self, product_id: str, buyer_id: str = None, score: float = 0.0, status: str = "SUGGESTED", meta: dict = None, session: AsyncSession = None):
		payload = {"product_id": product_id, "buyer_id": buyer_id, "score": score, "status": status, "meta": meta}
		return await self._execute_transaction(super().create_market_match, payload, session=session)

	async def list_market_matches(self, buyer_id: str = None, status: str = None, limit: int = 20, session: AsyncSession = None):
		filters = {"buyer_id": buyer_id, "status": status, "limit": clamp_limit(limit)}
		return await self._execute_transaction(super().list_market_matches, filters, session=session, commit=False)

	async def log_conversation(self, user_id: str, query: str, response: str, agent_type: str = None, crop: str = None, zone_id: str = None, mode: str = "text", audio_url: str = None, execution_path: list = None, confidence_score: float = None, tokens_used: int = 0, response_time_ms: int = None, session: AsyncSession = None):
		return await self._execute_transaction(super().log_conversation, user_id, query, response, agent_type, crop, zone_id, mode, audio_url, execution_path, confidence_score, tokens_used, response_time_ms, session=session)

	async def create_agent_action(self, agent_name: str, action_type: str, payload: dict, user_id: str = None, priority: str = "MEDIUM", ai_reasoning: str = None, order_id: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().create_agent_action, agent_name, action_type, payload, user_id, priority, ai_reasoning, order_id, session=session)

	async def get_pending_actions(self, agent_name: str = None, limit: int = 20, session: AsyncSession = None):
		return await self._execute_transaction(super().get_pending_actions, agent_name, clamp_limit(limit), session=session, commit=False)

	async def update_action_status(self, action_id: str, new_status: str, admin_notes: str = None, validated_by_id: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().update_action_status, action_id, new_status, admin_notes, validated_by_id, session=session)

	async def log_audit(self, actor_id: str, action: str, entity_type: str, entity_id: str, old_value: dict = None, new_value: dict = None, ip_address: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().log_audit, actor_id, action, entity_type, entity_id, old_value, new_value, ip_address, session=session)

	async def get_trust_score(self, user_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_trust_score, user_id, session=session, commit=False)

	async def update_trust_score(self, user_id: str, agent_name: str, justification: str, data_points: dict, reliability_delta: float = 0, quality_delta: float = 0, compliance_delta: float = 0, resilience_delta: float = 0, session: AsyncSession = None):
		return await self._execute_transaction(super().update_trust_score, user_id, agent_name, justification, data_points, reliability_delta, quality_delta, compliance_delta, resilience_delta, session=session)

	async def report_anomaly(self, zone_id: str, level: str, title: str, message: str = None, source: str = None, details: dict = None, session: AsyncSession = None):
		return await self._execute_transaction(super().report_anomaly, zone_id, level, title, message, source, details, session=session)

	async def get_active_anomalies(self, zone_id: str = None, limit: int = 20, session: AsyncSession = None):
		return await self._execute_transaction(super().get_active_anomalies, zone_id, clamp_limit(limit), session=session, commit=False)

	async def emit_territory_event(self, zone_id: str, event_type: str, payload: dict = None, meta: dict = None, session: AsyncSession = None):
		return await self._execute_transaction(super().emit_territory_event, zone_id, event_type, payload, meta, session=session)

	# ==================================================================
	# Dashboards
	# ==================================================================
	async def get_producer_dashboard(self, producer_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_producer_dashboard, producer_id, session=session, commit=False)

	async def get_zone_market_overview(self, zone_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_zone_market_overview, zone_id, session=session, commit=False)

	
	# =================================================================
    # CROP MANAGEMENT (Delegated to CropMixin)
    # =================================================================

	async def create_crop_cycle(self, farm_id: str, data: Dict[str, Any], session: AsyncSession = None):
		return await self._execute_transaction(super().create_crop_cycle, farm_id, data, session=session)

	async def get_cycle_with_context(self, cycle_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_cycle_with_context, cycle_id, session=session)

	async def log_intervention(self, cycle_id: str, data: Dict[str, Any], session: AsyncSession = None):
		return await self._execute_transaction(super().log_intervention, cycle_id, data, session=session)

	async def add_growth_log(self, cycle_id: str, stage_code: int, image_url: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().add_growth_log, cycle_id, stage_code, image_url=image_url, session=session)

	async def create_recommendation(self, user_id: str, cycle_id: str, rec_data: Dict[str, Any], session: AsyncSession = None):
		return await self._execute_transaction(super().create_recommendation, user_id, cycle_id, rec_data, session=session)

	async def get_agent_memory(self, user_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_agent_memory, user_id, session=session)

	async def update_agent_memory(self, user_id: str, last_topic: str, alerts: Dict = None, prefs: Dict = None, session: AsyncSession = None):
		return await self._execute_transaction(super().update_agent_memory, user_id, last_topic, alerts=alerts, prefs=prefs, session=session)

	async def get_yield_performance_metrics(self, cycle_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_yield_performance_metrics, cycle_id, session=session)

	async def get_biological_readiness(self, cycle_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_biological_readiness, cycle_id, session=session)

	async def get_cycle_economics(self, cycle_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_cycle_economics, cycle_id, session=session)

	async def get_active_sanitary_risks(self, farm_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_active_sanitary_risks, farm_id, session=session)

	async def get_crop_requirements(self, crop_type: str, variety: str = None, session: AsyncSession = None):
		return await self._execute_transaction(super().get_crop_requirements, crop_type, variety=variety, session=session)

	async def analyze_thermal_stress(self, cycle_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().analyze_thermal_stress, cycle_id, session=session)

	async def add_agronomic_standard(self, data: Dict[str, Any], session: AsyncSession = None):
		return await self._execute_transaction(super().add_agronomic_standard, data, session=session)

	async def add_crop_growth_stage(self, data: Dict[str, Any], session: AsyncSession = None):
			return await self._execute_transaction(super().add_crop_growth_stage, data, session=session)

	async def add_pest_disease_trigger(self, data: Dict[str, Any], session: AsyncSession = None):
		return await self._execute_transaction(super().add_pest_disease_trigger, data, session=session)

	async def add_growth_stage_requirement(self, data: Dict[str, Any], session: AsyncSession = None):
		return await self._execute_transaction(super().add_growth_stage_requirement, data, session=session)

	async def check_growth_compliance(self, cycle_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().check_growth_compliance, cycle_id, session=session)

	async def get_instant_resource_needs(self, cycle_id: str, session: AsyncSession = None):
		return await self._execute_transaction(super().get_instant_resource_needs, cycle_id, session=session)



__all__ = ["AgriDatabaseService"]
if __name__ == "__main__":
    import asyncio
    import sys
    import logging
    from datetime import datetime, timedelta

    async def test_agronomic_brain():
        # 1. Configuration du logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        
        service = AgriDatabaseService()
        
        # Tes IDs de test
        farm_id = "f4d7cedd-1859-4446-89e6-6b1af78ef81a"
        user_id = "5a33023c-b955-4a9c-b72e-7e62017f0145"

        print(f"\n--- 🧠 1. TEST : seed_agronomic_knowledge ---")
        try:
            # On remplit le cerveau avec le Maïs, Mil, etc.
           
            cycle = await service.get_cycle_with_context("eb497ba9-763d-49e6-932a-6140db52970b")

            print(f"✅ Cycle créé : ID {json.dumps(cycle['id'])}, {cycle}")

            

        except Exception as e:
            print(f"❌ Erreur critique lors du test : {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            from agriconnect.core.database import close_db
            await close_db()
            print("\n--- 🔒 Connexion fermée ---")

    # Lancement du test
    try:
        asyncio.run(test_agronomic_brain())
    except KeyboardInterrupt:
        sys.exit(0)

