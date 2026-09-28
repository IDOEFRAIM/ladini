import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import desc, func, select

from ladini.domain.models import (
    Expense,
    Order,
    OrderItem,
    Producer,
    Product,
    Stock,
    StockMovement,
    User,
    _uuid4,
)

from .base import BaseMixin
from .common import clean_text, positive_float
from .errors import BusinessRuleException
from .pricing_persistence import declared_sale_snapshot_columns

logger = logging.getLogger("ladini.services.database")


def _uuid() -> str:
    return _uuid4()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError):
        return False


class MarketplaceMixin(BaseMixin):
    # =======================================================================
    # RESOLUTION HELPERS
    # =======================================================================

    async def _resolve_producer_id(self, identifier: str) -> Union[str, Dict[str, Any]]:
        """Resolve a producer_id from either a UUID or a phone number."""
        current_session = self.session
        if _is_uuid(identifier):
            stmt = select(Producer).where(Producer.id == identifier)
            result = await current_session.execute(stmt)
            prod = result.scalar_one_or_none()
            if prod:
                return str(prod.id)
            stmt2 = select(Producer).where(Producer.user_id == identifier)
            result2 = await current_session.execute(stmt2)
            prod2 = result2.scalar_one_or_none()
            if prod2:
                return str(prod2.id)
            return {
                "status": "error",
                "message": f"Profil producteur introuvable pour l'identifiant : {identifier}",
            }
        else:
            stmt = select(User).where(User.phone == str(identifier).strip())
            result = await current_session.execute(stmt)
            user = result.scalar_one_or_none()
            if not user:
                return {
                    "status": "error",
                    "message": f"Aucun utilisateur trouvé pour : {identifier}",
                }
            stmt2 = select(Producer).where(Producer.user_id == user.id)
            result2 = await current_session.execute(stmt2)
            prod = result2.scalar_one_or_none()
            if not prod:
                return {
                    "status": "error",
                    "message": f"Profil producteur introuvable pour l'utilisateur : {identifier}",
                }
            return str(prod.id)

    # =======================================================================
    # CATEGORY RESOLUTION
    # =======================================================================

    # NB : `guess_category` vit désormais UNIQUEMENT dans `ProducerMgmtMixin`
    # (services/database/producer.py) — résolution DYNAMIQUE via la table
    # Category/SubCategory en base, avec repli sur mots-clés seulement en dernier
    # recours. Ce mixin en définissait un doublon STATIQUE (mapping figé, jamais
    # mis à jour) qui gagnait silencieusement via l'ordre du MRO. Voir
    # [[farm-autoprovision-critical-goals]] pour le mécanisme général du piège.

    # =======================================================================
    # GESTION DE L'EXPLOITATION (FARM)
    # =======================================================================

    # NB : `get_or_create_farm` et `update_farm` vivent désormais UNIQUEMENT dans
    # `ProducerMgmtMixin` (services/database/producer.py). Ce mixin (`MarketplaceMixin`)
    # en définissait autrefois des doublons — un piège de MRO silencieux : listé
    # AVANT `ProducerMgmtMixin` dans `AgriDatabaseService` (services/database/d.py),
    # ce doublon gagnait la résolution d'attribut et masquait la version phone-aware
    # qui auto-crée le profil Producer manquant. Symptôme observé : `get_or_create_farm`
    # rejetait "producer_id is a required property" alors que l'appelant envoyait
    # `phone` — CETTE version (supprimée) n'acceptait pas `phone` du tout. Voir
    # [[farm-autoprovision-critical-goals]]. Ne pas réintroduire un mixin dupliqué
    # sans vérifier `AgriDatabaseService.__mro__` pour la méthode concernée.

    # =======================================================================
    # GESTION DES STOCKS
    # =======================================================================

    async def add_stock(
        self,
        farm_id: str,
        item_name: str,
        quantity: float,
        producer_phone: str,
        unit: str = "KG",
        stock_type: str = "HARVEST",
        reason: str = "Ajout via agent",
        warehouse_id: str = None,
        organization_id: str = None,
    ) -> Dict[str, Any]:
        """Incrémente ou crée une ligne de stock pour un produit donné.

        ``producer_phone`` est OBLIGATOIRE (audit sécurité agent 2026-09-10) :
        c'est l'identité du tour, seule preuve d'autorisation sur ``farm_id``.
        Avant ce correctif, cette méthode ne vérifiait que l'EXISTENCE de la
        ferme (`Farm.id == farm_id`) — donc tout `farm_id` valide de la
        plateforme était une cible d'écriture. Voir
        ``BaseMixin._assert_farm_owned_by``.
        """
        current_session = self.session
        farm_id = clean_text(farm_id, "farm_id", required=True)
        item_name = clean_text(item_name, "item_name", required=True)
        quantity = positive_float(quantity, "quantity")
        unit = clean_text(unit, "unit", required=False, max_length=20) or "KG"

        await self._assert_farm_owned_by(farm_id, producer_phone)

        stmt = (
            select(Stock)
            .where(
                Stock.farm_id == farm_id,
                func.lower(Stock.item_name) == item_name.lower(),
            )
            .with_for_update()
        )
        result = await current_session.execute(stmt)
        stock = result.scalar_one_or_none()

        if stock:
            # `quantity` (ci-dessus, `positive_float`) est un `float` ;
            # `Stock.quantity` est une colonne `Numeric` (chargée en
            # `decimal.Decimal`) — `Decimal += float` lève `TypeError`
            # (même classe de bug que `services/database/producer.py::
            # cancel_confirmed_order`, incident réel 2026-09-15). Conversion
            # explicite avant l'arithmétique, jamais d'opérateur augmenté
            # sur la colonne Decimal directement.
            stock.quantity = float(stock.quantity or 0.0) + quantity
            stock_id = str(stock.id)
            new_total = stock.quantity
        else:
            stock_id = _uuid()
            stock = Stock(
                id=stock_id,
                farm_id=farm_id,
                item_name=item_name,
                quantity=quantity,
                unit=unit,
                type=stock_type,
                warehouse_id=warehouse_id,
                organization_id=organization_id,
            )
            current_session.add(stock)
            new_total = quantity

        mvt = StockMovement(
            id=_uuid(), stock_id=stock_id, type="IN", quantity=quantity, reason=reason
        )
        current_session.add(mvt)
        await current_session.flush()

        return {
            "status": "success",
            "data": {
                "stock_id": stock_id,
                "item_name": item_name,
                "added": quantity,
                "new_total": new_total,
                "unit": unit,
            },
        }

    async def remove_stock(
        self,
        farm_id: str,
        item_name: str,
        quantity: float,
        producer_phone: str,
        reason: str = "Retrait",
        movement_type: str = "OUT",
    ) -> Dict[str, Any]:
        """Décrémente le stock d'un produit après vérification des disponibilités.

        ``producer_phone`` est OBLIGATOIRE (audit sécurité agent 2026-09-10).
        C'était la variante la plus dommageable de la faille IDOR sur
        ``farm_id`` : elle ne vérifiait même pas l'existence de la ferme et
        ciblait directement `Stock.farm_id == farm_id`, permettant de DÉTRUIRE
        l'inventaire vendable d'un autre producteur. Voir
        ``BaseMixin._assert_farm_owned_by``.
        """
        current_session = self.session
        farm_id = clean_text(farm_id, "farm_id", required=True)
        item_name = clean_text(item_name, "item_name", required=True)
        quantity = positive_float(quantity, "quantity")

        await self._assert_farm_owned_by(farm_id, producer_phone)

        stmt = (
            select(Stock)
            .where(
                Stock.farm_id == farm_id,
                func.lower(Stock.item_name) == item_name.lower(),
            )
            .with_for_update()
        )
        result = await current_session.execute(stmt)
        stock = result.scalar_one_or_none()

        if not stock:
            raise BusinessRuleException(
                f"Aucun stock de '{item_name}' trouvé pour cette exploitation."
            )
        if stock.quantity < quantity:
            raise BusinessRuleException(
                f"Stock insuffisant : {stock.quantity} {stock.unit} disponibles, retrait de {quantity} {stock.unit} demandé.",
                reason="insufficient_stock",
            )

        # Voir le commentaire miroir dans `add_stock` ci-dessus : `quantity`
        # est un `float`, `Stock.quantity` une colonne `Numeric` — conversion
        # explicite avant l'arithmétique, jamais d'opérateur augmenté direct.
        stock.quantity = float(stock.quantity or 0.0) - quantity

        mvt = StockMovement(
            id=_uuid(),
            stock_id=str(stock.id),
            type=movement_type,
            quantity=quantity,
            reason=reason,
        )
        current_session.add(mvt)
        await current_session.flush()

        return {
            "status": "success",
            "data": {
                "stock_id": str(stock.id),
                "item_name": item_name,
                "removed": quantity,
                "remaining": stock.quantity,
                "unit": stock.unit,
            },
        }

    async def adjust_stock(
        self,
        farm_id: str,
        item_name: str,
        quantity_change: float,
        producer_phone: str,
        reason: str = "Adjustment via MCP",
        unit: str = "KG",
        stock_type: str = "HARVEST",
        warehouse_id: str = None,
        organization_id: str = None,
    ) -> Dict[str, Any]:
        # `producer_phone` propagé aux deux délégués : sans lui, ce point
        # d'entrée serait un contournement complet du contrôle de propriété
        # posé sur `add_stock`/`remove_stock` (audit sécurité agent 2026-09-10).
        if quantity_change >= 0:
            return await self.add_stock(
                farm_id=farm_id,
                item_name=item_name,
                quantity=quantity_change,
                producer_phone=producer_phone,
                unit=unit,
                stock_type=stock_type,
                reason=reason,
                warehouse_id=warehouse_id,
                organization_id=organization_id,
            )
        else:
            return await self.remove_stock(
                farm_id=farm_id,
                item_name=item_name,
                quantity=abs(quantity_change),
                producer_phone=producer_phone,
                reason=reason,
            )

    # NB : `get_stocks` vit désormais UNIQUEMENT dans `ProducerMgmtMixin`
    # (services/database/producer.py) — vue catalogue complète (Product avec
    # prix/quantité_en_vente, multi-fermes, cycles à venir), résolue par
    # phone/producer_id. Ce mixin en définissait un doublon TRIVIAL (un simple
    # dump `Stock` pour UNE ferme, aucune donnée Product) qui gagnait
    # silencieusement via le MRO — cause du bug « le producteur ne voit pas
    # son catalogue » (SALES_GET_CATALOG). Voir [[farm-autoprovision-critical-goals]].

    async def get_farm_stocks(
        self, farm_id: str, producer_phone: str
    ) -> Dict[str, Any]:
        """Inventaire DÉTAILLÉ d'UNE exploitation (goal `STOCK_GET_DETAIL`).

        Complément de `get_stocks` (`STOCK_GET_SUMMARY`), qui donne la vue
        multi-sites : ici on descend dans une seule ferme et on enrichit
        chaque ligne de stock de son dernier mouvement (date + type), ce que
        la vue synthétique n'expose pas.

        Historique (2026-09-10) : cet outil était référencé partout
        (`INTENT_CONFIG`, `ToolId.GET_FARM_STOCKS`, `StockService.get_detail`,
        `TOOL_SCOPE_MAP`) mais AUCUNE méthode ne l'implémentait — le goal
        `STOCK_GET_DETAIL` était donc mort (retiré du catalogue LLM via
        `_DEPRECATED_INTENTS`). Implémenté proprement + ré-exposé.

        ``producer_phone`` OBLIGATOIRE : c'est l'identité du tour (épinglée à
        la session par `schema_resolver`), seule preuve d'autorisation sur
        ``farm_id`` — sans elle cette lecture livrerait l'inventaire complet
        de n'importe quelle exploitation de la plateforme (même classe d'IDOR
        que `get_stock_movements`/`get_expenses`, audit sécurité agent
        2026-09-10). Voir `BaseMixin._assert_farm_owned_by`.
        """
        current_session = self.session
        farm_id = clean_text(farm_id, "farm_id", required=True)
        farm = await self._assert_farm_owned_by(farm_id, producer_phone)

        stock_rows = (
            await current_session.execute(
                select(Stock)
                .where(Stock.farm_id == farm.id)
                .order_by(func.lower(Stock.item_name))
            )
        ).scalars().all()

        stock_ids = [s.id for s in stock_rows]
        last_movement: Dict[Any, StockMovement] = {}
        if stock_ids:
            movement_rows = (
                await current_session.execute(
                    select(StockMovement)
                    .where(StockMovement.stock_id.in_(stock_ids))
                    .order_by(desc(StockMovement.created_at))
                )
            ).scalars().all()
            # Lignes déjà triées par date décroissante : la 1re vue par
            # stock_id est la plus récente.
            for mv in movement_rows:
                last_movement.setdefault(mv.stock_id, mv)

        items: List[Dict[str, Any]] = []
        for s in stock_rows:
            mv = last_movement.get(s.id)
            items.append(
                {
                    "stock_id": str(s.id),
                    "item_name": s.item_name,
                    "quantity": float(s.quantity) if s.quantity is not None else 0.0,
                    "unit": s.unit or "KG",
                    "type": s.type or "HARVEST",
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                    "last_movement_type": mv.type if mv else None,
                    "last_movement_at": (
                        mv.created_at.isoformat() if mv and mv.created_at else None
                    ),
                }
            )

        return {
            "status": "success",
            "data": {
                "farm_id": str(farm.id),
                "farm_name": farm.name,
                "location": getattr(farm, "location", None),
                "count": len(items),
                "stocks": items,
            },
        }

    async def get_stock_movements(
        self, stock_id: str, producer_phone: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Historique des mouvements d'un lot.

        ``producer_phone`` OBLIGATOIRE (audit sécurité agent 2026-09-10) :
        cette lecture n'était scopée que par `stock_id`, donc exposait la
        rotation d'inventaire de n'importe quel producteur.
        """
        current_session = self.session
        await self._assert_stock_owned_by(stock_id, producer_phone)
        stmt = (
            select(StockMovement)
            .where(StockMovement.stock_id == stock_id)
            .order_by(desc(StockMovement.created_at))
            .limit(limit)
        )
        result = await current_session.execute(stmt)
        return [
            {
                "type": m.type,
                "quantity": m.quantity,
                "reason": m.reason,
                "date": m.created_at.isoformat() if m.created_at else None,
            }
            for m in result.scalars()
        ]

    # =======================================================================
    # PRODUCTS
    # =======================================================================

    # NB : `create_product` vit désormais UNIQUEMENT dans `ProducerMgmtMixin`
    # (services/database/producer.py) — valide l'existence du profil producteur
    # (message clair sinon) ET auto-résout `sub_category_id` depuis le nom si
    # absent. Ce mixin en définissait un doublon qui laissait TOUJOURS
    # `sub_category_id=NULL` (jamais résolu) — cassait silencieusement le
    # filtrage MATCHABLE de `get_producer_auctions` (qui filtre sur
    # `Product.sub_category_id`), forçant un repli permanent sur ALL pour
    # chaque producteur. Voir [[farm-autoprovision-critical-goals]],
    # [[auction-bid-lifecycle]].

    async def record_sale(
        self,
        phone: str,
        product_name: str,
        quantity: float,
        total_price: float,
        unit: str = "KG",
        client_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Journalise une vente directe effectuée par un producteur."""

        current_session = self.session
        resolved_producer_id = await self._resolve_producer_id(phone)
        if isinstance(resolved_producer_id, dict):
            # `_resolve_producer_id` est un helper privé qui retourne un dict
            # d'échec par convention interne (non exposé au dispatcher). Le
            # point d'entrée public (`record_sale`, méthode d'écriture wrappée
            # par @transactional) DOIT lever une exception plutôt que
            # retourner ce dict tel quel, pour rester 100% pilotée par le flux
            # d'exécution.
            raise BusinessRuleException(
                resolved_producer_id.get("message", "Profil producteur introuvable."),
                reason="producer_not_found",
            )

        product_label = clean_text(product_name, "product_name", required=True)
        unit_clean = clean_text(unit, "unit", required=False, max_length=20) or "KG"
        quantity_value = positive_float(quantity, "quantity")
        amount_value = positive_float(total_price, "total_price", allow_zero=True)

        stmt_product = (
            select(Product)
            .where(
                Product.producer_id == resolved_producer_id,
                func.lower(Product.name) == product_label.lower(),
            )
            .limit(1)
        )
        product_result = await current_session.execute(stmt_product)
        product = product_result.scalar_one_or_none()

        if not product:
            product = Product(
                id=_uuid(),
                short_code=_uuid()[:8].upper(),
                name=product_label,
                category_label="VENTE_DIRECTE",
                price=amount_value / quantity_value if quantity_value else amount_value,
                unit=unit_clean,
                quantity_for_sale=0,
                producer_id=resolved_producer_id,
                is_available=False,
            )
            current_session.add(product)
            await current_session.flush()

        order_id = _uuid()
        order = Order(
            id=order_id,
            customer_name=client_name or "Vente directe",
            customer_phone=str(phone).strip(),
            payment_method="CASH",
            payment_status="PAID",
            status="COMPLETED",
            delivery_status="FULFILLED",
            source="AGENT",
            order_type="DIRECT_SALE",
            total_amount=amount_value,
            subtotal=amount_value,
            tax_amount=0.0,
            currency="XOF",
            delivery_fee=0.0,
            is_agent_order=True,
        )
        current_session.add(order)

        item_price = amount_value / quantity_value if quantity_value else amount_value
        # Phase B2a : le producteur a dit un TOTAL (« 50 kg pour 25 000 ») -> TOTAL_LOT exact ;
        # `price_at_sale` n'est que le dérivé par unité.
        order_item = OrderItem(
            id=_uuid(),
            order_id=order_id,
            product_id=product.id,
            quantity=quantity_value,
            price_at_sale=item_price,
            **declared_sale_snapshot_columns(
                total_amount=amount_value,
                quantity=quantity_value,
                unit=unit_clean,
                price_at_sale=item_price,
            ),
        )
        current_session.add(order_item)

        await current_session.flush()

        return {
            "status": "success",
            "data": {
                "sale_id": order_id,
                "product_id": str(product.id),
                "quantity": quantity_value,
                "unit": unit_clean,
                "total_amount": amount_value,
                "price_per_unit": item_price,
            },
        }

    # =======================================================================
    # ORDERS
    # =======================================================================

    # NB : `update_order_status` vit désormais UNIQUEMENT dans `ProducerMgmtMixin`
    # (services/database/producer.py) — verrouille la ligne (`with_for_update`)
    # avant mise à jour, évitant une course entre deux mises à jour concurrentes
    # du même statut de commande. Ce mixin en définissait un doublon sans
    # verrou. Voir [[farm-autoprovision-critical-goals]].

    # =======================================================================
    # EXPENSES
    # =======================================================================

    async def add_expense(
        self,
        farm_id: str,
        label: str,
        amount: float,
        producer_phone: str,
        category: str = "OTHER",
        date: datetime = None,
    ) -> Dict[str, Any]:
        """Enregistre une dépense sur une exploitation.

        ``producer_phone`` est OBLIGATOIRE (audit sécurité agent 2026-09-10) :
        cette méthode écrivait un `Expense` sur n'importe quel `farm_id` sans
        même vérifier que la ferme existe — donc sans aucun contrôle de
        propriété. Voir ``BaseMixin._assert_farm_owned_by``.
        """
        current_session = self.session
        farm_id = clean_text(farm_id, "farm_id", required=True)
        label = clean_text(label, "label", required=True)
        amount = positive_float(amount, "amount")

        await self._assert_farm_owned_by(farm_id, producer_phone)
        expense = Expense(
            id=_uuid(),
            farm_id=farm_id,
            label=label,
            amount=amount,
            category=category,
            date=date or datetime.utcnow(),
        )
        current_session.add(expense)
        await current_session.flush()
        return {"status": "success", "data": expense.to_dict()}

    async def get_expenses(
        self, farm_id: str, producer_phone: str, category: str = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Journal des dépenses d'une exploitation.

        ``producer_phone`` OBLIGATOIRE (audit sécurité agent 2026-09-10) : sans
        contrôle de propriété, cette lecture livrait la structure de coûts
        complète de n'importe quel concurrent.
        """
        current_session = self.session
        await self._assert_farm_owned_by(farm_id, producer_phone)
        stmt = select(Expense).where(Expense.farm_id == farm_id)
        if category:
            stmt = stmt.where(Expense.category == category)
        stmt = stmt.order_by(desc(Expense.date)).limit(limit)
        result = await current_session.execute(stmt)
        return [e.to_dict() for e in result.scalars()]

    async def get_expense_summary(
        self, farm_id: str, producer_phone: str
    ) -> Dict[str, Any]:
        """Totaux de dépenses par catégorie.

        ``producer_phone`` OBLIGATOIRE (audit sécurité agent 2026-09-10) — même
        fuite inter-locataire que ``get_expenses``, sous forme agrégée.
        """
        current_session = self.session
        await self._assert_farm_owned_by(farm_id, producer_phone)
        stmt = (
            select(
                Expense.category,
                func.sum(Expense.amount).label("total"),
                func.count(Expense.id).label("count"),
            )
            .where(Expense.farm_id == farm_id)
            .group_by(Expense.category)
        )
        result = await current_session.execute(stmt)
        categories = {}
        grand_total = 0.0
        for row in result:
            categories[row.category] = {
                "total": float(row.total),
                "count": int(row.count),
            }
            grand_total += float(row.total)
        return {
            "status": "success",
            "data": {"categories": categories, "grand_total": grand_total},
        }
