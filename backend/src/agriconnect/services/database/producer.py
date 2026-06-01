import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Literal, Union
from decimal import Decimal

from sqlalchemy import select, update, delete, and_, desc, func
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.domain.models import (
    Producer, Farm, Stock, StockMovement, User, Product, 
    CropCycle, Order, Client, Expense, SurplusOffer, SubCategory, Category,Stock
)
from .base import BaseMixin
from .common import clean_text, positive_float, normalize_phone

logger = logging.getLogger("agriconnect.services.producer_mgmt")

MovementType = Literal['IN', 'OUT', 'WASTE']


class ProducerMgmtMixin(BaseMixin):
    """
    Mixin centralisé pour la gestion des exploitations, des catalogues de produits,
    des finances et des flux de stocks transactionnels via agent MCP.
    """
    # ✅ LA SIGNATURE CORRIGÉE : Plus de paramètre "session" dans les parenthèses
    async def guess_category(self, product_name: str) -> str:
        """
        Analyse le nom d'un produit et interroge la base de données 
        via self.session pour trouver la catégorie parente correspondante.
        """
        if not product_name:
            return "AUTRES"

        name_clean = product_name.strip()

        try:
            # 1. Résolution dynamique via les sous-catégories existantes en BD
            stmt = (
                select(Category.name)
                .join(SubCategory, SubCategory.category_id == Category.id)
                .where(SubCategory.name.ilike(f"%{name_clean}%"))
                .limit(1)
            )
            category_name = await self.session.scalar(stmt)

            if category_name:
                return category_name.upper()

            # 2. Deuxième chance : Correspondance directe avec une catégorie principale
            stmt_alt = select(Category.name).limit(100)
            categories_res = await self.session.scalars(stmt_alt)
            all_categories = categories_res.all()

            for cat in all_categories:
                if cat.upper() in name_clean.upper():
                    return cat.upper()

        except Exception as e:
            logger.error(f"⚠️ Erreur lors de la résolution de la catégorie pour '{product_name}': {e}")

        # 3. Fallback de sécurité
        MAPPING_DE_SECOURS = {
            "CÉRÉALES": ["MAÏS", "RIZ", "MIL", "SORGHO", "FONIO"],
            "LÉGUMES": ["TOMATE", "OIGNON", "PIMENT", "CHOU", "GOMBO", "CAROTTE"],
            "FRUITS": ["MANGUE", "ORANGE", "CITRON", "BANANE", "PAPAYE"],
            "ANIMAUX": ["POULET", "BOEUF", "MOUTON", "CHÈVRE", "OEUF"]
        }

        for categorie, mots_cles in MAPPING_DE_SECOURS.items():
            if any(mot in name_clean.upper() for mot in mots_cles):
                return categorie

        return "AUTRES"

    # ─── SECTION 1 : GESTION DES FERMES (FARMS) ───────────────────────────
    async def get_or_create_farm(self, phone: str, farm_name: str, zone_id: str = None) -> Dict[str, Any]:
        """
        Récupère la ferme existante d'un producteur ou en crée une nouvelle par défaut via self.session.
        """
        # 1. On récupère proprement l'utilisateur via le service (gère le format dict/objet)
        user = await self.get_user_by_phone(phone)
        user_id = user["id"] if isinstance(user, dict) else user.id
        user_name = user.get("name") if isinstance(user, dict) else getattr(user, "name", None)

        # 2. On vérifie et crée le Producer si manquant par rapport à l'unicité de user_id
        producer_stmt = select(Producer).where(Producer.user_id == (user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))))
        producer = (await self.session.execute(producer_stmt)).scalar_one_or_none()
        
        if not producer:
            logger.info("ℹ️ Profil producteur manquant pour l'utilisateur %s, création automatique...", user_id)
            producer = Producer(
                id=user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id)),
                user_id=user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id)),
                business_name=user_name or "Mon Agrobusiness"
            )
            self.session.add(producer)
            await self.session.flush()

        # 3. Maintenant on cherche la ferme de manière sécurisée
        stmt = select(Farm).where(Farm.producer_id == producer.id)
        # 💡 CORRECTION : .scalars().first() au lieu de .scalar_one_or_none()
        farm = (await self.session.execute(stmt)).scalars().first()
        
        if farm:
            logger.info("ℹ️ Ferme existante trouvée pour le producteur %s", phone)
            return farm.to_dict()

        # Sinon, création de la ferme s'il n'en a aucune
        farm = Farm(
            id=uuid.uuid4(), 
            name=farm_name, 
            producer_id=producer.id, 
            zone_id=uuid.UUID(str(zone_id)) if zone_id else None
        )
        self.session.add(farm)
        await self.session.flush()
        await self.session.refresh(farm)
        
        logger.info("✅ Ferme créée automatiquement : %s pour le numéro %s", farm_name, phone)
        return farm.to_dict()


    async def create_farm(self, phone: str, name: str, location: str = None, size: float = None, soil_type: str = None, water_source: str = None, zone_id: str = None) -> Dict[str, Any]:
        """
        Crée explicitement une ferme via self.session en s'appuyant sur le profil existant.
        """
        phone = clean_text(phone, "phone", required=True)
        name = clean_text(name, "name", required=True)

        # 🚀 UTILISATION DE BASE.PY : On récupère l'objet Producer d'origine (pas un dict !)
        try:
            _, producer = await self.get_producer_profile(phone=phone)
        except ValueError:
            # Si le profil producteur n'existe pas encore, on le génère à la volée
            user = await self.get_user_by_phone(phone)
            user_id = user["id"] if isinstance(user, dict) else user.id
            user_name = user.get("name") if isinstance(user, dict) else getattr(user, "name", None)
            
            producer = Producer(
                id=user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id)),
                user_id=user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id)),
                business_name=user_name or "Mon Agrobusiness"
            )
            self.session.add(producer)
            await self.session.flush()

        new_farm = Farm(
            id=uuid.uuid4(),
            producer_id=producer.id,  # Garanti d'être un attribut d'objet valide
            name=name,
            location=location,
            size=size,
            soil_type=soil_type,
            water_source=water_source,
            zone_id=uuid.UUID(str(zone_id)) if zone_id else None
        )
        self.session.add(new_farm)
        await self.session.flush()
        await self.session.refresh(new_farm)
        return new_farm.to_dict()

    async def update_farm(self, phone: str, **kwargs) -> Optional[Dict[str, Any]]:
        """
        Met à jour dynamiquement les attributs de la ferme via self.session.
        """
        try:
            farm = await self.get_producer_farm(phone)

            for key, value in kwargs.items():
                if key not in ("id", "producer_id") and hasattr(farm, key):
                    if key == "zone_id" and value:
                        setattr(farm, key, uuid.UUID(str(value)))
                    else:
                        setattr(farm, key, value)
                else:
                    logger.debug(f"Attribut ignoré ou protégé : {key}")

            await self.session.flush()
            await self.session.refresh(farm)
            return farm.to_dict()

        except ValueError as e:
            logger.warning(f"Échec de mise à jour de la ferme pour {phone} : {e}")
            return None
        except Exception as e:
            logger.error(f"Erreur critique lors de l'update de la ferme ({phone}) : {e}")
            raise

    # ─── SECTION 2 : GESTION DU CATALOGUE PRODUITS ───────────────────────
    async def create_product(
            self, 
            phone: str, 
            name: str, 
            price: float, 
            quantity_for_sale: float, 
            unit: str = "KG", 
            category_label: str = None, 
            sub_category_id: str = None, 
            description: str = None, 
            local_names: dict = None
        ) -> Dict[str, Any]:
            """
            Ajoute un produit au catalogue public de vente du producteur via self.session.
            """
            phone = clean_text(phone, "phone", required=True)
            name = clean_text(name, "name", required=True)
            price = positive_float(price, "price", allow_zero=True)
            quantity_for_sale = positive_float(quantity_for_sale, "quantity_for_sale", allow_zero=True)
            unit = clean_text(unit, "unit", required=False, max_length=20) or "KG"

            try:
                profile_res = await self.get_producer_profile(phone)
                
                if not profile_res or profile_res[0] is None:
                    logger.warning(f"⚠️ Échec create_product : Le numéro {phone} n'a pas de profil producteur.")
                    return {
                        "status": "error",
                        "message": f"Création impossible : Le numéro {phone} n'est rattaché à aucun compte producteur actif."
                    }
                
                user, producer = profile_res
                
                if not producer or not producer.id:
                    return {'status': 'error', 'message': "Profil producteur invalide ou corrompu. Pas d'id disponible."}
                    
                product_id = str(uuid.uuid4())
                short_code = product_id[:8].upper()
                
                if not sub_category_id:
                    sub_cat_stmt = select(SubCategory.id).where(SubCategory.name.ilike(f"%{name.strip()}%")).limit(1)
                    sub_category_id = await self.session.scalar(sub_cat_stmt)
                
                category_label = (
                    clean_text(category_label, "category_label", required=False) 
                    or await self.guess_category(product_name=name)
                )
                
                product = Product(
                    id=uuid.UUID(product_id), 
                    short_code=short_code, 
                    name=name.strip(), 
                    price=float(price), 
                    unit=unit.upper().strip(), 
                    quantity_for_sale=float(quantity_for_sale), 
                    producer_id=producer.id,
                    category_label=category_label, 
                    sub_category_id=uuid.UUID(str(sub_category_id)) if sub_category_id else None, 
                    description=description.strip() if description else None, 
                    local_names=local_names,
                    created_at=datetime.now(),
                    updated_at=datetime.now()
                )
                
                self.session.add(product)
                await self.session.flush()

                return {
                    "status": "success",
                    "product_id": product_id,
                    "short_code": short_code,
                    "message": f"🎉 Le produit *{name}* a été ajouté avec succès à votre catalogue de vente !",
                    "data": {
                        "product_id": product_id, 
                        "short_code": short_code, 
                        "name": name, 
                        "price_fcfa": price, 
                        "quantity": quantity_for_sale, 
                        "unit": unit,
                        "category_label": category_label
                    }
                }
                    
            except Exception as e:
                logger.error(f"❌ Erreur système dans create_product : {str(e)}")
                return {"status": "error", "message": f"Erreur technique lors du stockage du produit : {str(e)}"}

    async def list_products(self, phone: str) -> Dict[str, Any]:
        """
        Vision Phone-First : Liste tous les produits via self.session.
        """
        try:
            clean_phone = normalize_phone(phone)
            profile_res = await self.get_producer_profile(clean_phone)
            if not profile_res or profile_res[0] is None:
                return {"status": "error", "message": "Votre compte producteur n'est pas identifié."}
                
            user, producer = profile_res

            stmt = (
                select(Product)
                .where(Product.producer_id == producer.id)
                .order_by(desc(Product.created_at))
            )
            result = await self.session.execute(stmt)
            products = result.scalars().all()

            if not products:
                return {
                    "status": "success",
                    "count": 0,
                    "message": "📦 Votre catalogue est actuellement vide. Pour ajouter un produit, écrivez par exemple : 'Je veux vendre 50 kg de riz à 1200 CFA le kg'."
                }

            products_list = []
            menu_lines = ["📦 *Votre catalogue de produits en vente :*"]
            mapping_cache = {}

            for i, p in enumerate(products, start=1):
                price_val = float(p.price) if isinstance(p.price, Decimal) else p.price
                products_list.append({
                    "product_id": str(p.id),
                    "short_code": p.short_code,
                    "name": p.name,
                    "price": price_val,
                    "quantity": p.quantity_for_sale,
                    "unit": p.unit
                })

                line = (
                    f"\n*{i}. {p.name}* (Réf: #{p.short_code})\n"
                    f"💰 Prix : *{price_val} CFA/{p.unit}*\n"
                    f"⚖️ Stock dispo : {p.quantity_for_sale} {p.unit}"
                )
                menu_lines.append(line)
                mapping_cache[str(i)] = str(p.id)

            menu_lines.append("\n_Pour modifier un prix ou une quantité, mentionnez simplement le nom du produit ou son numéro._")

            return {
                "status": "success",
                "count": len(products_list),
                "formatted_menu": "\n".join(menu_lines),
                "mapping": mapping_cache,
                "data": products_list
            }

        except Exception as e:
            logger.error(f"❌ Erreur lors du listing des produits : {str(e)}")
            return {"status": "error", "message": "Impossible d'accéder à votre catalogue pour le moment."}

    # ─── SECTION 3 : LOGIQUE DU STOCK TRANSACTIONNEL ──────────────────────

    async def add_stock(self, phone: str, item_name: str, quantity: float, unit: str = "KG", stock_type: str = "HARVEST", reason: str = "Ajout via agent", warehouse_id: str = None, organization_id: str = None) -> Dict[str, Any]:
        """
        Incrémente ou crée une ligne de stock pour l'exploitation via self.session.
        """
        phone = clean_text(phone, "phone", required=True)
        item_name = clean_text(item_name, "item_name", required=True)
        quantity = positive_float(quantity, "quantity")
        unit = clean_text(unit, "unit", required=False, max_length=20) or "KG"

        try:
            farm = await self.get_producer_farm(phone)
            farm_id = str(farm.id)

            stmt = select(Stock).where(Stock.farm_id == farm_id, func.lower(Stock.item_name) == item_name.lower()).with_for_update()
            result = await self.session.execute(stmt)
            stock = result.scalar_one_or_none()

            if stock:
                stock.quantity += quantity
                stock_id = str(stock.id)
                new_total = stock.quantity
            else:
                stock_id = str(uuid.uuid4())
                stock = Stock(
                    id=uuid.UUID(stock_id), 
                    farm_id=uuid.UUID(farm_id), 
                    item_name=item_name, 
                    quantity=quantity, 
                    unit=unit, 
                    type=stock_type, 
                    warehouse_id=uuid.UUID(warehouse_id) if warehouse_id else None, 
                    organization_id=uuid.UUID(organization_id) if organization_id else None
                )
                self.session.add(stock)
                new_total = quantity

            mvt = StockMovement(id=uuid.uuid4(), stock_id=uuid.UUID(stock_id), type="IN", quantity=quantity, reason=reason)
            self.session.add(mvt)
            await self.session.flush()

            return {"stock_id": stock_id, "item_name": item_name, "added": quantity, "new_total": new_total, "unit": unit}
        except ValueError as e:
            return {"error": str(e)}

    async def remove_stock(self, phone: str, item_name: str, quantity: float, reason: str = "Retrait", movement_type: str = "OUT") -> Dict[str, Any]:
        """
        Décrémente le stock disponible après validation du solde via self.session.
        """
        phone = clean_text(phone, "phone", required=True)
        item_name = clean_text(item_name, "item_name", required=True)
        quantity = positive_float(quantity, "quantity")

        try:
            farm = await self.get_producer_farm(phone)

            stmt = select(Stock).where(Stock.farm_id == str(farm.id), func.lower(Stock.item_name) == item_name.lower()).with_for_update()
            result = await self.session.execute(stmt)
            stock = result.scalar_one_or_none()

            if not stock:
                return {"error": f"Aucun stock de '{item_name}' trouvé pour cette exploitation."}
            if stock.quantity < quantity:
                return {"error": f"Stock insuffisant : {stock.quantity} {stock.unit} disponibles."}

            stock.quantity -= quantity

            mvt = StockMovement(id=uuid.uuid4(), stock_id=stock.id, type=movement_type, quantity=quantity, reason=reason)
            self.session.add(mvt)
            await self.session.flush()

            return {"stock_id": str(stock.id), "item_name": item_name, "removed": quantity, "remaining": stock.quantity, "unit": stock.unit}
        except ValueError as e:
            return {"error": str(e)}

    async def adjust_stock(self, phone: str, item_name: str, quantity_change: float, reason: str = "Adjustment via MCP", unit: str = "KG", stock_type: str = "HARVEST", warehouse_id: str = None, organization_id: str = None) -> Dict[str, Any]:
        """
        Ajuste le stock de manière transparente (positif ou négatif).
        """
        if quantity_change >= 0:
            return await self.add_stock(phone=phone, item_name=item_name, quantity=quantity_change, unit=unit, stock_type=stock_type, reason=reason, warehouse_id=warehouse_id, organization_id=organization_id)
        else:
            return await self.remove_stock(phone=phone, item_name=item_name, quantity=abs(quantity_change), reason=reason)



    async def get_stocks(self, phone: str, *args, **kwargs) -> Dict[str, Any]:
        """Récupère l'intégralité des stocks d'un producteur via son numéro de téléphone.
        
        Structure le retour ferme par ferme en évitant les crashs de Lazy Loading.
        """
        try:
            clean_phone = str(phone).strip()
            logger.debug(f"[get_stocks] Extraction des stocks pour le téléphone : {clean_phone}")

            # 🚀 1. REQUÊTE AVEC JOINTURE ET PRÉ-CHARGEMENT (joinedload)
            # On dit explicitement à SQLAlchemy de charger aussi les relations enfants 'stocks'
            # pour éviter qu'il essaie de re-contacter la base de données dans la boucle 'for'.
            farm_stmt = (
                select(Farm)
                .join(Farm.producer)
                .join(Producer.user)
                .options(joinedload(Farm.stocks))  # 🧠 Charge les stocks de la ferme immédiatement !
                .where(User.phone == clean_phone)
            )

            farm_result = await self.session.execute(farm_stmt)
            # unique() est obligatoire ici car joinedload crée des doublons de lignes dans le résultat brut SQL
            farms = farm_result.scalars().unique().all()

            if not farms:
                return {
                    "status": "success",
                    "message": f"Aucune exploitation trouvée pour le numéro {clean_phone}.",
                    "data": {}
                }

            # 2. Regroupement des stocks ferme par ferme (ZÉRO requêtes SQL ici, tout est en mémoire cache !)
            stocks_farm_by_farm = {}

            for farm in farms:
                farm_id_str = str(farm.id)
                
                # Plus besoin de faire un select(Stock) ici ! On lit directement la relation pré-chargée
                # On trie simplement par nom d'article en Python pour respecter ton 'order_by'
                sorted_stocks = sorted(farm.stocks, key=lambda s: s.item_name if s.item_name else "")

                farm_stocks_list = [
                    {
                        "stock_id": str(s.id) if hasattr(s, "id") else str(s.stock_id),
                        "item_name": str(s.item_name),
                        "quantity": float(s.quantity) if s.quantity else 0.0
                    }
                    for s in sorted_stocks
                ]

                stocks_farm_by_farm[farm_id_str] = {
                    "farm_name": str(farm.name) if farm.name else "Ferme sans nom",
                    "location": str(farm.location) if farm.location else "Non spécifiée",
                    "size": float(farm.size) if farm.size else 0.0,
                    "stocks": farm_stocks_list
                }

            return {
                "status": "success",
                "message": f"Stocks de {len(farms)} ferme(s) récupérés avec succès.",
                "data": stocks_farm_by_farm
            }

        except Exception as e:
            logger.error(f"❌ Erreur SQL de jointure dans get_stocks : {str(e)}", exc_info=True)
            raise e

    async def add_stock_movement(
        self, 
        phone: str, 
        stock_id: any, 
        mtype: str, 
        quantity: float, 
        reason: str = None
    ) -> any:
        """
        Enregistre un mouvement de stock granulaire (IN/OUT) avec vérification 
        stricte des permissions du producteur.
        """
        from sqlalchemy.orm import joinedload
        from agriconnect.domain.models import Stock, Farm, Producer, User, StockMovement

        # 1. Préparation de la requête avec verrou ciblé
        stmt = (
            select(Stock)
            .where(Stock.id == stock_id)
            .options(
                joinedload(Stock.farm)
                .joinedload(Farm.producer)
                .joinedload(Producer.user)
            )
            .with_for_update(of=Stock)
        )
        
        res = await self.session.execute(stmt)
        
        # 2. Extraction immédiate de l'objet unique
        stock_obj = res.unique().scalar_one_or_none()
        
        # 3. Barrière de sécurité et validation des droits d'accès
        if not stock_obj:
            raise ValueError("Stock introuvable.")
            
        if not stock_obj.farm or not stock_obj.farm.producer or not stock_obj.farm.producer.user:
            raise PermissionError("Structure de propriété du stock incomplète en base de données.")
            
        if stock_obj.farm.producer.user.phone != phone:
            raise PermissionError("Accès refusé au stock ciblé.")
            
        # 4. Application de la logique métier (Incrémentation ou Décrémentation)
        if mtype == "IN":
            stock_obj.quantity += quantity
        elif mtype == "OUT":
            if stock_obj.quantity < quantity:
                raise ValueError(f"Stock insuffisant. Disponible : {stock_obj.quantity} {stock_obj.unit}")
            stock_obj.quantity -= quantity
        else:
            raise ValueError("Type de mouvement invalide. Utilisez 'IN' ou 'OUT'.")
            
        # 5. Création de la ligne d'historique (Parfaitement alignée avec ton modèle BDD)
        movement = StockMovement(
            stock_id=stock_obj.id,
            type=mtype,
            quantity=quantity,
            reason=reason
        )
        
        self.session.add(movement)
        await self.session.flush() # Enregistre sans clore la transaction globale
        
        # À la fin de add_stock_movement :
        return {
            "status": "success",
            "stock_id": stock_obj.id,
            "item_name": stock_obj.item_name,
            # 🚀 On calcule l'ancienne quantité mathématiquement pour faire plaisir au test
            "old_quantity": stock_obj.quantity - quantity if mtype == "IN" else stock_obj.quantity + quantity,
            "new_quantity": stock_obj.quantity,
            "unit": stock_obj.unit
        }

    async def get_stock_movements(self, stock_id: any, limit: int = 5) -> list:
        """
        Récupère l'historique des mouvements (IN/OUT) pour un stock donné,
        trié du plus récent au plus ancien.
        """
        from agriconnect.domain.models import StockMovement
        
        stmt = (
            select(StockMovement)
            .where(StockMovement.stock_id == stock_id)
            .order_by(StockMovement.created_at.desc())
            .limit(limit)
        )
        
        res = await self.session.execute(stmt)
        movements = res.scalars().all()
        
        return [
            {
                "id": m.id,
                "type": m.type,
                "quantity": m.quantity,
                "reason": m.reason,
                "created_at": m.created_at.isoformat() if m.created_at else None
            }
            for m in movements
        ]

    async def delete_stock(self, phone: str, stock_id: any) -> bool:
        from sqlalchemy.orm import joinedload
        from agriconnect.domain.models import Stock, Farm, Producer, User

        stmt = (
            select(Stock)
            .where(Stock.id == stock_id)
            .options(joinedload(Stock.farm).joinedload(Farm.producer).joinedload(Producer.user))
        )
        res = await self.session.execute(stmt)
        stock_obj = res.unique().scalar_one_or_none() # 🚀 On extrait TOUT DE SUITE

        if not stock_obj or not stock_obj.farm or not stock_obj.farm.producer or not stock_obj.farm.producer.user:
            raise ValueError("Stock introuvable ou droits insuffisants.")

        if stock_obj.farm.producer.user.phone != phone:
            raise ValueError("Stock introuvable ou droits insuffisants.")

        await self.session.delete(stock_obj)
        await self.session.flush()
        return True

    # ─── SECTION 4 : SUIVI COMMERCIAL & CRM (CLIENTS / ORDERS) ───────────

    async def update_order_status(self, order_id: str, new_status: str, payment_status: str = None) -> Optional[Dict[str, Any]]:
        """
        Met à jour de manière sécurisée le statut d'une commande via self.session.
        """
        stmt = select(Order).where(Order.id == uuid.UUID(order_id)).with_for_update()
        result = await self.session.execute(stmt)
        order = result.scalar_one_or_none()
        
        if not order:
            logger.warning(f"Commande introuvable : {order_id}")
            return None
            
        order.status = new_status
        if payment_status:
            order.payment_status = payment_status
            
        await self.session.flush()
        await self.session.refresh(order)
        return order.to_dict()

    async def get_or_create_client(self, phone: str, name: str, client_phone: str, email: str = None, location: str = None) -> Dict[str, Any]:
        """
        Identifie ou ajoute un client via self.session.
        """
        name = clean_text(name, "name", required=True)
        client_phone = clean_text(client_phone, "client_phone", required=True, max_length=40)
        
        try:
            user, _ = await self.get_producer_profile(phone)
            
            stmt = select(Client).where(Client.producer_id == user.id, Client.phone == client_phone)
            result = await self.session.execute(stmt)
            client = result.scalar_one_or_none()
            
            if client:
                return client.to_dict()

            client = Client(id=uuid.uuid4(), name=name, phone=client_phone, email=email, location=location, producer_id=user.id)
            self.session.add(client)
            await self.session.flush()
            await self.session.refresh(client)
            return client.to_dict()
        except ValueError as e:
            return {"error": str(e)}

    async def get_clients(self, phone: str) -> Union[List[Dict[str, Any]], Dict[str, str]]:
        """
        Liste les clients via self.session.
        """
        try:
            user, _ = await self.get_producer_profile(phone)
            stmt = select(Client).where(Client.producer_id == user.id).order_by(desc(Client.total_spent))
            result = await self.session.execute(stmt)
            return [c.to_dict() for c in result.scalars()]
        except ValueError as e:
            return {"error": str(e)}

    # ─── SECTION 5 : FINANCE & SUIVI AGRONOMIQUE (EXPENSES / CROPS / SURPLUS) ───
    async def add_expense(
        self, 
        phone: str, 
        label: str, 
        amount: float, 
        category: str, 
        date: datetime = None
    ) -> dict:
        """
        Enregistre une nouvelle dépense/charge financière pour la ferme du producteur.
        Filtre proprement via des jointures SQL et cible la première ferme disponible.
        """
        import uuid
        from datetime import datetime
        from sqlalchemy import select
        from sqlalchemy.orm import joinedload
        from agriconnect.domain.models import Farm, Producer, User, Expense

        # 1. Récupération de la ferme avec des jointures explicites pour éviter le produit cartésien
        stmt = (
            select(Farm)
            .join(Farm.producer)  # Jointure explicite Farm -> Producer
            .join(Producer.user)  # Jointure explicite Producer -> User
            .where(User.phone == phone)
            .options(
                joinedload(Farm.producer)
                .joinedload(Producer.user)
            )
        )
        
        res = await self.session.execute(stmt)
        # 🚀 On utilise .first() au lieu de .scalar_one_or_none() 
        # pour éviter de planter si le producteur possède plusieurs fermes en BDD
        farm_obj = res.scalars().first()

        if not farm_obj:
            raise ValueError("Ferme introuvable ou droits insuffisants pour ce producteur.")

        # 2. Sécurisation et nettoyage du format de la date pour la BDD (naive timestamp)
        if date is None:
            insert_date = datetime.now()
        else:
            insert_date = date.replace(tzinfo=None) if date.tzinfo else date

        # 3. Instanciation du modèle d'enregistrement de la dépense
        expense = Expense(
            id=uuid.uuid4(),
            farm_id=farm_obj.id,
            label=label,
            amount=amount,
            category=category,
            date=insert_date
        )

        # 4. Sauvegarde persistante en base de données
        self.session.add(expense)
        await self.session.flush()

        return {
            "status": "success",
            "id": expense.id,
            "farm_id": expense.farm_id,
            "label": expense.label,
            "amount": expense.amount,
            "category": expense.category,
            "date": expense.date.isoformat() if expense.date else None
        }

    async def get_expenses(self, phone: str, category: str = None, limit: int = 50) -> Union[List[Dict[str, Any]], Dict[str, str]]:
        """
        Historique comptable linéaire des dépenses via self.session.
        """
        try:
            farm = await self.get_producer_farm(phone)
            stmt = select(Expense).where(Expense.farm_id == farm.id)
            if category:
                stmt = stmt.where(Expense.category == category)
                
            stmt = stmt.order_by(desc(Expense.date)).limit(limit)
            result = await self.session.execute(stmt)
            return [e.to_dict() for e in result.scalars()]
        except ValueError as e:
            return {"error": str(e)}

    async def get_expense_summary(self, phone: str) -> Dict[str, Any]:
        """
        Génère une synthèse groupée sécurisée via self.session.
        """
        try:
            farm = await self.get_producer_farm(phone)
            stmt = select(
                Expense.category, 
                func.sum(Expense.amount).label("total"), 
                func.count(Expense.id).label("count")
            ).where(Expense.farm_id == farm.id).group_by(Expense.category)
            
            result = await self.session.execute(stmt)
            
            categories = {}
            grand_total = 0.0
            
            for row in result:
                row_dict = dict(row._mapping)
                total_val = float(row_dict["total"]) if isinstance(row_dict["total"], Decimal) else row_dict["total"]
                
                categories[row_dict["category"]] = {
                    "total": total_val, 
                    "count": int(row_dict["count"])
                }
                grand_total += total_val
                
            return {"categories": categories, "grand_total": grand_total}
        except ValueError as e:
            return {"error": str(e)}

    async def get_crop_cycles(self, phone: str) -> Union[List[Dict[str, Any]], Dict[str, str]]:
        """
        Consulte les cycles agronomiques via self.session.
        """
        try:
            farm = await self.get_producer_farm(phone)
            stmt = select(CropCycle).where(CropCycle.farm_id == farm.id)
            result = await self.session.execute(stmt)
            return [c.to_dict() for c in result.scalars()]
        except ValueError as e:
            return {"error": str(e)}
