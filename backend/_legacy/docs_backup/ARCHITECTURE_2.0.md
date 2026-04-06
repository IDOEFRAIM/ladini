# AgriConnect 2.0 — Architecture Tri-Protocoles
## Blueprint Technique National

---

## 1. Philosophie : "Protocol-First Architecture"

AgriConnect n'est plus un monolithe Python.
C'est un **écosystème** où chaque protocole a son rôle :

| Protocole | Rôle | Analogie |
|-----------|------|----------|
| **MCP** | Système Nerveux | Accès standardisé aux données (DB, RAG, Météo) |
| **A2A** | Réseau Social | Communication inter-agents (discovery, matching, négociation) |
| **AG-UI** | Visage Universel | Rendu adaptatif multi-canal (WhatsApp, Web, SMS/USSD) |

---

## 2. Architecture des Fichiers

```
backend/protocols/
├── __init__.py                # Point d'entrée protocoles
├── mcp/                       # MCP — Système Nerveux
│   ├── __init__.py
│   ├── mcp_db.py              # Serveur MCP Database (Profils, Épisodes, Marketplace)
│   ├── mcp_rag.py             # Serveur MCP RAG (Base documentaire agronomique)
│   ├── mcp_weather.py         # Serveur MCP Weather (Météo, Alertes, Satellites)
│   └── mcp_context.py         # Serveur MCP Context Host (Optimiseur de contexte)
├── ag_ui/                     # AG-UI — Visage Universel
│   ├── __init__.py
│   ├── components.py          # Composants structurés (Card, Action, ListPicker, Chart, Alert)
│   └── renderer.py            # Renderers (WhatsApp, Web, SMS/USSD)
└── a2a/                       # A2A — Réseau Social des Agents
    ├── __init__.py
    ├── registry.py            # Registre d'agents (Agent Cards, indexation)
    ├── messaging.py           # Canal de messages (send, broadcast, handshake)
    └── discovery.py           # Service de discovery et routing
```

---

## 3. Serveurs MCP (Détail)

### 3.1 MCP Database (`mcp_db.py`)

**Ressources exposées :**
| URI | Description |
|-----|-------------|
| `agri://profile/{user_id}` | Profil ferme JSONB |
| `agri://episodes/{user_id}` | Mémoire épisodique |
| `agri://marketplace/products/{zone}` | Produits disponibles par zone |

**Outils exposés :**
| Outil | Description |
|-------|-------------|
| `update_profile(user_id, patch)` | Mise à jour MERGE du profil |
| `search_episodes(user_id, ...)` | Recherche épisodique filtrée |

### 3.2 MCP RAG (`mcp_rag.py`)

| Outil | Description |
|-------|-------------|
| `search_agronomy_docs(query, region, level, top_k)` | Recherche HyDe + reranking |
| `get_doc_sources(query)` | Traçabilité des sources |

### 3.3 MCP Weather (`mcp_weather.py`)

| Outil | Description |
|-------|-------------|
| `get_weather(location)` | Conditions actuelles |
| `get_forecast(location, days)` | Prévisions |
| `get_alerts(zone)` | Alertes climatiques |
| `get_flood_risk(location)` | Risque inondation |
| `get_satellite_data(location)` | NDVI/EVI |

### 3.4 MCP Context Host (`mcp_context.py`)

| Outil | Description |
|-------|-------------|
| `build_context(user_id, query, zone, crop)` | Contexte optimisé complet |
| `enrich_state(state)` | Enrichissement de l'état orchestrateur |
| `record_interaction(...)` | Enregistrement épisodique |
| `get_token_budget()` | Budget tokens par composant |

---

## 4. Composants AG-UI (Détail)

| Composant | WhatsApp | Web | SMS/USSD |
|-----------|----------|-----|----------|
| `TextBlock` | Message texte | `<p>` | Texte brut |
| `Card` | Formaté + image | Card Material | Résumé condensé |
| `ActionButton` | Bouton interactif | `<button>` | "Tapez 1 pour..." |
| `ListPicker` | Liste interactive | Dropdown | Menu numéroté |
| `FormField` | Question interactive | `<input>` | "Répondez avec..." |
| `ChartData` | Image générée | Chart.js | Résumé tendances |
| `AlertBanner` | ⚠️ Message alerte | Bannière colorée | "ALERTE: ..." |

---

## 5. Protocole A2A (Détail)

### 5.1 Agents Internes Enregistrés

