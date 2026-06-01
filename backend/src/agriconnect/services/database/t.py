
import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta
from sqlalchemy import select

# Import de TON service réel et de tes modèles
from .d import AgriDatabaseService
from agriconnect.domain.models import User, BuyerProfile, Producer

# Configuration des logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("AgriConnect.RealAuctionTest")


async def run_real_database_test():
    logger.info("🔌 Connexion et initialisation du AgriDatabaseService réel...")
    # Initialisation de ton vrai service (qui doit déjà gérer sa session de DB)
    service = AgriDatabaseService()
    
    # ⚠️ REMPLACE PAR DES NUMÉROS EXISTANTS DANS TA BASE POUR LE TEST
    buyer_phone = "+22678143821"
    producer_phone = "+22601479800"
    
    print("\n" + "="*70)
    print("🛢️  DÉMARRAGE DU TEST D'INTÉGRATION RÉEL (SANS MOCK)")
    print("="*70)

    try:
        # -----------------------------------------------------------------
        # ÉTAPE ÉCHAUFFEMENT : Vérification des pré-requis en BDD
        # -----------------------------------------------------------------
        logger.info("🔎 Vérification de l'existence des profils en base...")
        
        buyer_check = await service.get_buyer_profile_by_phone(buyer_phone)
        if buyer_check["status"] == "error":
            print(f"❌ Erreur : L'acheteur avec le téléphone {buyer_phone} n'existe pas en BDD.")
            print("Veuillez changer la variable 'buyer_phone' avec un numéro valide.")
            return

        producer_check = await service.get_producer_profile_by_phone(producer_phone)
        if producer_check["status"] == "error":
            print(f"❌ Erreur : Le producteur avec le téléphone {producer_phone} n'existe pas en BDD.")
            print("Veuillez changer la variable 'producer_phone' avec un numéro valide.")
            return

        print("✅ Profils Acheteur et Producteur trouvés et validés.")

        # -----------------------------------------------------------------
        # TEST 1 : Utilitaires de Résolution
        # -----------------------------------------------------------------
        print("\n🔹 [TEST 1] Test de resolve_sub_category et get_active_auction_for_user...")
        # On cherche un produit générique qui doit exister dans ton catalogue (ex: "mais")
        sub_cat_id = await service.resolve_sub_category("mais")
        print(f"  ↳ resolve_sub_category : {'✅ OK' if sub_cat_id else '⚠️ Produit non trouvé'} (ID: {sub_cat_id})")

        # -----------------------------------------------------------------
        # TEST 2 : create_auction (Côté Acheteur)
        # -----------------------------------------------------------------
        print("\n🔹 [TEST 2] Création d'un VRAI appel d'offre (create_auction)...")
        future_deadline = datetime.now() + timedelta(days=7)
        
        create_res = await service.create_auction(
            phone=buyer_phone,
            product_query="mais", # Recherche floue
            qty=5.0,
            unit="TONNE",
            max_price=300.0,
            deadline=future_deadline,
            description="Test automatique d'intégration réelle",
            auto_extend=True
        )
        
        print(f"  ↳ Statut : {create_res['status'].upper()}")
        if create_res["status"] == "error":
            print(f"  ↳ Message d'erreur de la DB : {create_res.get('message')}")
            return
            
        auction_id = create_res.get("auction_id")
        print(f"  ↳ ID Enchère inséré en BDD : {auction_id}")
        print(f"  ↳ Menu WhatsApp généré :\n{create_res.get('summary')}")

        # -----------------------------------------------------------------
        # TEST 3 : get_auctions (Marketplace View pour le producteur)
        # -----------------------------------------------------------------
        print("\n🔹 [TEST 3] Test de get_auctions (Marketplace pour le producteur)...")
        # Le producteur regarde le marché (il ne doit pas voir ses propres offres s'il était acheteur)
        market_res = await service.get_auctions(phone=producer_phone, view_mode="MARKETPLACE")
        print(f"  ↳ Statut : {market_res['status'].upper()} | Total en cours : {market_res.get('count')}")
        print(f"  ↳ Menu WhatsApp reçu :\n{market_res.get('formatted_menu')}")

        # -----------------------------------------------------------------
        # TEST 4 : place_bid (Côté Producteur)
        # -----------------------------------------------------------------
        print("\n🔹 [TEST 4] Soumission d'une vraie offre (place_bid)...")
        bid_res = await service.place_bid(
            auction_id=auction_id,
            phone=producer_phone,
            offered_price=280.0,
            message="Proposition réelle via script de test."
        )
        print(f"  ↳ Statut : {bid_res['status'].upper()}")
        print(f"  ↳ Message retour : {bid_res.get('message')}")
        
        # -----------------------------------------------------------------
        # TEST 5 : get_auctions_bids (L'acheteur voit les offres sur son marché)
        # -----------------------------------------------------------------
        print("\n🔹 [TEST 5] Lecture des offres reçues par l'acheteur (get_auctions_bids)...")
        buyer_bids_res = await service.get_auctions_bids(phone=buyer_phone, status="OPEN")
        print(f"  ↳ Statut : {buyer_bids_res['status'].upper()} | Offres comptées : {buyer_bids_res.get('count')}")
        print(f"  ↳ Menu WhatsApp Acheteur :\n{buyer_bids_res.get('formatted_menu')}")

        # On récupère l'ID du bid qui a été généré via le mapping du menu WhatsApp pour le test suivant
        mapping = buyer_bids_res.get("mapping", {})
        bid_id = mapping.get("1") # Prend la première ligne du menu

        # -----------------------------------------------------------------
        # TEST 6 : get_my_active_bids (Le producteur suit ses offres envoyées)
        # -----------------------------------------------------------------
        print("\n🔹 [TEST 6] Suivi des offres par le producteur (get_my_active_bids)...")
        prod_bids_res = await service.get_my_active_bids(phone=producer_phone)
        print(f"  ↳ Statut : {prod_bids_res['status'].upper()}")
        print(f"  ↳ Menu WhatsApp Producteur :\n{prod_bids_res.get('formatted_menu')}")

        # -----------------------------------------------------------------
        # TEST 7 : select_winning_bid & Conversion Order (Dénouement)
        # -----------------------------------------------------------------
        if bid_id:
            print(f"\n🔹 [TEST 7] Clôture et Choix de l'offre gagnante (ID: {bid_id})...")
            final_res = await service.select_winning_bid(bid_id=bid_id)
            print(f"  ↳ Statut final de l'opération : {final_res['status'].upper()}")
            
            if final_res["status"] == "success":
                print(f"  ↳ Vraie commande (Order) créée avec l'ID : {final_res.get('order_id')}")
                print(f"\n📢 Résumé pour l'Acheteur :\n{final_res.get('summary_buyer')}")
                print(f"\n🎉 Résumé pour le Producteur :\n{final_res.get('summary_producer')}")
            else:
                print(f"  ↳ Échec de la clôture : {final_res.get('message')}")
        else:
            print("\n🔹 [TEST 7] Clôture impossible : Aucun ID de proposition (Bid) n'a pu être extrait du flux.")

        print("\n" + "="*70)
        print("🎉 FIN DU TEST EN CONDITIONS RÉELLES - ZÉRO ERREUR TECHNIQUE DÉTECTÉE")
        print("="*70 + "\n")

    except Exception as e:
        logger.error(f"💥 CRASH TECHNIQUE INTERCEPTÉ SUR LA VRAIE DB : {str(e)}", exc_info=True)



if __name__ == "__main__":
    # Exécution asynchrone sur ta base de données active
    asyncio.run(run_real_database_test())