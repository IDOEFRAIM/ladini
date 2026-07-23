"""
Test du filtre AgriScope (détection hors-sujet par LLM)
"""

from agriconnect.orchestrator.intention import AgriScopeChecker
from futur.rag.components import get_llm_client

def test_agriscope():
    """Test de la détection hors-sujet intelligente"""
    
    print("=" * 60)
    print("TEST AGRISCOPE - Détection Hors-Sujet par LLM")
    print("=" * 60)
    
    llm = get_llm_client()
    checker = AgriScopeChecker(llm_client=llm)
    
    test_cases = [
        # Questions AGRICOLES (doivent être acceptées)
        ("Prix du maïs à Koutiala", True),
        ("Comment traiter la chenille légionnaire?", True),
        ("Météo à Bobo-Dioulasso demain", True),
        ("Contacter INERA pour conseil", True),
        ("Quand semer le coton?", True),
        ("Engrais NPK combien par hectare?", True),
        
        # Questions HORS-SUJET (doivent être rejetées)
        ("Qui a gagné la guerre du Vietnam?", False),
        ("Les meilleurs artistes rap du Sahel", False),
        ("Match de football ce soir à Ouaga", False),
        ("Comment utiliser un smartphone Samsung?", False),
        ("Élections présidentielles 2025", False),
    ]
    
    passed = 0
    failed = 0
    
    for query, expected_agricultural in test_cases:
        result = checker.check_scope(query)
        is_agricultural = result["is_agricultural"]
        confidence = result["confidence"]
        reason = result["reason"]
        
        status = "✅ PASS" if is_agricultural == expected_agricultural else "❌ FAIL"
        emoji = "✅" if is_agricultural else "🚫"
        
        if is_agricultural == expected_agricultural:
            passed += 1
        else:
            failed += 1
        
        print(f"\n{status} | {emoji} | Conf: {confidence:.2f}")
        print(f"Query: {query}")
        print(f"Reason: {reason}")
    
    print("\n" + "=" * 60)
    print(f"RÉSULTATS: {passed} PASS, {failed} FAIL ({passed}/{len(test_cases)})")
    print("=" * 60)

if __name__ == "__main__":
    test_agriscope()
