"""
Off-Topic Guard - Garde-fou Anti Hors-Sujet
============================================

Module pour REJETER toutes les questions non-agricoles.

Principe: "AgriBot = Assistant AGRICOLE uniquement, pas encyclopédie générale"
"""

import logging
from typing import Dict, Tuple, Optional
import re

logger = logging.getLogger("OffTopicGuard")


class OffTopicGuard:
    """
    Filtre qui rejette IMMÉDIATEMENT toute question hors domaine agricole.
    
    ACCEPTÉ:
    - Agriculture (cultures, élevage, sol, eau)
    - Marché rural (prix, vente, achat)
    - Météo agricole
    - Santé plantes/animaux
    - Subventions/aides agricoles
    - Techniques culturales
    
    REJETÉ:
    - Histoire (Vietnam, guerres...)
    - Musique/Art (rap, artistes...)
    - Politique générale
    - Sport
    - Technologie non-agricole
    - Questions philosophiques
    """
    
    def __init__(self):
        self.agricultural_keywords = self._build_agricultural_keywords()
        self.off_topic_keywords = self._build_off_topic_keywords()

    def _build_agricultural_keywords(self) -> set:
        """
        Mots-clés acceptés (domaine agricole).
        """
        return {
            # Cultures
            "maïs", "mais", "sorgho", "mil", "riz", "coton", "niébé", "arachide",
            "soja", "sésame", "igname", "manioc", "patate",
            
            # Élevage
            "vache", "bœuf", "mouton", "chèvre", "porc", "poulet", "volaille",
            "élevage", "bétail", "pâturage", "fourrage",
            
            # Agriculture générale
            "culture", "cultiver", "plantation", "semis", "récolte", "champ",
            "parcelle", "hectare", "rendement", "production",
            
            # Sol/Eau
            "sol", "terre", "compost", "fumure", "engrais", "npk", "urée",
            "irrigation", "arrosage", "eau", "pluie", "sécheresse",
            
            # Santé plantes
            "maladie", "ravageur", "insecte", "criquet", "chenille", "puceron",
            "traitement", "pesticide", "neem", "fongicide",
            
            # Marché
            "prix", "vendre", "acheter", "marché", "coopérative", "acheteur",
            "bénéfice", "revenu", "argent", "fcfa",
            
            # Météo
            "météo", "temps", "température", "chaleur", "canicule", "inondation",
            "prévision", "saison",
            
            # Institutions agricoles
            "inera", "sonagess", "bunasols", "sofitex", "maah", "anpe",
            "conseiller", "technicien", "vulgarisation",
            
            # Techniques
            "semoir", "charrue", "houe", "machette", "tracteur", "motoculteur",
            "paillage", "buttage", "désherbage", "labour",
            
            # Salutations (OK mais pas le sujet principal)
            "bonjour", "bonsoir", "salut", "merci", "aide", "conseil",
        }

    def _build_off_topic_keywords(self) -> Dict[str, str]:
        """
        Mots-clés INTERDITS avec message de rejet associé.
        
        Returns:
            Dict[mot_interdit, message_rejet]
        """
        return {
            # Histoire/Guerre
            "guerre": "Je suis un assistant agricole, pas un historien.",
            "vietnam": "Je ne réponds qu'aux questions agricoles.",
            "conflit": "Je me concentre uniquement sur l'agriculture.",
            "bataille": "Mon domaine est l'agriculture, pas l'histoire militaire.",
            
            # Musique/Art
            "rap": "Je ne suis pas un expert musical, mais agricole.",
            "artiste": "Je me concentre sur l'agriculture, pas l'art.",
            "chanson": "Mon expertise est agricole, pas musicale.",
            "musique": "Je ne traite que des questions agricoles.",
            "concert": "Je suis spécialisé en agriculture uniquement.",
            
            # Politique générale
            "président": "Je ne donne pas d'avis politiques, seulement agricoles.",
            "élection": "Mon rôle est d'aider les agriculteurs, pas la politique.",
            "gouvernement": "Je traite uniquement des politiques agricoles.",
            "parti": "Je me limite aux questions agricoles.",
            
            # Sport
            "football": "Je suis assistant agricole, pas sportif.",
            "basket": "Mon expertise est l'agriculture.",
            "match": "Je ne traite que de questions agricoles.",
            
            # Technologie non-agricole
            "smartphone": "Je ne fais que de la technologie agricole.",
            "ordinateur": "Mon domaine est l'agriculture.",
            "internet": "Je me concentre sur l'agriculture uniquement.",
            
            # Santé humaine générale
            "cancer": "Je ne suis pas médecin, contactez un professionnel de santé.",
            "maladie humaine": "Consultez un médecin, je traite de santé végétale.",
            "hôpital": "Je ne traite que de santé agricole.",
            
            # Philosophie/Religion
            "dieu": "Je me concentre sur des questions pratiques agricoles.",
            "philosophie": "Mon rôle est d'aider concrètement les agriculteurs.",
            "religion": "Je traite uniquement d'agriculture.",
        }

    def check_query(self, query: str) -> Tuple[bool, Optional[str]]:
        """
        Vérifie si une requête est dans le domaine agricole.
        
        Args:
            query: Texte de la requête utilisateur
            
        Returns:
            (is_on_topic, rejection_message)
            - is_on_topic: True si agricole, False sinon
            - rejection_message: Message à afficher si hors-sujet
        """
        query_lower = query.lower().strip()
        
        # 1. Vérification mots-clés INTERDITS (priorité)
        for forbidden_word, rejection_msg in self.off_topic_keywords.items():
            if forbidden_word in query_lower:
                logger.warning(f"🚫 Hors-sujet détecté: '{forbidden_word}' dans '{query[:50]}'")
                return False, rejection_msg
        
        # 2. Vérification présence mots-clés agricoles
        words = re.findall(r'\w+', query_lower)
        agricultural_words_found = sum(1 for word in words if word in self.agricultural_keywords)
        
        # Si au moins 20% des mots sont agricoles, on accepte
        if len(words) > 0:
            ratio = agricultural_words_found / len(words)
            if ratio >= 0.15:  # Au moins 15% de mots agricoles
                logger.info(f"✅ Question agricole acceptée ({ratio*100:.0f}% mots agricoles)")
                return True, None
        
        # 3. Cas spécial: salutations seules (OK)
        greetings = {"bonjour", "bonsoir", "salut", "hello", "hi"}
        if query_lower in greetings or len(words) <= 2:
            return True, None
        
        # 4. Si aucun mot agricole trouvé → REJET
        logger.warning(f"🚫 Pas de mots agricoles trouvés dans: '{query[:50]}'")
        rejection_message = (
            "Désolé, je suis un assistant agricole spécialisé. "
            "Je ne peux vous aider que sur:\n"
            "- 🌾 Cultures (maïs, coton, riz...)\n"
            "- 🐄 Élevage\n"
            "- 💰 Prix marché\n"
            "- ☁️ Météo agricole\n"
            "- 🌱 Santé des plantes\n"
            "- 🏛️ Subventions agricoles\n\n"
            "Reformulez votre question sur l'un de ces sujets agricoles."
        )
        return False, rejection_message

    def enforce_agricultural_scope(self, query: str) -> str:
        """
        Si la question est valide, la retourne telle quelle.
        Si hors-sujet, lève une exception avec message de rejet.
        
        Usage dans orchestrator:
        >>> try:
        >>>     query = guard.enforce_agricultural_scope(user_query)
        >>> except ValueError as e:
        >>>     return {"response": str(e), "status": "OFF_TOPIC"}
        """
        is_on_topic, rejection_msg = self.check_query(query)
        
        if not is_on_topic:
            raise ValueError(rejection_msg)
        
        return query


# ======================================================================
# TESTS
# ======================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    guard = OffTopicGuard()
    
    test_cases = [
        # Cas acceptés
        ("Quel est le prix du maïs?", True),
        ("Comment traiter la chenille légionnaire?", True),
        ("Météo à Koutiala demain?", True),
        ("Bonjour, j'ai besoin d'aide", True),
        ("Comment contacter l'INERA?", True),
        
        # Cas rejetés
        ("Qui a gagné la guerre du Vietnam?", False),
        ("Parle-moi des artistes rap au Sahel", False),
        ("Le président a-t-il raison?", False),
        ("Match de football ce soir?", False),
        ("Comment utiliser un smartphone?", False),
    ]
    
    print("\n" + "="*70)
    print("TESTS OFF-TOPIC GUARD")
    print("="*70)
    
    for query, expected_on_topic in test_cases:
        is_on_topic, rejection_msg = guard.check_query(query)
        
        status = "✅ PASS" if (is_on_topic == expected_on_topic) else "❌ FAIL"
        result = "ACCEPTÉ" if is_on_topic else "REJETÉ"
        
        print(f"\n{status} | {result}")
        print(f"Query: {query}")
        if rejection_msg:
            print(f"Rejet: {rejection_msg[:80]}...")
