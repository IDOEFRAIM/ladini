"""Registre statique des agents internes AgriConnect."""

from agriconnect.protocols.a2a.registry import AgentCard, AgentDomain

internal_agents = [
            AgentCard(
                agent_id="plant_doctor",
                name="PlantDoctor",
                description="Diagnostic phytosanitaire et recommandations de traitement",
                domain=AgentDomain.DIAGNOSIS,
                intents=["DIAGNOSE", "IDENTIFY_DISEASE", "RECOMMEND_TREATMENT", "CHECK_SYMPTOM"],
                capabilities=["text", "image", "voice"],
                zones=["all"],
                crops=["all"],
                protocol="internal",
                avg_response_ms=800,
            ),
            AgentCard(
                agent_id="market_coach",
                name="MarketCoach",
                description="Analyse de marché, prix et conseil de vente",
                domain=AgentDomain.MARKET,
                intents=["CHECK_PRICE", "SELL_OFFER", "BUY_OFFER", "SCAM_CHECK", "MARKET_ANALYSIS"],
                capabilities=["text", "voice"],
                # Use a generic zone subscription — agents should filter by payload.zone
                zones=["all"],
                crops=["all"],
                protocol="internal",
                avg_response_ms=600,
            ),
            AgentCard(
                agent_id="formation_coach",
                name="FormationCoach",
                description="Formation agricole et conseil technique",
                domain=AgentDomain.FORMATION,
                intents=["LEARN", "HOW_TO", "BEST_PRACTICE", "TRAINING_MODULE"],
                capabilities=["text", "voice"],
                zones=["all"],
                crops=["all"],
                protocol="internal",
                avg_response_ms=700,
            ),
            AgentCard(
                agent_id="climate_sentinel",
                name="ClimateSentinel",
                description="Veille climatique, alertes météo et conseil agrométéo",
                domain=AgentDomain.WEATHER,
                intents=["CHECK_WEATHER", "GET_ALERT", "FLOOD_RISK", "SATELLITE_DATA", "AGRO_METEO"],
                capabilities=["text", "voice", "map"],
                zones=["all"],
                crops=["all"],
                protocol="internal",
                avg_response_ms=500,
            ),
            AgentCard(
                agent_id="marketplace_agent",
                name="MarketplaceAgent",
                description="Gestion stocks, annonces, matching acheteur-vendeur",
                domain=AgentDomain.MARKETPLACE,
                intents=[
                    "REGISTER_STOCK", "SELL_PRODUCT", "BUY_PRODUCT",
                    "CHECK_STOCK", "CHECK_ORDERS", "FIND_BUYERS",
                    "FIND_PRODUCTS", "CREATE_ORDER", "MATCH_OFFER",
                ],
                capabilities=["text", "voice", "transaction"],
                zones=["all"],
                crops=["all"],
                protocol="internal",
                avg_response_ms=600,
            ),
        ]


