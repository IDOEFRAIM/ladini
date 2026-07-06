import asyncio
from agriconnect.infrastructure.mcp.security import MCPShield # Ajustez le chemin
import time
async def main():
    shield = MCPShield(session_id="test_local")
    
    # 1. Lister les outils pour vérifier que le registre charge bien
    tools = await shield.list_allowed_tools()
    print(f"Outils détectés: {len(tools)}")
    start = time.time()
    # 2. Appeler un outil de lecture simple (ex: db_status si existant)
    try:
        result = await shield.authorize_and_call("get_producer_orders", {"phone":"+212782901759","producer_id":"0669b8b0-8e8b-4838-81de-aaef50538974"})
        print("Résultat:", result)
        print(f"Le temps mis:{time.time()-start}")
    except Exception as e:
        print(f"Erreur lors de l'appel: {e}")

if __name__ == "__main__":
    print("="*20)
    print("We re starting")
    
    asyncio.run(main())
    print("="*20)