| Agent ID | Domaine | Intents | Zones |
|----------|---------|---------|-------|
| `plant_doctor` | Diagnosis | DIAGNOSE, IDENTIFY_DISEASE, RECOMMEND_TREATMENT | ALL |
| `market_coach` | Market | CHECK_PRICE, SELL_OFFER, BUY_OFFER, SCAM_CHECK | 8 villes |
| `formation_coach` | Formation | LEARN, HOW_TO, BEST_PRACTICE | ALL |
| `climate_sentinel` | Weather | CHECK_WEATHER, GET_ALERT, FLOOD_RISK | ALL |
| `marketplace_agent` | Marketplace | REGISTER_STOCK, SELL_PRODUCT, FIND_BUYERS, MATCH_OFFER | ALL |

### 5.2 Workflow Handshake (Négociation)

```
Vendeur → PROPOSED → Acheteur
Acheteur → ACCEPTED / REJECTED / COUNTER → Vendeur
Si COUNTER → retour à l'étape 2
COMPLETED quand les deux acceptent
```

### 5.3 Agents Externes (à intégrer)

| Partenaire | Domaine | Intents |
|------------|---------|---------|
| SONAGESS | Inventory | CHECK_STOCK, SUPPLY |
| Banque locale | Finance | SCORE_CREDIT, LOAN_REQUEST |
| Transporteur | Transport | ESTIMATE_DELIVERY, BOOK_TRANSPORT |
| Coopérative | Marketplace | BULK_ORDER, GROUP_SELL |

---

## 6. Matrice de Cohérence (Checklist Qualité)

| Composant | Protocole | Pourquoi |
|-----------|-----------|----------|
| Accès Base de Données | **MCP** (Resources) | Sécurité : empêche un agent d'halluciner des données SQL |
| Consultation Météo/Prix | **MCP** (Tools) | Standardisation : changement de fournisseur transparent |
| Dialogue Utilisateur | **AG-UI** | Accessibilité : lisible par l'analphabète et l'expert |
| Matching Vente | **A2A** | Scalabilité : 10 000 acheteurs sans bloquer l'orchestrateur |
| Scoring Crédit | **MCP** (Read) + **Signature** | Confiance : preuve que les données viennent de la DB réelle |

---

## 7. Flux de Requête AgriConnect 2.0

```
1. Requête Utilisateur (WhatsApp / Web / SMS)
   │
2. AG-UI : Détection du canal → choix du renderer
   │
3. Orchestrateur (message_flow.py)
   ├── MCP Context : build_context(user_id, query) → profil + épisodes
   ├── Analyse des besoins (LLM)
   ├── A2A Discovery : find_agents(intent, zone)
   │
4. Fan-out : Agents en parallèle
   ├── PlantDoctor    ← MCP RAG (search_agronomy_docs)
   ├── MarketCoach    ← MCP DB (marketplace products)
   ├── ClimateSentinel ← MCP Weather (get_weather, get_alerts)
   ├── FormationCoach ← MCP RAG (search_agronomy_docs)
   └── MarketplaceAgent ← A2A (broadcast_offer, handshake)
   │
5. Fan-in : Synthèse des réponses
   │
6. AG-UI : Rendu adaptatif
   ├── WhatsApp → Messages interactifs Twilio
   ├── Web → JSON enrichi React/Vue
   └── SMS → Segments condensés
   │
7. MCP Context : record_interaction() → mémoire épisodique
```

---

## 8. Modèles LLM Utilisés

| Modèle | Usage | Contexte |
|--------|-------|----------|
| `llama-3.1-8b-instant` | Planification, extraction, résumés | Rapide, léger |
| `llama-3.3-70b-versatile` | Réponses finales, diagnostic | Puissant, raisonné |
| Provider : **Groq** | API LLM | Inférence ultra-rapide |

---

## 9. Estimation Tokens par Message (post-optimisation)

| Composant | Tokens |
|-----------|--------|
| Profil structuré (MCP DB) | ~80 |
| Épisodes pertinents (MCP Context) | ~120 |
| Prompt système (cacheable) | ~500-700 |
| Contexte RAG (MCP RAG) | ~500-800 |
| Question utilisateur | ~100 |
| **Total par agent** | **~1 300-1 800** |
| **Plafond absolu** | **2 200** |

---

## 10. Prochaines Étapes

1. **Semaine 1** : Migrer les appels directs SQL/API → MCP Tools
2. **Semaine 2** : Faire produire des `AgriResponse` AG-UI par chaque agent
3. **Semaine 3** : Activer le handshake A2A pour la Marketplace
4. **Semaine 4** : Intégrer le premier agent externe (SONAGESS ou banque)
5. **Semaine 5** : Déployer AP2 (mandats signés) pour les transactions
