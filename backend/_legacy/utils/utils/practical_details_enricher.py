"""
Practical Details Enricher - Enrichisseur de Détails Pratiques
================================================================

Module pour transformer des conseils vagues en instructions ACTIONNABLES.

Principe: "Ne JAMAIS dire 'Faites X' sans expliquer COMMENT, QUAND, POURQUOI"
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("PracticalDetailsEnricher")


class PracticalDetailsEnricher:
    """
    Enrichit les conseils agricoles avec détails pratiques.
    
    AVANT: "Pailler le sol"
    APRÈS: "Pailler le sol avec couche de 10cm de paille/feuilles mortes.
            Appliquer tôt le matin. Économise 40% d'eau."
    """
    
    def __init__(self):
        self.practical_guidelines = self._build_practical_guidelines()

    def _build_practical_guidelines(self) -> Dict[str, Dict]:
        """
        Base de données des détails pratiques par action agricole.
        
        Structure: {
            "action": {
                "comment": "Instructions détaillées",
                "quand": "Moment optimal",
                "pourquoi": "Bénéfices/raisons",
                "quantite": "Dosages/mesures",
                "cout": "Coût estimé",
                "alternatives": ["alternative1", "alternative2"]
            }
        }
        """
        return {
            # IRRIGATION
            "arroser": {
                "comment": (
                    "1. Arrosez au pied des plants, PAS sur les feuilles\n"
                    "2. Utilisez arrosoir ou goutte-à-goutte\n"
                    "3. Sol doit être humide à 10cm profondeur (testez avec doigt)"
                ),
                "quand": "Tôt matin (5h-7h) OU soir (18h-20h) - JAMAIS plein soleil",
                "pourquoi": "Évaporation minimale + absorption maximale + pas de brûlure feuilles",
                "quantite": "10-15 litres/plant/jour si sécheresse, 5L si normal",
                "cout": "Gratuit si puits/forage. Motopompe: ~500 FCFA/heure",
                "alternatives": [
                    "Paillage pour réduire besoin",
                    "Cuvette autour plant pour retenir eau",
                    "Arrosage groupé avec voisins (partager motopompe)"
                ]
            },
            
            "arroser de nuit": {
                "comment": (
                    "1. Arroser entre 22h-5h si possible\n"
                    "2. Utilisez système goutte-à-goutte avec réservoir\n"
                    "3. OU préparer cuvettes et arroser avant coucher"
                ),
                "quand": "Entre 22h et 5h du matin (idéal: 2h-4h)",
                "pourquoi": "ZÉRO évaporation + eau pénètre profondément + rosée naturelle",
                "quantite": "Même quantité que jour mais plus efficace",
                "cout": "Système goutte-à-goutte DIY: 5,000 FCFA/parcelle",
                "alternatives": [
                    "Arroser très tôt matin (5h-6h)",
                    "Mulching pour retenir humidité nocturne"
                ]
            },
            
            # PAILLAGE
            "pailler": {
                "comment": (
                    "1. Étaler couche uniforme de 8-10cm autour plants\n"
                    "2. Laisser 5cm libre autour tige (éviter pourriture)\n"
                    "3. Renouveler si paille se décompose"
                ),
                "quand": "Après semis + 2 semaines OU après sarclage",
                "pourquoi": "Économise 30-40% eau + bloque mauvaises herbes + enrichit sol",
                "quantite": "1 boule paille (~20kg) pour 50m² / 400kg pour 1 hectare",
                "cout": "Gratuit si résidus culture. Achat: 200-500 FCFA/boule",
                "alternatives": [
                    "Feuilles mortes (gratuites)",
                    "Résidus de récolte (tiges mil/sorgho)",
                    "Herbes séchées (après désherbage)",
                    "Coques arachide"
                ]
            },
            
            "pailler le sol": {  # Même que "pailler"
                "comment": (
                    "1. Étaler couche uniforme de 8-10cm autour plants\n"
                    "2. Laisser 5cm libre autour tige (éviter pourriture)\n"
                    "3. Renouveler si paille se décompose"
                ),
                "quand": "Après semis + 2 semaines OU après sarclage",
                "pourquoi": "Économise 30-40% eau + bloque mauvaises herbes + enrichit sol",
                "quantite": "1 boule paille (~20kg) pour 50m² / 400kg pour 1 hectare",
                "cout": "Gratuit si résidus culture. Achat: 200-500 FCFA/boule",
                "alternatives": [
                    "Feuilles mortes (gratuites)",
                    "Résidus de récolte (tiges mil/sorgho)",
                    "Herbes séchées (après désherbage)",
                    "Coques arachide"
                ]
            },
            
            # TRAITEMENT RAVAGEURS
            "pulvériser": {
                "comment": (
                    "1. Préparer solution dans seau propre\n"
                    "2. Remplir pulvérisateur (bien nettoyer avant)\n"
                    "3. Pulvériser SOUS les feuilles (là où ravageurs cachent)\n"
                    "4. Mouiller toute la plante mais sans dégouliner"
                ),
                "quand": "Tôt matin (6h-8h) OU fin après-midi (17h-19h) - PAS soleil direct",
                "pourquoi": "Produit efficace + pas de brûlure + temps de séchage optimal",
                "quantite": "50-100ml produit/litre d'eau (selon produit - LIRE ÉTIQUETTE)",
                "cout": "Pulvérisateur: 2,500-5,000 FCFA. Location: 500 FCFA/jour",
                "alternatives": [
                    "Arrosoir avec pomme (si pas de pulvérisateur)",
                    "Balai de branchages trempé dans solution",
                    "Traitement localisé avec chiffon imbibé"
                ]
            },
            
            "eau savonneuse": {
                "comment": (
                    "1. Dissoudre 300g savon noir dans 10L eau tiède\n"
                    "2. Bien mélanger jusqu'à mousse légère\n"
                    "3. Filtrer avec tissu si morceaux\n"
                    "4. Utiliser dans 24h (ne se conserve pas)"
                ),
                "quand": "Tôt matin - Renouveler tous les 3 jours si nécessaire",
                "pourquoi": "Étouffe insectes + sans danger + ultra bon marché",
                "quantite": "30g savon/litre eau (=300g/10L) / 1.5kg savon pour 1 hectare",
                "cout": "500-800 FCFA/kg savon noir au marché = 750 FCFA/ha",
                "alternatives": [
                    "Savon lessive (même dosage mais moins efficace)",
                    "Cendres de bois + eau (insecticide naturel)",
                    "Huile neem 30ml/L (plus puissant, 2,500 FCFA/L)"
                ]
            },
            
            # FERTILISATION
            "fertiliser": {
                "comment": (
                    "1. Faire poquet/cuvette autour plant (10cm rayon)\n"
                    "2. Épandre engrais dans poquet\n"
                    "3. Recouvrir légèrement de terre\n"
                    "4. Arroser IMMÉDIATEMENT après (activation)"
                ),
                "quand": "Matin avant grande chaleur + sol légèrement humide",
                "pourquoi": "Nutriments disponibles rapidement + pas de brûlure racines",
                "quantite": "NPK 15-15-15: 150-200kg/ha / Urée: 50-100kg/ha",
                "cout": "NPK: 18,000 FCFA/50kg / Urée: 15,000 FCFA/50kg",
                "alternatives": [
                    "Compost maison: 2-3 tonnes/ha (gratuit)",
                    "Fumier animal: 5-10 tonnes/ha (5,000 FCFA/tonne)",
                    "Purin feuilles (gratuit, recette donnée)"
                ]
            },
            
            # DÉSHERBAGE
            "désherber": {
                "comment": (
                    "1. Arracher mauvaises herbes À LA MAIN (racines incluses)\n"
                    "2. OU sarcler à 2-3cm profondeur avec daba\n"
                    "3. Laisser herbes sécher 2 jours puis utiliser comme paillage"
                ),
                "quand": "Après pluie quand sol meuble + herbes jeunes (< 10cm)",
                "pourquoi": "Herbes volent eau+nutriments / Sarclage aère le sol",
                "quantite": "2-3 passages/saison: 20 jours, 40 jours, 60 jours après semis",
                "cout": "Main-d'œuvre: 10,000-15,000 FCFA/ha/passage",
                "alternatives": [
                    "Paillage épais (prévention)",
                    "Herbicide naturel: vinaigre + sel",
                    "Association cultures (ombrage réduit herbes)"
                ]
            },
            
            # PROTECTION RAVAGEURS
            "surveiller": {
                "comment": (
                    "1. Inspecter plants TOUS LES 3 JOURS minimum\n"
                    "2. Vérifier DESSOUS feuilles (œufs, larves cachés là)\n"
                    "3. Compter ravageurs: <5/plant=OK, 5-10=Attention, >10=Traiter\n"
                    "4. Prendre photo si doute et envoyer au 55555"
                ),
                "quand": "Tôt matin (ravageurs moins actifs) + après chaque pluie",
                "pourquoi": "Détection précoce = traitement facile + moins coûteux",
                "quantite": "2-3 visites/semaine minimum pendant saison",
                "cout": "Temps seulement (15min/parcelle)",
                "alternatives": [
                    "Pièges jaunes adhésifs (1,000 FCFA/10 pièges)",
                    "Groupe surveillance avec voisins (rotation)",
                    "Photos téléphone + analyse IA gratuite"
                ]
            },
        }

    def enrich_advice(self, advice_text: str) -> str:
        """
        Enrichit un conseil avec détails pratiques.
        
        Args:
            advice_text: Texte original (ex: "Pailler le sol")
            
        Returns:
            Texte enrichi avec COMMENT, QUAND, POURQUOI, COMBIEN
        """
        enriched = advice_text
        
        # Détection actions mentionnées
        advice_lower = advice_text.lower()
        
        for action, details in self.practical_guidelines.items():
            if action in advice_lower:
                logger.info(f"💡 Enrichissement détails pour: {action}")
                
                # Construction bloc détaillé
                detail_block = f"\n\n📋 DÉTAILS PRATIQUES - {action.upper()}:\n"
                detail_block += "="*50 + "\n\n"
                
                detail_block += f"❓ COMMENT:\n{details['comment']}\n\n"
                detail_block += f"⏰ QUAND:\n{details['quand']}\n\n"
                detail_block += f"💡 POURQUOI:\n{details['pourquoi']}\n\n"
                detail_block += f"📏 QUANTITÉ:\n{details['quantite']}\n\n"
                detail_block += f"💰 COÛT:\n{details['cout']}\n\n"
                
                if details.get('alternatives'):
                    detail_block += "🔄 ALTERNATIVES:\n"
                    for alt in details['alternatives']:
                        detail_block += f"   • {alt}\n"
                
                # Insertion après la première mention de l'action
                enriched = enriched.replace(
                    action,
                    f"**{action}**",  # Mise en évidence
                    1  # Première occurrence seulement
                )
                enriched += detail_block
                break  # Un seul enrichissement par conseil
        
        return enriched

    def enrich_response(self, response_text: str) -> str:
        """
        Enrichit une réponse complète avec détails pour TOUTES les actions.
        """
        enriched = response_text
        
        # Liste des actions détectées
        detected_actions = []
        for action in self.practical_guidelines.keys():
            if action in response_text.lower():
                detected_actions.append(action)
        
        if detected_actions:
            logger.info(f"💡 {len(detected_actions)} action(s) à enrichir: {detected_actions}")
            
            # Ajout bloc détails à la fin
            enriched += "\n\n" + "="*70 + "\n"
            enriched += "📖 INSTRUCTIONS DÉTAILLÉES\n"
            enriched += "="*70 + "\n"
            
            for action in detected_actions:
                details = self.practical_guidelines[action]
                
                enriched += f"\n🔹 {action.upper()}\n"
                enriched += f"   ❓ COMMENT: {details['comment']}\n"
                enriched += f"   ⏰ QUAND: {details['quand']}\n"
                enriched += f"   💡 POURQUOI: {details['pourquoi']}\n"
                enriched += f"   📏 QUANTITÉ: {details['quantite']}\n"
                enriched += f"   💰 COÛT: {details['cout']}\n"
                
                if details.get('alternatives'):
                    enriched += "   🔄 ALTERNATIVES:\n"
                    for alt in details['alternatives']:
                        enriched += f"      • {alt}\n"
                enriched += "\n"
        
        return enriched


# ======================================================================
# TESTS
# ======================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    enricher = PracticalDetailsEnricher()
    
    # Test 1: Conseil vague
    print("\n" + "="*70)
    print("TEST 1: Enrichissement conseil vague")
    advice = "Il faut pailler le sol pour protéger vos cultures."
    enriched = enricher.enrich_advice(advice)
    print(f"AVANT:\n{advice}\n")
    print(f"APRÈS:\n{enriched}")
    
    # Test 2: Réponse avec plusieurs actions
    print("\n" + "="*70)
    print("TEST 2: Enrichissement réponse complète")
    response = (
        "Pour lutter contre la sécheresse:\n"
        "1. Arroser de nuit\n"
        "2. Pailler le sol\n"
        "3. Surveiller les plants régulièrement"
    )
    enriched = enricher.enrich_response(response)
    print(f"AVANT:\n{response}\n")
    print(f"APRÈS:\n{enriched}")
