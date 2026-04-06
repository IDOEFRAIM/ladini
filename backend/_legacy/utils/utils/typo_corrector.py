"""
Typo Corrector - Correction Automatique Termes Agricoles Locaux
================================================================

Module pour comprendre les fautes d'orthographe courantes des agriculteurs
et corriger automatiquement AVANT le traitement.

Principe: "L'agriculteur ne doit PAS être un expert en orthographe"
"""

import re
import logging
from typing import Dict, Tuple, List
from difflib import get_close_matches

logger = logging.getLogger("TypoCorrector")


class AgriTypoCorrector:
    """
    Correcteur de fautes pour termes agricoles locaux.
    
    Cas d'usage:
    - "inera" → "INERA" (Institut de recherche)
    - "sonagess" → "SONAGESS" (Société nationale)
    - "kou" → "Koutiala"
    - "mais" → "maïs"
    """
    
    def __init__(self):
        # Dictionnaire termes officiels (version correcte)
        self.official_terms = self._build_official_dictionary()
        
        # Dictionnaire fautes courantes → correction
        self.common_typos = self._build_typo_dictionary()
        
        # Dictionnaire synonymes locaux
        self.local_synonyms = self._build_synonym_dictionary()

    def _build_official_dictionary(self) -> Dict[str, str]:
        """
        Termes officiels agricoles du Burkina Faso.
        """
        return {
            # Institutions
            "inera": "INERA",
            "sonagess": "SONAGESS",
            "bunasols": "BUNASOLS",
            "sofitex": "SOFITEX",
            "dgpv": "DGPV",
            "maah": "MAAH",
            "anpe": "ANPE",
            
            # Villes/Zones
            "kou": "Koutiala",
            "koutiala": "Koutiala",
            "bobo": "Bobo-Dioulasso",
            "ouaga": "Ouagadougou",
            "fada": "Fada N'Gourma",
            "dedougou": "Dédougou",
            "kaya": "Kaya",
            "tenkodogo": "Tenkodogo",
            
            # Cultures
            "mais": "maïs",
            "sorgho": "sorgho",
            "mil": "mil",
            "riz": "riz",
            "niebe": "niébé",
            "arachide": "arachide",
            "coton": "coton",
            "sesame": "sésame",
            "soja": "soja",
            
            # Produits phyto
            "neem": "neem",
            "karate": "Karaté (insecticide)",
            "manebe": "Manèbe (fongicide)",
            
            # Termes techniques
            "nph": "NPK",
            "uree": "urée",
            "compost": "compost",
            "fumure": "fumure",
            "paillage": "paillage",
        }

    def _build_typo_dictionary(self) -> Dict[str, str]:
        """
        Fautes courantes → Correction directe.
        """
        return {
            # Fautes fréquentes institutions
            "ineara": "INERA",
            "inéra": "INERA",
            "sonagesse": "SONAGESS",
            "sonages": "SONAGESS",
            "bunasol": "BUNASOLS",
            
            # Fautes cultures
            "maïs": "maïs",  # Accent correct
            "maiz": "maïs",
            "sorgo": "sorgho",
            "niébe": "niébé",
            "nieble": "niébé",
            "cotone": "coton",
            
            # Fautes produits
            "npk": "NPK",
            "n-p-k": "NPK",
            "karaté": "Karaté (insecticide)",
            "manèbe": "Manèbe (fongicide)",
            
            # Fautes villes
            "koutiyala": "Koutiala",
            "koutala": "Koutiala",
            "bobodioullasso": "Bobo-Dioulasso",
            "ouagadougou": "Ouagadougou",
        }

    def _build_synonym_dictionary(self) -> Dict[str, str]:
        """
        Synonymes locaux → Terme standard.
        """
        return {
            # Langues locales → Français
            "sumbala": "graines fermentées néré",
            "dolo": "bière de mil",
            "tô": "pâte de mil/sorgho",
            "zom-kom": "feuilles de baobab",
            
            # Expressions locales
            "la pluie refuse": "pas de pluie",
            "le soleil tape": "forte chaleur",
            "la terre est fatiguée": "sol appauvri",
            "les criquets attaquent": "invasion acridienne",
            
            # Termes simplifiés
            "eau de savon": "savon noir dilué",
            "poudre blanche": "urée ou NPK",
            "poudre rouge": "potasse",
        }

    def correct_query(self, query: str) -> Tuple[str, List[str]]:
        """
        Corrige automatiquement une requête utilisateur.
        
        Args:
            query: Texte original de l'utilisateur
            
        Returns:
            (texte_corrigé, liste_corrections_appliquées)
        """
        original = query
        corrections = []
        
        # 1. Normalisation basique
        query_lower = query.lower().strip()
        
        # 2. Correction typos directs (remplacement exact)
        for typo, correct in self.common_typos.items():
            if typo in query_lower:
                pattern = re.compile(re.escape(typo), re.IGNORECASE)
                query = pattern.sub(correct, query)
                corrections.append(f"{typo} → {correct}")
        
        # 3. Correction termes officiels
        words = query_lower.split()
        corrected_words = []
        
        for word in query.split():  # Garder la casse originale
            word_lower = word.lower().strip(",.!?;:")
            
            if word_lower in self.official_terms:
                corrected = self.official_terms[word_lower]
                corrected_words.append(corrected)
                if word != corrected:
                    corrections.append(f"{word} → {corrected}")
            else:
                # Recherche fuzzy (similitude)
                matches = get_close_matches(
                    word_lower,
                    self.official_terms.keys(),
                    n=1,
                    cutoff=0.8
                )
                if matches:
                    corrected = self.official_terms[matches[0]]
                    corrected_words.append(corrected)
                    corrections.append(f"{word} → {corrected} (fuzzy)")
                else:
                    corrected_words.append(word)
        
        corrected_query = " ".join(corrected_words)
        
        # 4. Remplacement synonymes locaux
        for local, standard in self.local_synonyms.items():
            if local in corrected_query.lower():
                pattern = re.compile(re.escape(local), re.IGNORECASE)
                corrected_query = pattern.sub(f"{standard} ({local})", corrected_query)
                corrections.append(f"'{local}' → '{standard}'")
        
        if corrections:
            logger.info(f"✏️ Corrections appliquées: {corrections}")
        
        return corrected_query, corrections

    def suggest_completions(self, partial_query: str, max_suggestions: int = 5) -> List[str]:
        """
        Suggère des complétions pour une requête partielle.
        
        Utile pour auto-complétion dans l'interface.
        """
        partial_lower = partial_query.lower().strip()
        
        suggestions = []
        
        # Recherche dans termes officiels
        for term in self.official_terms.keys():
            if term.startswith(partial_lower):
                suggestions.append(self.official_terms[term])
        
        # Recherche fuzzy si pas assez de résultats
        if len(suggestions) < max_suggestions:
            fuzzy_matches = get_close_matches(
                partial_lower,
                self.official_terms.keys(),
                n=max_suggestions - len(suggestions),
                cutoff=0.6
            )
            for match in fuzzy_matches:
                suggestion = self.official_terms[match]
                if suggestion not in suggestions:
                    suggestions.append(suggestion)
        
        return suggestions[:max_suggestions]


