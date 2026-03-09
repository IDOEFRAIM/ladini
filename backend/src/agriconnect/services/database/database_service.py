"""Thin wrapper to preserve historic import path.

The original monolithic implementation was split into mixins under
`agriconnect.services.database`. To avoid breaking imports that target
`agriconnect.services.database.database_service`, this module simply
re-exports the composed `AgriDatabaseService` facade.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
import logging
import asyncio
from typing import Any, Callable

from .common import get_db
from agriconnect.core.database import init_db, close_db, check_connection
from .auth import AuthMixin
from .utils import UtilsMixin
from .marketplace import MarketplaceMixin
from .transactions import TransactionsMixin
from .intelligence import IntelligenceMixin
from .dashboards import DashboardsMixin


class AgriDatabaseService(AuthMixin, UtilsMixin, MarketplaceMixin, TransactionsMixin, IntelligenceMixin, DashboardsMixin):
	"""Service central gérant le cycle de vie des sessions et la délégation aux mixins.

	Cette classe expose les mêmes méthodes publiques qu'avant, mais centralise
	l'ouverture/fermeture des sessions via `get_db()` et injecte la session dans
	les mixins (qui attendent `session: AsyncSession` en premier argument).
	"""

	# Exceptions specific to the DB service
	class DatabaseServiceError(Exception):
		"""Generic wrapper exception used to avoid leaking SQLAlchemy internals."""
		pass

	class IntegrityError(DatabaseServiceError):
		pass

	# Internal logger
	_logger = logging.getLogger(__name__)

	async def _execute_transaction(self, func: Callable, *args, commit: bool = True, **kwargs) -> Any:
		"""Run a mixin function inside a managed session with error handling.

		- Opens a session via `get_db()`
		- Calls `func(session, *args, **kwargs)`
		- Commits on success when `commit=True`
		- Rolls back and raises a `DatabaseServiceError` on failure
		"""
		async with get_db() as session:
			try:
				result = await func(session, *args, **kwargs)
				if commit:
					await session.commit()
				return result
			except SQLAlchemyError as e:
				# Map SQLAlchemy errors to service-level errors
				await session.rollback()
				self._logger.exception("Database error in %s: %s", getattr(func, "__name__", str(func)), e)
				# You could inspect `e` and raise more specific exceptions here
				raise self.DatabaseServiceError(f"Database operation failed: {getattr(func, '__name__', str(func))}") from e
			except Exception as e:
				await session.rollback()
				self._logger.exception("Unexpected error in %s: %s", getattr(func, "__name__", str(func)), e)
				raise

	# --- Auth
	async def get_user_by_phone(self, phone: str):
		return await self._execute_transaction(super().get_user_by_phone, phone, commit=False)

	async def get_user_by_id(self, user_id: str):
		return await self._execute_transaction(super().get_user_by_id, user_id, commit=False)

	async def identify_or_create_user(self, phone: str, name: str = None, zone_id: str = None):
		return await self._execute_transaction(super().identify_or_create_user, phone, name, zone_id)

	# --- Utils
	async def normalize_unit(self, quantity: float, unit: str):
		return await self._execute_transaction(super().normalize_unit, quantity, unit, commit=False)

	async def guess_category(self, product_name: str):
		return await self._execute_transaction(super().guess_category, product_name, commit=False)

	async def check_price_anomaly(self, product_name: str, proposed_price: float, zone_id: str):
		return await self._execute_transaction(super().check_price_anomaly, product_name, proposed_price, zone_id, commit=False)

	# --- Marketplace (Farms / Stocks / Products / Orders / Clients / Expenses / Crops)
	async def get_or_create_farm(self, producer_id: str, farm_name: str = "Ma ferme", zone_id: str = None):
		return await self._execute_transaction(super().get_or_create_farm, producer_id, farm_name, zone_id)

	async def get_farms(self, producer_id: str):
		return await self._execute_transaction(super().get_farms, producer_id, commit=False)

	async def update_farm(self, farm_id: str, **kwargs):
		return await self._execute_transaction(super().update_farm, farm_id, **kwargs)

	async def add_stock(self, farm_id: str, item_name: str, quantity: float, unit: str = "KG", stock_type: str = "HARVEST", reason: str = "Ajout via agent", warehouse_id: str = None, organization_id: str = None):
		return await self._execute_transaction(super().add_stock, farm_id, item_name, quantity, unit, stock_type, reason, warehouse_id, organization_id)

	async def remove_stock(self, farm_id: str, item_name: str, quantity: float, reason: str = "Retrait", movement_type: str = "OUT"):
		return await self._execute_transaction(super().remove_stock, farm_id, item_name, quantity, reason, movement_type)

	async def adjust_stock(self, farm_id: str, item_name: str, quantity_change: float, reason: str = "Adjustment via MCP", unit: str = "KG", stock_type: str = "HARVEST", warehouse_id: str = None, organization_id: str = None):
		return await self._execute_transaction(super().adjust_stock, farm_id, item_name, quantity_change, reason, unit, stock_type, warehouse_id, organization_id)

	async def get_stocks(self, farm_id: str):
		return await self._execute_transaction(super().get_stocks, farm_id, commit=False)

	async def get_stock_movements(self, stock_id: str, limit: int = 20):
		return await self._execute_transaction(super().get_stock_movements, stock_id, limit, commit=False)

	async def create_product(self, producer_id: str, name: str = None, price: float = 0, quantity_for_sale: float = 0, unit: str = "KG", category_label: str = "Céréales", sub_category_id: str = None, description: str = None, local_names: dict = None):
		return await self._execute_transaction(super().create_product, producer_id, name, price, quantity_for_sale, unit, category_label, sub_category_id, description, local_names)

	async def list_products(self, producer_id: str):
		return await self._execute_transaction(super().list_products, producer_id, commit=False)

	async def search_products(self, product_name: str, zone_id: str = None, limit: int = 10):
		return await self._execute_transaction(super().search_products, product_name, zone_id, limit, commit=False)

	async def create_order(self, product_id: str, quantity: float, buyer_phone: str, buyer_name: str = None, zone_id: str = None, source: str = "WHATSAPP", buyer_id: str = None, organization_id: str = None, payment_method: str = "CASH"):
		return await self._execute_transaction(super().create_order, product_id, quantity, buyer_phone, buyer_name, zone_id, source, buyer_id, organization_id, payment_method)

	async def get_orders(self, buyer_id: str = None, buyer_phone: str = None, status: str = None, limit: int = 20):
		return await self._execute_transaction(super().get_orders, buyer_id, buyer_phone, status, limit, commit=False)

	async def update_order_status(self, order_id: str, new_status: str, payment_status: str = None):
		return await self._execute_transaction(super().update_order_status, order_id, new_status, payment_status)

	async def get_or_create_client(self, producer_id: str, name: str, phone: str, email: str = None, location: str = None):
		return await self._execute_transaction(super().get_or_create_client, producer_id, name, phone, email, location)

	async def get_clients(self, producer_id: str):
		return await self._execute_transaction(super().get_clients, producer_id, commit=False)

	async def add_expense(self, farm_id: str, label: str, amount: float, category: str = "OTHER", date=None):
		return await self._execute_transaction(super().add_expense, farm_id, label, amount, category, date)

	async def get_expenses(self, farm_id: str, category: str = None, limit: int = 50):
		return await self._execute_transaction(super().get_expenses, farm_id, category, limit, commit=False)

	async def get_expense_summary(self, farm_id: str):
		return await self._execute_transaction(super().get_expense_summary, farm_id, commit=False)

	async def create_crop_cycle(self, farm_id: str, crop_type: str, area_size: float, planted_at, expected_harvest_date, expected_yield: float, status: str = "PLANTED"):
		return await self._execute_transaction(super().create_crop_cycle, farm_id, crop_type, area_size, planted_at, expected_harvest_date, expected_yield, status)

	async def get_crop_cycles(self, farm_id: str):
		return await self._execute_transaction(super().get_crop_cycles, farm_id, commit=False)

	# --- Transactions / staging
	async def prepare_transaction_staging(self, payload: dict, expires_in_seconds: int = 3600):
		return await self._execute_transaction(super().prepare_transaction_staging, payload, expires_in_seconds)

	async def get_staged_transaction(self, transaction_id: str):
		return await self._execute_transaction(super().get_staged_transaction, transaction_id, commit=False)

	async def commit_staged_transaction(self, transaction_id: str, approved: bool = True):
		return await self._execute_transaction(super().commit_staged_transaction, transaction_id, approved)

	async def delete_expired_stagings(self, older_than_seconds: int = 0):
		return await self._execute_transaction(super().delete_expired_stagings, older_than_seconds)

	# --- Intelligence
	async def record_zone_metric(self, zone_id: str, metric_name: str, value: float):
		return await self._execute_transaction(super().record_zone_metric, zone_id, metric_name, value)

	async def log_conversation(self, user_id: str, query: str, response: str, agent_type: str = None, crop: str = None, zone_id: str = None, mode: str = "text", audio_url: str = None, execution_path: list = None, confidence_score: float = None, tokens_used: int = 0, response_time_ms: int = None):
		return await self._execute_transaction(super().log_conversation, user_id, query, response, agent_type, crop, zone_id, mode, audio_url, execution_path, confidence_score, tokens_used, response_time_ms)

	async def create_agent_action(self, agent_name: str, action_type: str, payload: dict, user_id: str = None, priority: str = "MEDIUM", ai_reasoning: str = None, order_id: str = None):
		return await self._execute_transaction(super().create_agent_action, agent_name, action_type, payload, user_id, priority, ai_reasoning, order_id)

	async def get_pending_actions(self, agent_name: str = None, limit: int = 20):
		return await self._execute_transaction(super().get_pending_actions, agent_name, limit, commit=False)

	async def update_action_status(self, action_id: str, new_status: str, admin_notes: str = None, validated_by_id: str = None):
		return await self._execute_transaction(super().update_action_status, action_id, new_status, admin_notes, validated_by_id)

	async def log_audit(self, actor_id: str, action: str, entity_type: str, entity_id: str, old_value: dict = None, new_value: dict = None, ip_address: str = None):
		return await self._execute_transaction(super().log_audit, actor_id, action, entity_type, entity_id, old_value, new_value, ip_address)

	async def get_trust_score(self, user_id: str):
		return await self._execute_transaction(super().get_trust_score, user_id, commit=False)

	async def update_trust_score(self, user_id: str, agent_name: str, justification: str, data_points: dict, reliability_delta: float = 0, quality_delta: float = 0, compliance_delta: float = 0, resilience_delta: float = 0):
		return await self._execute_transaction(super().update_trust_score, user_id, agent_name, justification, data_points, reliability_delta, quality_delta, compliance_delta, resilience_delta)

	async def report_anomaly(self, zone_id: str, level: str, title: str, message: str = None, source: str = None, details: dict = None):
		return await self._execute_transaction(super().report_anomaly, zone_id, level, title, message, source, details)

	async def get_active_anomalies(self, zone_id: str = None, limit: int = 20):
		return await self._execute_transaction(super().get_active_anomalies, zone_id, limit, commit=False)

	async def emit_territory_event(self, zone_id: str, event_type: str, payload: dict = None, meta: dict = None):
		return await self._execute_transaction(super().emit_territory_event, zone_id, event_type, payload, meta)

	# --- Dashboards
	async def get_producer_dashboard(self, producer_id: str):
		return await self._execute_transaction(super().get_producer_dashboard, producer_id, commit=False)

	async def get_zone_market_overview(self, zone_id: str):
		return await self._execute_transaction(super().get_zone_market_overview, zone_id, commit=False)


__all__ = ["AgriDatabaseService"]

if __name__ == "__main__":
	# Run all DB async operations inside one event loop to avoid "Event loop is closed" issues
	PRODUCER_ID = "3cadb350-59e5-4ad8-ae95-5ff7cc1350bd"

	async def _main():
		try:
			init_db()
		except Exception as e:
			print("Failed to initialize DB engine:", e)
			return

		ok = False
		try:
			ok = await check_connection()
		except Exception as e:
			print("DB health check failed:", e)

		if not ok:
			print("Database seems unavailable. Check DATABASE_URL and DB_CA_PATH.")
			return

		svc = AgriDatabaseService()
		try:
			result = await svc.list_products(PRODUCER_ID)
			print(result)
		except AgriDatabaseService.DatabaseServiceError as e:
			print("DatabaseServiceError:", e)
		except Exception as e:
			print("Unexpected error:", e)
		finally:
			try:
				await close_db()
			except Exception:
				pass

	asyncio.run(_main())