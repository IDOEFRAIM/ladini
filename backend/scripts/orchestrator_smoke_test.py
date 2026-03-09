import logging

from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow


def run_smoke_tests():
    logging.basicConfig(level=logging.INFO)
    bot = MessageResponseFlow()

    print("\n--- TEST 1: CHAT ---")
    state1 = {
        "requete_utilisateur": "Bonjour AgriBot, comment ça va ?",
        "zone_id": "Bobo",
        "crop": "Maïs",
    }
    print(bot.run(state1).get("final_response"))

    print("\n--- TEST 2: SOLO FORMATION ---")
    state2 = {
        "requete_utilisateur": "Explique-moi comment faire un compost simple.",
        "zone_id": "Bobo",
        "crop": "Maïs",
    }
    print(bot.run(state2).get("final_response"))

    print("\n--- TEST 3: CONSEIL PARALLÈLE ---")
    state3 = {
        "requete_utilisateur": "Que sais tu de mais?",
        "zone_id": "Sud-Ouest",
        "crop": "Maïs",
    }
    result = bot.run(state3)
    print(result.get("final_response"))
    print(f"Execution path: {result.get('execution_path')}")
    print(f"Expert responses: {len(result.get('expert_responses', []))}")


if __name__ == "__main__":
    run_smoke_tests()