# ======================================================================
# TESTS
# ======================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    corrector = AgriTypoCorrector()
    
    # Test 1: Faute INERA
    print("\n" + "="*60)
    print("TEST 1: Faute INERA")
    query = "Je veux contacter l'ineara pour conseil sur mais"
    corrected, corrections = corrector.correct_query(query)
    print(f"Original : {query}")
    print(f"Corrigé  : {corrected}")
    print(f"Changes  : {corrections}")
    
    # Test 2: Fautes multiples
    print("\n" + "="*60)
    print("TEST 2: Fautes multiples")
    query = "Prix maiz a kou et comment contacter sonagesse?"
    corrected, corrections = corrector.correct_query(query)
    print(f"Original : {query}")
    print(f"Corrigé  : {corrected}")
    print(f"Changes  : {corrections}")
    
    # Test 3: Expression locale
    print("\n" + "="*60)
    print("TEST 3: Expression locale")
    query = "Le soleil tape et la pluie refuse, ma terre est fatiguée"
    corrected, corrections = corrector.correct_query(query)
    print(f"Original : {query}")
    print(f"Corrigé  : {corrected}")
    print(f"Changes  : {corrections}")
    
    # Test 4: Auto-complétion
    print("\n" + "="*60)
    print("TEST 4: Auto-complétion")
    suggestions = corrector.suggest_completions("ine")
    print(f"Suggestions pour 'ine': {suggestions}")
